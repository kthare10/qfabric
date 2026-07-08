"""Distributed E91 / BBM92 over a real link (Phase E2).

Two processes run entanglement-based QKD across a TCP link:

  * Alice (role 0) hosts the QuantumStateService (the entangled register) and
    generates the Bell pairs. She measures her own halves locally and answers
    Bob's measurement RPCs.
  * Bob (role 1) has no local quantum state; he picks his bases independently and
    measures his halves through a RemoteQuantumManager (MEASURE_REQ/RESP over the
    Link). All classical coordination — basis announcement, QBER sample disclosure,
    CHSH bit disclosure — rides the real WAN, which is the research lever.

Only public data crosses the wire: Alice announces her bases; Bob discloses the
QBER *sample* and the CHSH-basis bits. Each side derives the key from its own bits
at the shared un-sampled key positions, so the raw key never transits the link
(parity with the BB84 emulator's sample-disclosure model).
"""

from __future__ import annotations

import numpy as np

from qne.bb84 import BB84Protocol
from .quantum_state_service import QuantumStateService
from .remote_qm import RpcChannel, RemoteQuantumManager
from .reconcile_link import bits_to_int, drive_cascade, serve_parities
from .e91 import _ANGLE, _MODES, _CHSH, chsh_value
from .listener import Link


def _key_positions(a_codes, b_codes, surviving, key_codes):
    """Detected pairs measured in the same key basis on both sides."""
    return [i for i in range(len(a_codes))
            if surviving[i] and int(a_codes[i]) == int(b_codes[i])
            and int(a_codes[i]) in key_codes]


def run_e91_node(role: int, name: str, peer: str, host: str, port: int, *,
                 num_pairs: int = 20000, fidelity: float = 0.98,
                 loss_probability: float = 0.0, mode: str = "e91",
                 sample_fraction: float = 0.1, seed: int = 0,
                 do_reconcile: bool = True, cascade_passes: int = 4) -> dict:
    """Run one side of a distributed E91/BBM92 session; return the result dict."""
    spec = _MODES[mode]
    key_codes = set(spec["key"])
    link = Link()
    if role == 1:                       # Bob listens; Alice connects (as in BB84)
        link.serve(host, port)
    else:
        link.connect(host, port)
    rpc = RpcChannel(link)
    link.start_rx()

    try:
        if role == 0:
            result = _run_alice(rpc, spec, key_codes, num_pairs, fidelity,
                                loss_probability, mode, sample_fraction, seed,
                                do_reconcile)
        else:
            result = _run_bob(rpc, spec, key_codes, num_pairs, mode,
                              sample_fraction, seed, do_reconcile, cascade_passes)
    finally:
        link.close()

    result.update({"role": role, "name": name, "mode": mode,
                   "quantum_transport": "entangled-state-service",
                   "tx_frames": link.tx_count, "rx_frames": link.rx_count})
    return result


def _run_alice(rpc, spec, key_codes, num_pairs, fidelity, loss_probability,
               mode, sample_fraction, seed, do_reconcile):
    svc = QuantumStateService(seed=seed)
    a_rng = np.random.default_rng(seed + 101)
    pairs = svc.create_pairs(num_pairs, fidelity=fidelity,
                             loss_probability=loss_probability)
    a_ids, b_ids, surviving = pairs["a_ids"], pairs["b_ids"], pairs["surviving"]

    a_codes = a_rng.choice(np.array(spec["alice"]), size=num_pairs)
    a_bits = [svc.measure(a_ids[i], _ANGLE[int(a_codes[i])]) for i in range(num_pairs)]

    # announce bases + which pairs to measure (public info)
    rpc.send("PLAN", {"surviving": [bool(s) for s in surviving],
                      "b_ids": [int(x) for x in b_ids],
                      "a_codes": [int(c) for c in a_codes]})

    # answer Bob's measurement RPC against the shared register
    req = rpc.recv("MEASURE_REQ")
    outcomes = [svc.measure(bid, _ANGLE[code]) for bid, code in req["reqs"]]
    rpc.send("MEASURE_RESP", {"outcomes": outcomes})

    rec = rpc.recv("RECONCILE")
    b_codes = rec["b_codes"]                      # len num_pairs, -1 where not measured
    sample_idx = rec["sample_idx"]
    b_sample_bits = rec["b_sample_bits"]
    chsh_bits = {int(k): v for k, v in rec["chsh_bits"].items()}

    # QBER from the disclosed sample (Alice has her own bits at those positions)
    qest = BB84Protocol.qber_from_disclosed(
        [a_bits[i] for i in sample_idx], b_sample_bits)

    # CHSH from the cross-basis disclosed bits (e91 only)
    chsh_s = chsh_n = None
    if mode == "e91":
        merged = [b_codes[i] if i in chsh_bits else -1 for i in range(num_pairs)]
        b_bit_arr = [chsh_bits.get(i) for i in range(num_pairs)]
        det = [i in chsh_bits for i in range(num_pairs)]
        chsh_s, chsh_n = chsh_value(a_codes, merged, a_bits, b_bit_arr, det)

    key_pos = _key_positions(a_codes, b_codes, surviving, key_codes)
    sample_set = set(sample_idx)
    key_only = [i for i in key_pos if i not in sample_set]
    alice_key = bits_to_int([a_bits[i] for i in key_only])
    secure_fraction = BB84Protocol.secure_key_fraction(qest.qber)

    summary = {"qber": qest.qber, "qber_ci": list(qest.confidence_interval),
               "num_sampled": qest.num_sampled, "chsh_s": chsh_s,
               "chsh_pairs": chsh_n, "sifted_bits": len(key_pos),
               "key_bits": len(key_only), "secure_fraction": secure_fraction,
               "final_key_bits": int(len(key_only) * secure_fraction)}
    rpc.send("SUMMARY", summary)

    # Cascade reconciliation: Bob drives, Alice answers parity queries over her
    # key bits (in the shared key_only order) until Bob signals done.
    reconciled = False
    corrections = bits_leaked = 0
    secure_len = 0
    if do_reconcile and key_only and secure_fraction > 0:   # abort above ~11% QBER
        final, corrections, bits_leaked = serve_parities(
            rpc, [a_bits[i] for i in key_only])
        alice_key = bits_to_int(final)                       # extracted secret key
        secure_len = len(final)
        reconciled = True

    return {"key": alice_key, "detected_pairs": int(sum(surviving)),
            "num_pairs": num_pairs, "reconciled": reconciled,
            "corrections": corrections, "bits_leaked": bits_leaked,
            "secure_key_bits": secure_len,
            **summary}


def _run_bob(rpc, spec, key_codes, num_pairs, mode, sample_fraction, seed,
             do_reconcile, cascade_passes):
    qm = RemoteQuantumManager(rpc)
    b_rng = np.random.default_rng(seed + 202)

    plan = rpc.recv("PLAN")
    surviving = plan["surviving"]
    b_ids = plan["b_ids"]
    a_codes = plan["a_codes"]

    b_codes = [-1] * num_pairs
    reqs = []
    for i in range(num_pairs):
        if surviving[i]:
            b_codes[i] = int(b_rng.choice(np.array(spec["bob"])))
            reqs.append((b_ids[i], b_codes[i]))
    outcomes = qm.measure_batch(reqs)
    b_bits = [None] * num_pairs
    k = 0
    for i in range(num_pairs):
        if surviving[i]:
            b_bits[i] = outcomes[k]
            k += 1

    # key positions both sides can compute (Alice announced a_codes)
    key_pos = _key_positions(a_codes, b_codes, surviving, key_codes)
    n_sample = BB84Protocol.sample_size(len(key_pos), sample_fraction)
    sample_idx = sorted(b_rng.choice(key_pos, size=n_sample, replace=False).tolist()) \
        if n_sample else []

    # disclose: QBER sample bits + CHSH-basis bits (public); keep the rest as key
    chsh_bits = {}
    if mode == "e91":
        for i in range(num_pairs):
            if surviving[i] and (int(a_codes[i]), b_codes[i]) in _CHSH["signs"]:
                chsh_bits[i] = b_bits[i]
    rpc.send("RECONCILE", {"b_codes": b_codes, "sample_idx": sample_idx,
                           "b_sample_bits": [b_bits[i] for i in sample_idx],
                           "chsh_bits": {str(k): v for k, v in chsh_bits.items()}})

    summary = rpc.recv("SUMMARY")
    sample_set = set(sample_idx)
    key_only = [i for i in key_pos if i not in sample_set]
    key_arr = [b_bits[i] for i in key_only]

    # Cascade reconciliation + privacy amplification over the wire.
    reconciled = False
    corrections = bits_leaked = 0
    secure_len = 0
    if do_reconcile and key_only and summary["secure_fraction"] > 0:  # abort above ~11% QBER
        key_arr, corrections, bits_leaked = drive_cascade(
            rpc, key_arr, summary["qber"], seed + 303, passes=cascade_passes)
        secure_len = len(key_arr)
        reconciled = True

    bob_key = bits_to_int(key_arr)
    return {"key": bob_key, "detected_pairs": int(sum(surviving)),
            "num_pairs": num_pairs, "reconciled": reconciled,
            "corrections": corrections, "bits_leaked": bits_leaked,
            "secure_key_bits": secure_len,
            **summary}
