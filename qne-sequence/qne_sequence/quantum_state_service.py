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

from .qstate_core import QStateRegister


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

    def drop(self, qubit_id: int) -> None:
        """Discard a lost qubit's state so its group can be garbage-collected."""
        g = self.register._groups.pop(qubit_id, None)
        if g is not None and len(g.ids) == 1:
            pass  # single-qubit group fully removed
