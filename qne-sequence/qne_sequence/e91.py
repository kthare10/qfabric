"""E91 / BBM92 — entanglement-based QKD on the shared quantum-state service.

Both parties measure halves of shared Bell pairs and keep the matching-basis
outcomes as correlated key material (sift + QBER reuse ``qne.bb84.BB84Protocol``,
so the metrics match the BB84 path). E91 additionally measures the cross-basis
combinations to compute the CHSH value S — a Bell-inequality test that certifies
entanglement (S > 2) and, with the Werner model, degrades as S = 2√2·F in lockstep
with QBER = (1−F)/2.

Modes:
  * ``bbm92`` — Z/X bases only; key from matching bases, security from QBER
    (the entanglement-based analogue of BB84; most key-efficient).
  * ``e91`` — Alice ∈ {0, π/4, π/2}, Bob ∈ {π/4, 3π/4, π/2}; key from the two
    shared angles (π/4, π/2), CHSH from the extreme combinations.

This module holds the transport-independent logic: it drives a session against a
*service object* (local now; a RemoteQuantumManager proxy over the wire next), so
the same protocol runs in-process for validation and distributed on FABRIC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from qne.bb84 import AliceRecord, BB84Protocol, BobRecord

# canonical measurement angles (X–Z plane), referenced by integer code
_ANGLE = {0: 0.0, 1: math.pi / 4, 2: math.pi / 2, 3: 3 * math.pi / 4}

# per-mode allowed basis codes for each party, and which codes form the key
_MODES = {
    "bbm92": {"alice": (0, 2), "bob": (0, 2), "key": (0, 2)},
    "e91":   {"alice": (0, 1, 2), "bob": (1, 2, 3), "key": (1, 2)},
}

# CHSH quartet for e91: S = E(a0,b0) − E(a0,b1) + E(a1,b0) + E(a1,b1)
# with a0=0, a1=π/2 (codes 0,2) and b0=π/4, b1=3π/4 (codes 1,3) → S = 2√2 at F=1.
_CHSH = {"a": (0, 2), "b": (1, 3), "signs": {(0, 1): +1, (0, 3): -1, (2, 1): +1, (2, 3): +1}}


@dataclass
class E91Result:
    mode: str
    num_pairs: int
    detected_pairs: int
    sifted_bits: int
    qber: float
    qber_ci: tuple[float, float]
    num_sampled: int
    secure_fraction: float
    final_key_bits: int
    key_bits: int
    chsh_s: float | None
    chsh_pairs: int
    alice_key: int | None
    bob_key: int | None
    extra: dict = field(default_factory=dict)


def _choose_codes(rng: np.random.Generator, codes: tuple[int, ...], n: int) -> np.ndarray:
    return rng.choice(np.array(codes), size=n)


def chsh_value(alice_codes, bob_codes, alice_bits, bob_bits, detected) -> tuple[float | None, int]:
    """CHSH S from the cross-basis outcomes (e91 only). Returns (S, n_used)."""
    corr: dict[tuple[int, int], list[int]] = {combo: [] for combo in _CHSH["signs"]}
    for i, ok in enumerate(detected):
        if not ok:
            continue
        combo = (int(alice_codes[i]), int(bob_codes[i]))
        if combo in corr:
            corr[combo].append(1 if alice_bits[i] == bob_bits[i] else -1)
    n_used = sum(len(v) for v in corr.values())
    if any(len(v) == 0 for v in corr.values()):
        return None, n_used
    s = 0.0
    for combo, sign in _CHSH["signs"].items():
        e = float(np.mean(corr[combo]))   # E = P(equal) − P(differ)
        s += sign * e
    return abs(s), n_used


def run_session(service, num_pairs: int, *, fidelity: float = 1.0,
                loss_probability: float = 0.0, mode: str = "e91",
                sample_fraction: float = 0.1, alice_seed: int = 1,
                bob_seed: int = 2) -> E91Result:
    """Run one E91/BBM92 session against ``service`` and return the result.

    ``service`` must expose ``create_pairs`` and ``measure`` (QuantumStateService
    locally, or a RemoteQuantumManager proxy). Alice measures her halves through
    the service too, so a single authority owns every collapse.
    """
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r} (use 'bbm92' or 'e91')")
    spec = _MODES[mode]
    a_rng = np.random.default_rng(alice_seed)
    b_rng = np.random.default_rng(bob_seed)

    pairs = service.create_pairs(num_pairs, fidelity=fidelity,
                                 loss_probability=loss_probability)
    a_ids, b_ids, surviving = pairs["a_ids"], pairs["b_ids"], pairs["surviving"]

    a_codes = _choose_codes(a_rng, spec["alice"], num_pairs)
    b_codes = _choose_codes(b_rng, spec["bob"], num_pairs)

    # Alice measures all her halves; Bob measures only the halves that survived
    # transport (lost pairs = no detection, like a lost BB84 photon).
    a_bits = [service.measure(a_ids[i], _ANGLE[int(a_codes[i])]) for i in range(num_pairs)]
    b_bits = [None] * num_pairs
    for i in range(num_pairs):
        if surviving[i]:
            b_bits[i] = service.measure(b_ids[i], _ANGLE[int(b_codes[i])])

    # CHSH test (e91) uses the cross-basis, detected pairs
    chsh_s, chsh_n = (None, 0)
    if mode == "e91":
        chsh_s, chsh_n = chsh_value(a_codes, b_codes, a_bits, b_bits, surviving)

    # Key sift: reuse BB84Protocol — "basis" is the measurement-angle code, so it
    # keeps only detected, matching-angle pairs that are in the key set.
    key_codes = set(spec["key"])
    alice_log = [AliceRecord(i, int(a_codes[i]), int(a_bits[i]))
                 for i in range(num_pairs) if int(a_codes[i]) in key_codes]
    bob_log = [BobRecord(i, int(b_codes[i]), int(b_bits[i]))
               for i in range(num_pairs)
               if surviving[i] and int(b_codes[i]) in key_codes]

    proto = BB84Protocol(sample_fraction=sample_fraction, seed=bob_seed + 11)
    sifted = proto.sift(alice_log, bob_log)
    qest = proto.estimate_qber(sifted)
    krate = proto.compute_key_rate(sifted, qest, num_pairs)

    # form the shared keys from the un-sampled sifted bits (sample bits disclosed)
    n_sample = qest.num_sampled
    a_key_bits = sifted.alice_bits[n_sample:]
    b_key_bits = sifted.bob_bits[n_sample:]
    def to_int(bits):
        return int("".join(str(b) for b in bits), 2) if bits else None

    return E91Result(
        mode=mode,
        num_pairs=num_pairs,
        detected_pairs=sum(surviving),
        sifted_bits=sifted.sifted_count,
        qber=qest.qber,
        qber_ci=qest.confidence_interval,
        num_sampled=n_sample,
        secure_fraction=BB84Protocol.secure_key_fraction(qest.qber),
        final_key_bits=krate.final_key_bits,
        key_bits=len(a_key_bits),
        chsh_s=chsh_s,
        chsh_pairs=chsh_n,
        alice_key=to_int(a_key_bits),
        bob_key=to_int(b_key_bits),
    )
