"""Repeater chains via entanglement swapping — local in-process (ROADMAP Phase 4).

A chain of ``n`` nodes is linked by ``L = n−1`` elementary Bell pairs, all held in
one QuantumStateService register: node 1 holds a₁; node i (1<i<n) holds b_{i−1} and
a_i; node n holds b_L. Each intermediate node performs a Bell-state measurement
(``bell_measure``) on its two halves and *heralds* the (m1, m2) outcome classically
toward the end node, which applies the accumulated Pauli correction X^x·Z^z
(Paulis compose by XOR of the herald bits, so one correction at the end is exactly
equivalent to correcting after every swap). What survives is a single entangled
pair between node 1 and node n.

Validation target — the Werner-chain law. ``create_bell_pair(f)`` emits
ρ = f·|Φ+⟩⟨Φ+| + (1−f)·I/4, i.e. the knob ``f`` is the *Werner parameter* w.
Swapping composes Werner states multiplicatively (each nontrivial Bell-group
character has bias f), so a chain of L links yields w_chain = f^L and

    F_chain = (1 + 3·f^L) / 4        QBER = (1 − f^L) / 2        S = 2√2·f^L

In terms of the true per-link fidelity F = (1+3f)/4 ⇔ f = (4F−1)/3 this is the
standard F_chain = (1 + 3·((4F−1)/3)^L)/4 (L = n−1 links for n nodes). The point
of this module is that the *circuit* (BSM + heralded correction on sampled Werner
pairs) reproduces that law — nothing here hard-codes it.

This is the "prove in-process" half of the E91 playbook; distributing the heralds
over the WAN is the follow-on step.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass, field

import numpy as np

from qne.bb84 import BB84Protocol

from .e91 import _ANGLE, chsh_value
from .quantum_state_service import QuantumStateService

# measurement-angle codes shared with e91.py: 0=Z, 1=π/4, 2=X(π/2), 3=3π/4
_BBM92_CODES = (0, 2)
_CHSH_A, _CHSH_B = (0, 2), (1, 3)


def chain_werner(fidelity: float, num_links: int) -> float:
    """End-to-end Werner parameter of a swapped chain: w = f^L."""
    return float(np.clip(fidelity, 0.0, 1.0)) ** num_links


def chain_fidelity(fidelity: float, num_links: int) -> float:
    """Predicted end-to-end fidelity F_chain = (1 + 3·f^L)/4."""
    return (1.0 + 3.0 * chain_werner(fidelity, num_links)) / 4.0


def chain_qber(fidelity: float, num_links: int) -> float:
    """Predicted matching-basis QBER of the end-to-end pair: (1 − f^L)/2."""
    return (1.0 - chain_werner(fidelity, num_links)) / 2.0


def chain_chsh(fidelity: float, num_links: int) -> float:
    """Predicted CHSH S of the end-to-end pair: 2√2·f^L."""
    return 2.0 * math.sqrt(2.0) * chain_werner(fidelity, num_links)


@dataclass
class ChainResult:
    """Outcome of one repeater-chain session (all *_pred fields are the law)."""
    num_nodes: int
    num_links: int
    mode: str
    attempts: int
    delivered: int
    swaps: int
    corrected: bool
    qber: float | None
    qber_ci: tuple[float, float] | None
    qber_pred: float
    matched_pairs: int
    fidelity_est: float | None
    fidelity_pred: float
    chsh_s: float | None
    chsh_pairs: int
    chsh_pred: float
    heralds: dict = field(default_factory=dict)   # "m1m2" -> count over all swaps


def run_chain_session(num_nodes: int, num_pairs: int, *, fidelity: float = 1.0,
                      loss_probability: float = 0.0, mode: str = "bbm92",
                      apply_correction: bool = True, seed: int = 0) -> ChainResult:
    """Distribute ``num_pairs`` end-to-end pairs over an ``num_nodes`` chain.

    Per attempt: create the L link pairs (each Werner with parameter ``fidelity``),
    swap at every intermediate node, forward the heralds, correct at the end node,
    then measure both ends. ``loss_probability`` applies per *link*; an attempt
    whose links don't all survive delivers nothing (heralded generation would
    retry — here we just count it).

    Modes:
      * ``bbm92`` — both ends measure in random Z/X; matching-basis outcomes give
        QBER and the fidelity estimate F̂ = (1 + 3·(1−2·QBER))/4. All outcomes are
        disclosed: this is a physics-validation session, not key production.
      * ``chsh``  — ends measure at the E91 CHSH angles; reports S.

    ``apply_correction=False`` skips the heralded Pauli fix-up: the end pair is
    then an even mixture over the four Bell states (QBER → 0.5, S → 0), which is
    the control showing the classical herald channel is load-bearing.
    """
    if num_nodes < 2:
        raise ValueError("a chain needs at least 2 nodes")
    if mode not in ("bbm92", "chsh"):
        raise ValueError(f"unknown mode {mode!r} (use 'bbm92' or 'chsh')")
    num_links = num_nodes - 1

    svc = QuantumStateService(seed=seed)
    a_rng = np.random.default_rng(seed + 101)
    b_rng = np.random.default_rng(seed + 202)
    loss_rng = np.random.default_rng(seed + 77)

    delivered = 0
    swaps = 0
    heralds: dict[str, int] = {}
    a_codes: list[int] = []
    b_codes: list[int] = []
    a_bits: list[int] = []
    b_bits: list[int] = []

    for _ in range(num_pairs):
        if loss_probability > 0.0 and any(
                loss_rng.random() < loss_probability for _ in range(num_links)):
            continue                      # a link failed; no end-to-end pair
        pairs = [svc.register.create_bell_pair(fidelity) for _ in range(num_links)]
        x = z = 0
        for i in range(num_links - 1):    # node i+2 swaps b_i with a_{i+1}
            m1, m2 = svc.bell_measure(pairs[i][1], pairs[i + 1][0])
            z ^= m1
            x ^= m2
            swaps += 1
            key = f"{m1}{m2}"
            heralds[key] = heralds.get(key, 0) + 1
        if apply_correction and (x or z):
            svc.apply_correction(pairs[-1][1], x, z)

        end_a, end_b = pairs[0][0], pairs[-1][1]
        if mode == "bbm92":
            ca = int(a_rng.choice(_BBM92_CODES))
            cb = int(b_rng.choice(_BBM92_CODES))
        else:
            ca = int(a_rng.choice(_CHSH_A))
            cb = int(b_rng.choice(_CHSH_B))
        a_codes.append(ca)
        b_codes.append(cb)
        a_bits.append(svc.measure(end_a, _ANGLE[ca]))
        b_bits.append(svc.measure(end_b, _ANGLE[cb]))
        delivered += 1

    qber = fid_est = None
    qber_ci = None
    matched = 0
    chsh_s, chsh_n = None, 0
    if mode == "bbm92":
        pos = [i for i in range(delivered) if a_codes[i] == b_codes[i]]
        matched = len(pos)
        est = BB84Protocol.qber_from_disclosed(
            [a_bits[i] for i in pos], [b_bits[i] for i in pos])
        qber = est.qber
        qber_ci = est.confidence_interval
        # invert QBER = (1 − w)/2 → ŵ, then F̂ = (1 + 3ŵ)/4
        fid_est = (1.0 + 3.0 * (1.0 - 2.0 * est.qber)) / 4.0 if matched else None
    else:
        chsh_s, chsh_n = chsh_value(a_codes, b_codes, a_bits, b_bits,
                                    [True] * delivered)

    return ChainResult(
        num_nodes=num_nodes,
        num_links=num_links,
        mode=mode,
        attempts=num_pairs,
        delivered=delivered,
        swaps=swaps,
        corrected=apply_correction,
        qber=qber,
        qber_ci=qber_ci,
        qber_pred=chain_qber(fidelity, num_links),
        matched_pairs=matched,
        fidelity_est=fid_est,
        fidelity_pred=chain_fidelity(fidelity, num_links),
        chsh_s=chsh_s,
        chsh_pairs=chsh_n,
        chsh_pred=chain_chsh(fidelity, num_links),
        heralds=heralds,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Local repeater chain: swap Werner pairs end-to-end and "
                    "compare against the Werner-chain law.")
    ap.add_argument("--nodes", type=int, default=3)
    ap.add_argument("--pairs", type=int, default=5000)
    ap.add_argument("--fidelity", type=float, default=0.95,
                    help="per-link Werner parameter f (register knob)")
    ap.add_argument("--loss", type=float, default=0.0, help="per-link loss probability")
    ap.add_argument("--mode", choices=["bbm92", "chsh"], default="bbm92")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-correction", dest="correction", action="store_false",
                    help="skip the heralded Pauli correction (control run)")
    args = ap.parse_args(argv)

    result = run_chain_session(args.nodes, args.pairs, fidelity=args.fidelity,
                               loss_probability=args.loss, mode=args.mode,
                               apply_correction=args.correction, seed=args.seed)
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
