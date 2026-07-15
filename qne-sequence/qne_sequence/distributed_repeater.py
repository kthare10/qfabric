"""Distributed repeater chain — entanglement swapping across N processes.

This distributes the heralding of ``repeater.py`` (the in-process chain) over real
links, completing the "prove in-process, then distribute" path used for E91. The
chain has ``K = num_stations`` repeater stations between the endpoints, so
``N = K + 2`` processes and ``L = K + 1`` elementary links:

  * **alice** (role 0) hosts the QuantumStateService — the register authority —
    and generates all L elementary link pairs per attempt. She measures her end
    (a₁) locally and serves the other parties' quantum ops over the wire.
  * **station i** (role 2, ``station_index = i``) holds the two middle halves of
    its segment (b_i, a_{i+1}). It performs its Bell-state measurements as a
    batched RPC against the register (BSM_REQ), then forwards its (m1, m2)
    herald bits to Bob over its OWN link — the classical herald traffic whose
    latency is the multi-hop research lever. A station neither knows nor cares
    how many siblings it has; the station code is identical for any K.
  * **bob** (role 1) receives one herald stream per station, composes the Pauli
    correction per pair by XOR (X^x·Z^z with x = ⊕m2ᵢ, z = ⊕m1ᵢ — valid because
    Pauli corrections compose by XOR), applies it to his half (b_L) via RPC,
    measures, then runs the standard sift / QBER-sample / CHSH disclosure and
    Cascade+PA against Alice.

Links (2K+1 total): alice↔station_i carries the swap plan + BSM ops; station_i→bob
carries only heralds; alice↔bob carries the end-to-end QKD classical protocol.
Port scheme on top of ``port_ab``: station i listens for alice on
``port_ab + 2i − 1`` and bob listens for station i on ``port_ab + 2i`` (for
K = 1 this reduces to the original port+1 / port+2 layout). Only public data
crosses any link: qubit ids, basis codes, heralds, the QBER sample, parities.

Physics notes: Alice measures her end *before* the swaps happen (delayed-choice
entanglement swapping), and the stations swap independently in any order (the
BSMs commute; the register merges groups in whatever order ops arrive — Alice
serializes them in station order for determinism). The end-to-end statistics
follow the Werner chain law F = (1 + 3·f^L)/4. Skipping the correction
(``--no-correction``) collapses QBER to 1/2: the herald channels are load-bearing.
"""

from __future__ import annotations

from time import sleep

import numpy as np

from qne.bb84 import BB84Protocol

from .distributed_e91 import _key_positions
from .e91 import _ANGLE, _CHSH, _MODES, chsh_value
from .listener import Link
from .quantum_state_service import QuantumStateService
from .reconcile_link import bits_to_int, drive_cascade, serve_parities
from .remote_qm import RpcChannel
from .repeater import chain_chsh, chain_fidelity, chain_qber
from .timesync import sync_link

ROLES = {"alice": 0, "bob": 1, "repeater": 2}
_SERVE_TIMEOUT = 60.0               # accept window per listener (K links come up serially)


def run_repeater_node(role: int, name: str, host: str, *, port_ab: int,
                      port_ar: int = 0, port_rb: int = 0,
                      num_stations: int = 1, station_index: int = 1,
                      num_pairs: int = 5000,
                      fidelity: float = 0.95, loss_probability: float = 0.0,
                      mode: str = "bbm92", sample_fraction: float = 0.1,
                      seed: int = 0, do_reconcile: bool = True,
                      cascade_passes: int = 4, finite_key: bool = False,
                      eps_sec: float = 1e-9, eps_cor: float = 1e-15,
                      auth_key: str | None = None,
                      apply_correction: bool = True,
                      bob_host: str | None = None,
                      repeater_host: str | None = None,
                      repeater_hosts: list[str] | None = None,
                      channel_delay: int = 0) -> dict:
    """Run one node of the N-process repeater chain; return its result dict.

    Start order (listeners first): bob, stations, alice — though ``Link.connect``
    retries make the order forgiving. ``mode`` is 'bbm92' (Z/X key) or 'e91'
    (adds the CHSH test across the swapped chain). ``loss_probability`` applies
    per LINK; an attempt whose links don't all survive is never generated.

    Addressing: ``host`` is this node's LISTEN address. ``bob_host`` is where
    bob's listeners are reached; ``repeater_hosts`` lists where each station's
    listener is reached (one entry per station, alice-side); both default to
    ``host`` (right on loopback). ``port_ar``/``port_rb`` override the derived
    ports only for the single-station chain (kept for compatibility).
    """
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r} (use 'bbm92' or 'e91')")
    if num_stations < 1:
        raise ValueError("a repeater chain needs at least 1 station")
    if not (1 <= station_index <= num_stations):
        raise ValueError(f"station_index {station_index} outside 1..{num_stations}")
    bob_host = bob_host or host
    if repeater_hosts is None:
        repeater_hosts = [repeater_host or host] * num_stations
    if len(repeater_hosts) != num_stations:
        raise ValueError(f"repeater_hosts needs {num_stations} entries")

    def _port_ar(i: int) -> int:
        if num_stations == 1 and port_ar:
            return port_ar
        return port_ab + 2 * i - 1

    def _port_rb(i: int) -> int:
        if num_stations == 1 and port_rb:
            return port_rb
        return port_ab + 2 * i

    fk_eps = {"eps_sec": eps_sec, "eps_cor": eps_cor} if finite_key else None
    links: list[Link] = []
    rpcs: list[RpcChannel] = []
    syncs: list[dict] = []

    def _paced_rpc(link: Link, serving: bool) -> RpcChannel:
        # Per-link clock sync (before start_rx), then a lookahead-paced channel:
        # every classical message on this link is delivered at t_send + delay.
        offset_ns, rtt_ns = sync_link(link, serving=serving)
        syncs.append({"offset_ns": offset_ns, "rtt_ns": rtt_ns})
        rpc = RpcChannel(link, delay_ps=channel_delay, peer_offset_ns=offset_ns)
        rpcs.append(rpc)
        return rpc

    try:
        if role == ROLES["bob"]:
            ab = Link(auth_key=auth_key)
            ab.serve(host, port_ab, timeout=_SERVE_TIMEOUT)
            rpc_a = _paced_rpc(ab, serving=True)
            ab.start_rx()
            links = [ab]
            rpc_stations = []
            for i in range(1, num_stations + 1):
                rb = Link(auth_key=auth_key)
                rb.serve(host, _port_rb(i), timeout=_SERVE_TIMEOUT)
                rpc_stations.append(_paced_rpc(rb, serving=True))
                rb.start_rx()
                links.append(rb)
            result = _run_bob(rpc_a, rpc_stations, num_pairs, mode,
                              sample_fraction, seed, do_reconcile,
                              cascade_passes, fk_eps, apply_correction)
        elif role == ROLES["repeater"]:
            ar = Link(auth_key=auth_key)
            ar.serve(host, _port_ar(station_index), timeout=_SERVE_TIMEOUT)
            rpc_a = _paced_rpc(ar, serving=True)
            rb = Link(auth_key=auth_key)
            rb.connect(bob_host, _port_rb(station_index))
            rpc_b = _paced_rpc(rb, serving=False)
            links = [ar, rb]
            ar.start_rx()
            rb.start_rx()
            result = _run_repeater(rpc_a, rpc_b)
        else:                                       # alice — register authority
            rpc_stations = []
            for i in range(1, num_stations + 1):
                ar = Link(auth_key=auth_key)
                ar.connect(repeater_hosts[i - 1], _port_ar(i))
                rpc_stations.append(_paced_rpc(ar, serving=False))
                ar.start_rx()
                links.append(ar)
            ab = Link(auth_key=auth_key)
            ab.connect(bob_host, port_ab)
            rpc_b = _paced_rpc(ab, serving=False)
            ab.start_rx()
            links.append(ab)
            # Start barrier (mirrors the two-node path's _START_BARRIER_PS):
            # bob accepts + syncs its K station links serially AFTER ab, each
            # gated by that station's 0.25 s connect-retry loop — so without a
            # barrier alice's first PLAN frames can be stamped before bob is
            # able to dequeue them, which reads as a spurious "late" in the
            # lookahead certificate.
            sleep(0.5 + 0.25 * num_stations)
            result = _run_alice(rpc_stations, rpc_b, num_pairs, fidelity,
                                loss_probability, mode, sample_fraction, seed,
                                do_reconcile, fk_eps)
    finally:
        for link in links:
            link.close()

    result.update({
        "role": role, "name": name, "mode": mode,
        "num_nodes": num_stations + 2, "num_links": num_stations + 1,
        "num_stations": num_stations, "corrected": apply_correction,
        "quantum_transport": "entangled-state-service",
        "tx_frames": sum(lk.tx_count for lk in links),
        "rx_frames": sum(lk.rx_count for lk in links),
        "authenticated": auth_key is not None,
        "auth_failures": sum(lk.auth_failures for lk in links),
        "channel_delay_ps": channel_delay,
        "timesync": syncs,
        "lookahead": {
            "on_time_events": sum(r.on_time_events for r in rpcs),
            "late_events": sum(r.late_events for r in rpcs),
            "max_lateness_ps": max((r.max_lateness_ns for r in rpcs),
                                   default=0) * 1000,
        },
    })
    if role == ROLES["repeater"]:
        result["station_index"] = station_index
    return result


def _herald_hist(heralds) -> dict:
    hist: dict[str, int] = {}
    for m1, m2 in heralds:
        key = f"{m1}{m2}"
        hist[key] = hist.get(key, 0) + 1
    return hist


def _run_alice(rpc_stations, rpc_b, num_pairs, fidelity, loss_probability, mode,
               sample_fraction, seed, do_reconcile, fk_eps):
    spec = _MODES[mode]
    key_codes = set(spec["key"])
    num_stations = len(rpc_stations)
    num_links = num_stations + 1
    svc = QuantumStateService(seed=seed)
    loss_rng = np.random.default_rng(seed + 77)
    a_rng = np.random.default_rng(seed + 101)

    # Generate all L link pairs per surviving attempt (per-LINK loss, as in
    # repeater.py: a failed link heralds "no pair", the attempt just retries).
    # Station i swaps (b_i, a_{i+1}); the end-to-end pair is (a_1, b_L).
    swap_pairs: list[list[list[int]]] = [[] for _ in range(num_stations)]
    a_end_ids: list[int] = []              # a_1 — Alice's end qubit
    b_end_ids: list[int] = []              # b_L — Bob's end qubit
    for _ in range(num_pairs):
        if loss_probability > 0.0 and any(
                loss_rng.random() < loss_probability for _ in range(num_links)):
            continue
        pairs = [svc.register.create_bell_pair(fidelity) for _ in range(num_links)]
        for i in range(num_stations):
            swap_pairs[i].append([int(pairs[i][1]), int(pairs[i + 1][0])])
        a_end_ids.append(pairs[0][0])
        b_end_ids.append(pairs[-1][1])
    n_del = len(a_end_ids)

    # Alice measures her end first (delayed-choice: order doesn't change the
    # heralded statistics) and announces her codes — public after measurement.
    a_codes = a_rng.choice(np.array(spec["alice"]), size=n_del)
    a_bits = [svc.measure(a_end_ids[k], _ANGLE[int(a_codes[k])])
              for k in range(n_del)]

    for i, rpc in enumerate(rpc_stations):
        rpc.send("PLAN_R", {"swap_pairs": swap_pairs[i]})
    rpc_b.send("PLAN_B", {"end_ids": [int(x) for x in b_end_ids],
                          "a_codes": [int(c) for c in a_codes],
                          "attempts": num_pairs, "delivered": n_del,
                          "num_stations": num_stations})

    # Serve each station's batched swap (the BSMs against the shared register).
    # Station order is arbitrary physics-wise (the BSMs commute); serving in
    # index order just keeps the run deterministic.
    for rpc in rpc_stations:
        req = rpc.recv("BSM_REQ")
        heralds = [list(svc.bell_measure(int(q1), int(q2))) for q1, q2 in req["pairs"]]
        rpc.send("BSM_RESP", {"heralds": heralds})

    # Serve Bob's heralded corrections + measurements in one batch.
    req = rpc_b.recv("CORR_MEAS_REQ")
    outcomes = []
    for qid, x, z, code in req["reqs"]:
        if x or z:
            svc.apply_correction(int(qid), int(x), int(z))
        outcomes.append(svc.measure(int(qid), _ANGLE[int(code)]))
    rpc_b.send("CORR_MEAS_RESP", {"outcomes": outcomes})

    # From here on the flow is the E91 one: sample QBER, CHSH, Cascade + PA.
    rec = rpc_b.recv("RECONCILE")
    b_codes = rec["b_codes"]
    sample_idx = rec["sample_idx"]
    chsh_bits = {int(k): v for k, v in rec["chsh_bits"].items()}

    qest = BB84Protocol.qber_from_disclosed(
        [a_bits[k] for k in sample_idx], rec["b_sample_bits"])

    chsh_s = chsh_n = None
    if mode == "e91":
        merged = [b_codes[k] if k in chsh_bits else -1 for k in range(n_del)]
        b_bit_arr = [chsh_bits.get(k) for k in range(n_del)]
        det = [k in chsh_bits for k in range(n_del)]
        chsh_s, chsh_n = chsh_value(a_codes, merged, a_bits, b_bit_arr, det)

    key_pos = _key_positions(a_codes, b_codes, [True] * n_del, key_codes)
    sample_set = set(sample_idx)
    key_only = [k for k in key_pos if k not in sample_set]
    alice_key = bits_to_int([a_bits[k] for k in key_only])
    secure_fraction = BB84Protocol.secure_key_fraction(qest.qber)

    summary = {"qber": qest.qber, "qber_ci": list(qest.confidence_interval),
               "num_sampled": qest.num_sampled, "chsh_s": chsh_s,
               "chsh_pairs": chsh_n, "sifted_bits": len(key_pos),
               "key_bits": len(key_only), "secure_fraction": secure_fraction,
               "final_key_bits": int(len(key_only) * secure_fraction),
               "attempts": num_pairs, "delivered": n_del,
               "swaps": n_del * num_stations,
               "qber_pred": chain_qber(fidelity, num_links),
               "fidelity_pred": chain_fidelity(fidelity, num_links),
               "chsh_pred": chain_chsh(fidelity, num_links)}
    rpc_b.send("SUMMARY", summary)

    reconciled = False
    corrections = bits_leaked = 0
    secure_len = 0
    if do_reconcile and key_only and secure_fraction > 0:   # abort above ~11% QBER
        final, corrections, bits_leaked = serve_parities(
            rpc_b, [a_bits[k] for k in key_only])
        alice_key = bits_to_int(final)
        secure_len = len(final)
        reconciled = True

    finite_info = None
    if fk_eps is not None and reconciled:
        from qne.finite_key import finite_key_length
        fk = finite_key_length(len(key_only), qest.num_sampled, qest.qber,
                               bits_leaked, **fk_eps)
        finite_info = {"secret_bits": fk.secret_bits,
                       "asymptotic_bits": fk.asymptotic_bits,
                       "qber_upper": fk.qber_upper, "mu": fk.mu, **fk_eps}

    return {"key": alice_key, "reconciled": reconciled,
            "corrections": corrections, "bits_leaked": bits_leaked,
            "secure_key_bits": secure_len, "finite_key": finite_info,
            **summary}


def _run_bob(rpc_a, rpc_stations, num_pairs, mode, sample_fraction, seed,
             do_reconcile, cascade_passes, fk_eps, apply_correction):
    spec = _MODES[mode]
    key_codes = set(spec["key"])
    b_rng = np.random.default_rng(seed + 202)

    plan = rpc_a.recv("PLAN_B")
    end_ids = plan["end_ids"]
    a_codes = plan["a_codes"]
    n_del = plan["delivered"]

    # One herald stream per station — the multi-hop classical hops. Order of
    # arrival across stations doesn't matter (each has its own queue).
    herald_streams = []
    for rpc in rpc_stations:
        heralds = rpc.recv("HERALDS")["heralds"]
        if len(heralds) != n_del:
            raise ValueError(f"herald count {len(heralds)} != delivered {n_del}")
        herald_streams.append(heralds)

    # Compose the correction per pair: Paulis compose by XOR of the herald bits.
    b_codes = [int(c) for c in b_rng.choice(np.array(spec["bob"]), size=n_del)]
    reqs = []
    for k in range(n_del):
        x = z = 0
        for stream in herald_streams:
            m1, m2 = stream[k]
            x ^= int(m2)
            z ^= int(m1)
        if not apply_correction:
            x = z = 0
        reqs.append([int(end_ids[k]), x, z, b_codes[k]])
    b_bits = rpc_a.call("CORR_MEAS_REQ", {"reqs": reqs},
                        expected="CORR_MEAS_RESP")["outcomes"]

    key_pos = _key_positions(a_codes, b_codes, [True] * n_del, key_codes)
    n_sample = BB84Protocol.sample_size(len(key_pos), sample_fraction)
    sample_idx = sorted(b_rng.choice(key_pos, size=n_sample, replace=False).tolist()) \
        if n_sample else []

    chsh_bits = {}
    if mode == "e91":
        for k in range(n_del):
            if (int(a_codes[k]), b_codes[k]) in _CHSH["signs"]:
                chsh_bits[k] = b_bits[k]
    rpc_a.send("RECONCILE", {"b_codes": b_codes, "sample_idx": sample_idx,
                             "b_sample_bits": [b_bits[k] for k in sample_idx],
                             "chsh_bits": {str(k): v for k, v in chsh_bits.items()}})

    summary = rpc_a.recv("SUMMARY")
    sample_set = set(sample_idx)
    key_only = [k for k in key_pos if k not in sample_set]
    key_arr = [b_bits[k] for k in key_only]

    reconciled = False
    corrections = bits_leaked = 0
    secure_len = 0
    n_key_in = len(key_arr)
    if do_reconcile and key_only and summary["secure_fraction"] > 0:
        finite = ({"n_sample": summary["num_sampled"], **fk_eps}
                  if fk_eps is not None else None)
        key_arr, corrections, bits_leaked = drive_cascade(
            rpc_a, key_arr, summary["qber"], seed + 303, passes=cascade_passes,
            finite=finite)
        secure_len = len(key_arr)
        reconciled = True

    finite_info = None
    if fk_eps is not None and reconciled:
        from qne.finite_key import finite_key_length
        fk = finite_key_length(n_key_in, summary["num_sampled"], summary["qber"],
                               bits_leaked, **fk_eps)
        finite_info = {"secret_bits": fk.secret_bits,
                       "asymptotic_bits": fk.asymptotic_bits,
                       "qber_upper": fk.qber_upper, "mu": fk.mu, **fk_eps}

    all_heralds = [h for stream in herald_streams for h in stream]
    return {"key": bits_to_int(key_arr), "reconciled": reconciled,
            "corrections": corrections, "bits_leaked": bits_leaked,
            "secure_key_bits": secure_len, "finite_key": finite_info,
            "heralds": _herald_hist(all_heralds),
            "heralds_per_station": [_herald_hist(s) for s in herald_streams],
            **summary}


def _run_repeater(rpc_a, rpc_b):
    """Middle station: swap (batched BSM against the register), herald to Bob.

    Identical for any chain length — a station only knows its own segment.
    """
    plan = rpc_a.recv("PLAN_R")
    heralds = rpc_a.call("BSM_REQ", {"pairs": plan["swap_pairs"]},
                         expected="BSM_RESP")["heralds"]
    rpc_b.send("HERALDS", {"heralds": heralds})
    return {"swaps": len(heralds), "heralds": _herald_hist(heralds)}
