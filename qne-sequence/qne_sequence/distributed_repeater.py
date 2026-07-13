"""Distributed repeater chain — entanglement swapping across THREE processes.

This distributes the heralding of ``repeater.py`` (the in-process chain) over real
links, completing the "prove in-process, then distribute" path used for E91:

  * **alice** (role 0) hosts the QuantumStateService — the register authority —
    and generates both elementary link pairs per attempt: (a1,b1) for link A–R
    and (a2,b2) for link R–B. She measures her end (a1) locally and serves the
    other parties' quantum ops over the wire.
  * **repeater** (role 2) holds the two middle halves (b1, a2). It performs the
    Bell-state measurements as a batched RPC against the register (BSM_REQ), then
    forwards the (m1, m2) herald bits to Bob over its OWN link — the classical
    herald traffic whose latency is the multi-hop research lever.
  * **bob** (role 1) receives the heralds, applies the Pauli correction
    X^m2·Z^m1 to his half (b2) via RPC, measures it, then runs the standard
    sift / QBER-sample / CHSH disclosure and Cascade+PA against Alice.

Three links (Bob listens for Alice and the repeater; the repeater listens for
Alice): A↔R carries the swap plan + BSM ops; R↔B carries only heralds; A↔B
carries the end-to-end QKD classical protocol. Only public data crosses any
link: qubit ids, basis codes, heralds, the QBER sample, and Cascade parities.

Physics note: Alice measures her end *before* the swaps happen — delayed-choice
entanglement swapping — so the end-to-end statistics still follow the Werner
chain law F = (1 + 3·f^L)/4 (validated by the three-process tests). Skipping the
correction (``--no-correction``) collapses QBER to 1/2: the herald link is
load-bearing.
"""

from __future__ import annotations

import numpy as np

from qne.bb84 import BB84Protocol

from .distributed_e91 import _key_positions
from .e91 import _ANGLE, _CHSH, _MODES, chsh_value
from .listener import Link
from .quantum_state_service import QuantumStateService
from .reconcile_link import bits_to_int, drive_cascade, serve_parities
from .remote_qm import RpcChannel
from .repeater import chain_chsh, chain_fidelity, chain_qber

ROLES = {"alice": 0, "bob": 1, "repeater": 2}
_NUM_LINKS = 2                      # 3 nodes -> 2 elementary links


def run_repeater_node(role: int, name: str, host: str, *, port_ab: int,
                      port_ar: int, port_rb: int, num_pairs: int = 5000,
                      fidelity: float = 0.95, loss_probability: float = 0.0,
                      mode: str = "bbm92", sample_fraction: float = 0.1,
                      seed: int = 0, do_reconcile: bool = True,
                      cascade_passes: int = 4, finite_key: bool = False,
                      eps_sec: float = 1e-9, eps_cor: float = 1e-15,
                      auth_key: str | None = None,
                      apply_correction: bool = True,
                      bob_host: str | None = None,
                      repeater_host: str | None = None) -> dict:
    """Run one node of the 3-process repeater chain; return its result dict.

    Start order (like the 2-node runners, listeners first): bob, repeater, alice
    — though ``Link.connect`` retries make the order forgiving. ``mode`` is
    'bbm92' (Z/X key) or 'e91' (adds the CHSH test across the swapped chain).
    ``loss_probability`` applies per LINK; an attempt whose links don't both
    survive is never generated (heralded-generation retry, as in repeater.py).

    ``host`` is this node's LISTEN address (loopback locally, 0.0.0.0 on a
    slice). ``bob_host`` / ``repeater_host`` are where the OTHER parties are
    reached — they default to ``host``, which is right on loopback where all
    three share one address, and are set explicitly on FABRIC where each link
    terminates on a different node.
    """
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r} (use 'bbm92' or 'e91')")
    bob_host = bob_host or host
    repeater_host = repeater_host or host
    fk_eps = {"eps_sec": eps_sec, "eps_cor": eps_cor} if finite_key else None
    links: list[Link] = []
    try:
        if role == ROLES["bob"]:
            ab = Link(auth_key=auth_key)
            ab.serve(host, port_ab)
            rb = Link(auth_key=auth_key)
            rb.serve(host, port_rb)
            links = [ab, rb]
            rpc_a, rpc_r = RpcChannel(ab), RpcChannel(rb)
            ab.start_rx()
            rb.start_rx()
            result = _run_bob(rpc_a, rpc_r, num_pairs, mode, sample_fraction,
                              seed, do_reconcile, cascade_passes, fk_eps,
                              apply_correction)
        elif role == ROLES["repeater"]:
            ar = Link(auth_key=auth_key)
            ar.serve(host, port_ar)
            rb = Link(auth_key=auth_key)
            rb.connect(bob_host, port_rb)
            links = [ar, rb]
            rpc_a, rpc_b = RpcChannel(ar), RpcChannel(rb)
            ar.start_rx()
            rb.start_rx()
            result = _run_repeater(rpc_a, rpc_b)
        else:                                       # alice — register authority
            ar = Link(auth_key=auth_key)
            ar.connect(repeater_host, port_ar)
            ab = Link(auth_key=auth_key)
            ab.connect(bob_host, port_ab)
            links = [ar, ab]
            rpc_r, rpc_b = RpcChannel(ar), RpcChannel(ab)
            ar.start_rx()
            ab.start_rx()
            result = _run_alice(rpc_r, rpc_b, num_pairs, fidelity,
                                loss_probability, mode, sample_fraction, seed,
                                do_reconcile, fk_eps)
    finally:
        for link in links:
            link.close()

    result.update({
        "role": role, "name": name, "mode": mode, "num_nodes": 3,
        "num_links": _NUM_LINKS, "corrected": apply_correction,
        "quantum_transport": "entangled-state-service",
        "tx_frames": sum(lk.tx_count for lk in links),
        "rx_frames": sum(lk.rx_count for lk in links),
        "authenticated": auth_key is not None,
        "auth_failures": sum(lk.auth_failures for lk in links),
    })
    return result


def _herald_hist(heralds) -> dict:
    hist: dict[str, int] = {}
    for m1, m2 in heralds:
        key = f"{m1}{m2}"
        hist[key] = hist.get(key, 0) + 1
    return hist


def _run_alice(rpc_r, rpc_b, num_pairs, fidelity, loss_probability, mode,
               sample_fraction, seed, do_reconcile, fk_eps):
    spec = _MODES[mode]
    key_codes = set(spec["key"])
    svc = QuantumStateService(seed=seed)
    loss_rng = np.random.default_rng(seed + 77)
    a_rng = np.random.default_rng(seed + 101)

    # Generate both link pairs per surviving attempt (per-LINK loss, as in
    # repeater.py: a failed link heralds "no pair", the attempt just retries).
    swap_pairs: list[list[int]] = []       # (b1, a2) — the repeater's halves
    a_end_ids: list[int] = []              # a1 — Alice's end qubit
    b_end_ids: list[int] = []              # b2 — Bob's end qubit
    for _ in range(num_pairs):
        if loss_probability > 0.0 and any(
                loss_rng.random() < loss_probability for _ in range(_NUM_LINKS)):
            continue
        a1, b1 = svc.register.create_bell_pair(fidelity)
        a2, b2 = svc.register.create_bell_pair(fidelity)
        swap_pairs.append([int(b1), int(a2)])
        a_end_ids.append(a1)
        b_end_ids.append(b2)
    n_del = len(a_end_ids)

    # Alice measures her end first (delayed-choice: order doesn't change the
    # heralded statistics) and announces her codes — public after measurement.
    a_codes = a_rng.choice(np.array(spec["alice"]), size=n_del)
    a_bits = [svc.measure(a_end_ids[k], _ANGLE[int(a_codes[k])])
              for k in range(n_del)]

    rpc_r.send("PLAN_R", {"swap_pairs": swap_pairs})
    rpc_b.send("PLAN_B", {"end_ids": [int(x) for x in b_end_ids],
                          "a_codes": [int(c) for c in a_codes],
                          "attempts": num_pairs, "delivered": n_del})

    # Serve the repeater's batched swap (the BSMs against the shared register).
    req = rpc_r.recv("BSM_REQ")
    heralds = [list(svc.bell_measure(int(q1), int(q2))) for q1, q2 in req["pairs"]]
    rpc_r.send("BSM_RESP", {"heralds": heralds})

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
               "attempts": num_pairs, "delivered": n_del, "swaps": n_del,
               "qber_pred": chain_qber(fidelity, _NUM_LINKS),
               "fidelity_pred": chain_fidelity(fidelity, _NUM_LINKS),
               "chsh_pred": chain_chsh(fidelity, _NUM_LINKS)}
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


def _run_bob(rpc_a, rpc_r, num_pairs, mode, sample_fraction, seed,
             do_reconcile, cascade_passes, fk_eps, apply_correction):
    spec = _MODES[mode]
    key_codes = set(spec["key"])
    b_rng = np.random.default_rng(seed + 202)

    plan = rpc_a.recv("PLAN_B")
    end_ids = plan["end_ids"]
    a_codes = plan["a_codes"]
    n_del = plan["delivered"]

    # The heralds arrive over the REPEATER's link — the multi-hop classical hop.
    heralds = rpc_r.recv("HERALDS")["heralds"]
    if len(heralds) != n_del:
        raise ValueError(f"herald count {len(heralds)} != delivered {n_del}")

    # Correct X^m2·Z^m1 (restoring phi+) and measure, batched through the service.
    b_codes = [int(c) for c in b_rng.choice(np.array(spec["bob"]), size=n_del)]
    reqs = []
    for k in range(n_del):
        m1, m2 = heralds[k]
        x, z = (int(m2), int(m1)) if apply_correction else (0, 0)
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

    return {"key": bits_to_int(key_arr), "reconciled": reconciled,
            "corrections": corrections, "bits_leaked": bits_leaked,
            "secure_key_bits": secure_len, "finite_key": finite_info,
            "heralds": _herald_hist(heralds),
            **summary}


def _run_repeater(rpc_a, rpc_b):
    """Middle node: swap (batched BSM against the register), herald to Bob."""
    plan = rpc_a.recv("PLAN_R")
    heralds = rpc_a.call("BSM_REQ", {"pairs": plan["swap_pairs"]},
                         expected="BSM_RESP")["heralds"]
    rpc_b.send("HERALDS", {"heralds": heralds})
    return {"swaps": len(heralds), "heralds": _herald_hist(heralds)}
