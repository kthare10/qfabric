"""QuantumStateService — the authority that owns the shared entangled register.

One process runs this; both parties issue create/measure ops against it (locally,
or over the wire via RemoteQuantumManager). It wraps a QStateRegister and adds the
network-facing concerns the protocol needs:

  * pair *loss* — a pair whose transported half is dropped in the fiber/P4 channel
    is never established for the receiver (reuses the BB84 loss model, so sifted
    counts stay comparable across BB84 and E91).
  * batched measurement — a peer measures all its halves in one request (one round
    trip), mirroring bulk photon mode.

Designed for multi-hop: the register is n-qubit and keyed by id, so a repeater
station later swaps two qubits (Bell measurement) inside the same service.
"""

from __future__ import annotations

import numpy as np

from . import dqc
from .qstate_core import QStateRegister

_PLUS = np.array([1, 1], dtype=complex) / np.sqrt(2.0)


class QuantumStateService:
    """Owns the entangled register; serves create/measure to both parties."""

    def __init__(self, seed: int = 0):
        self.register = QStateRegister(seed=seed)
        # deterministic measurement-sample stream, independent of the register's
        # own Werner sampling, so outcomes are reproducible from the service seed
        self._samp_rng = np.random.default_rng(seed + 7919)

    def create_pairs(self, num_pairs: int, fidelity: float = 1.0,
                     loss_probability: float = 0.0) -> dict:
        """Create ``num_pairs`` Bell pairs; return the qubit ids for each side.

        Returns {"a_ids", "b_ids", "surviving"} where surviving[i] is False if the
        i-th pair's transported (B) half was lost — that pair yields no detection
        for the receiver, exactly like a lost BB84 photon.
        """
        a_ids: list[int] = []
        b_ids: list[int] = []
        surviving: list[bool] = []
        loss_rng = self.register._rng   # share the register's RNG stream
        for _ in range(num_pairs):
            a, b = self.register.create_bell_pair(fidelity)
            a_ids.append(a)
            b_ids.append(b)
            surviving.append(not (loss_probability > 0.0
                                  and loss_rng.random() < loss_probability))
        return {"a_ids": a_ids, "b_ids": b_ids, "surviving": surviving}

    def measure(self, qubit_id: int, angle: float) -> int:
        """Measure one qubit at ``angle`` (X–Z plane); collapse & return the bit."""
        return self.register.measure(qubit_id, angle,
                                     samp=float(self._samp_rng.random()))

    def measure_batch(self, requests: list[tuple[int, float]]) -> list[int]:
        """Measure many (qubit_id, angle) pairs in order; return the outcomes."""
        return [self.measure(qid, ang) for qid, ang in requests]

    def bell_measure(self, q1: int, q2: int) -> tuple[int, int]:
        """Entanglement swap: BSM on two qubits; returns the (m1, m2) herald bits.

        A repeater station holding one half of each adjacent link calls this; the
        herald travels classically to an end node, which applies the correction.
        """
        return self.register.bell_measure(q1, q2,
                                          samp1=float(self._samp_rng.random()),
                                          samp2=float(self._samp_rng.random()))

    def apply_correction(self, qubit_id: int, x: int, z: int) -> None:
        """Apply the heralded Pauli correction X^x·Z^z (restores Φ+ after swaps)."""
        self.register.apply_pauli(qubit_id, x=x, z=z)

    # -- distributed computing (ROADMAP Phase 6) --------------------------------

    def alloc_batch(self, count: int, state: str = "zero") -> list[int]:
        """Allocate ``count`` fresh data qubits in |0> or |+> and return their ids.

        A node's *local* qubits still live in the shared register — the centralized
        quantum-manager model the entanglement protocols already use (ASSUMPTIONS).
        """
        amp = None if state == "zero" else _PLUS
        if state not in ("zero", "plus"):
            raise ValueError(f"unknown data state {state!r} (expected 'zero'/'plus')")
        return [self.register.alloc(amp) for _ in range(count)]

    def telegate_apply_batch(self, requests: list[tuple[int, int, int]]) -> list[int]:
        """Serve the target node's half of a non-local CNOT, batched.

        ``requests`` are (epr_half, target_id, m1) — the peer's own qubits and the
        correction bit **as it arrived over the link**. Returns the m2 bits the peer
        owes back to the control node.
        """
        return [dqc.telegate_cnot_apply(self.register, epr, target, m1)
                for epr, target, m1 in requests]

    def teleport_recv_batch(self, requests: list[tuple[int, int, int]]) -> None:
        """Apply X^m2.Z^m1 to each received half — the receiver's half of a teleport."""
        for epr, m1, m2 in requests:
            dqc.teleport_recv(self.register, epr, m1, m2)

    def discard_pair(self, a_id: int, b_id: int) -> None:
        """Retire a pair whose transported half was lost in the channel.

        Measured out rather than deleted, so no half-consumed group is ever left
        behind for a later gate to entangle itself with.
        """
        self.measure(a_id, 0.0)
        self.measure(b_id, 0.0)

    def drop(self, qubit_id: int) -> None:
        """Discard a lost qubit's state so its group can be garbage-collected."""
        g = self.register._groups.pop(qubit_id, None)
        if g is not None and len(g.ids) == 1:
            pass  # single-qubit group fully removed
