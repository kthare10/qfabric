"""Multi-qubit state register — the shared quantum authority for entanglement.

BB84 could be distributed by serializing a classical photon *descriptor* (basis,
bit) per node; entanglement cannot. A Bell pair is a *joint* state, and measuring
one half must consistently collapse the other. SeQUeNCe's own ``QuantumManager``
is a single in-process object, so nothing in it spans the two FABRIC processes —
this register is the cross-process authority DESIGN §7 calls the "Quantum State
Service". One process owns a ``QStateRegister``; both parties issue create/measure
ops against it (locally or over the wire via RemoteQuantumManager).

Design notes:
  * n-qubit register from the start (keyed by integer qubit id), so a repeater
    chain's multi-qubit state and entanglement swapping (``bell_measure``) extend
    naturally — this is "designed for multi-hop".
  * Noise is a **Werner state** ρ = w·|Φ+⟩⟨Φ+| + (1−w)·I/4, sampled per pair
    (with prob depending on w, emit one of the four Bell states). With w = F this
    yields matching-basis QBER = (1−F)/2 AND CHSH S = 2√2·F, so the key-error rate
    and the Bell-inequality violation degrade together — exactly what makes an
    entanglement (E91) security test meaningful, and it keeps the ``fidelity`` knob
    identical to the BB84 path.
  * Measurement is projective at an arbitrary angle θ in the X–Z plane (θ=0 → Z
    basis, θ=π/2 → X basis, θ=π/4, 3π/4 → the E91/CHSH angles), driven by a random
    sample in [0,1) for reproducibility (mirrors SeQUeNCe's ``meas_samp``).
"""

from __future__ import annotations

import numpy as np

# The four Bell states as ket vectors over (qubit_a, qubit_b), basis |00>|01>|10>|11>.
_INV_SQRT2 = 1.0 / np.sqrt(2.0)
_BELL = {
    "phi_plus":  np.array([1, 0, 0, 1], dtype=complex) * _INV_SQRT2,   # (|00>+|11>)/√2
    "phi_minus": np.array([1, 0, 0, -1], dtype=complex) * _INV_SQRT2,  # (|00>-|11>)/√2
    "psi_plus":  np.array([0, 1, 1, 0], dtype=complex) * _INV_SQRT2,   # (|01>+|10>)/√2
    "psi_minus": np.array([0, 1, -1, 0], dtype=complex) * _INV_SQRT2,  # (|01>-|10>)/√2
}
_OTHER_BELL = ["phi_minus", "psi_plus", "psi_minus"]  # the three that add errors


def _rot_meas_unitary(theta: float) -> np.ndarray:
    """Single-qubit U s.t. measuring Z after U = measuring at angle θ (X–Z plane).

    U = Ry(−θ); it maps the +θ eigenvector (cos θ/2, sin θ/2) to |0>, so Z-outcome
    0 ↔ '+θ', 1 ↔ '−θ'.
    """
    c, s = np.cos(theta / 2), np.sin(theta / 2)
    return np.array([[c, s], [-s, c]], dtype=complex)


_PAULI_X = np.array([[0, 1], [1, 0]], dtype=complex)
_PAULI_Z = np.array([[1, 0], [0, -1]], dtype=complex)
_HADAMARD = np.array([[1, 1], [1, -1]], dtype=complex) * _INV_SQRT2
# CNOT over (control, target) in basis |00>|01>|10>|11>
_CNOT = np.array([[1, 0, 0, 0],
                  [0, 1, 0, 0],
                  [0, 0, 0, 1],
                  [0, 0, 1, 0]], dtype=complex)


class _Group:
    """A pure joint state over an ordered list of qubit ids."""

    __slots__ = ("ids", "amp")

    def __init__(self, ids: list[int], amp: np.ndarray):
        self.ids = ids
        self.amp = amp


class QStateRegister:
    """An n-qubit pure-state register with entangled groups keyed by qubit id."""

    def __init__(self, seed: int = 0):
        self._rng = np.random.default_rng(seed)
        self._groups: dict[int, _Group] = {}   # qubit id -> its group
        self._next_id = 0

    # -- allocation ------------------------------------------------------------

    def create_bell_pair(self, fidelity: float = 1.0) -> tuple[int, int]:
        """Allocate two qubits in a Werner state of the given fidelity.

        Returns (qubit_id_a, qubit_id_b). With prob mass matching
        ρ = F·|Φ+⟩⟨Φ+| + (1−F)·I/4, emit |Φ+⟩, else a uniformly-chosen other Bell
        state; averaged over shots this reproduces the Werner density matrix.
        """
        f = min(max(fidelity, 0.0), 1.0)
        # I/4 is the uniform mixture of all four Bell states, so:
        #   P(Φ+) = F + (1−F)/4 ;  P(each other) = (1−F)/4
        if self._rng.random() < f + (1.0 - f) / 4.0:
            amp = _BELL["phi_plus"].copy()
        else:
            amp = _BELL[_OTHER_BELL[int(self._rng.integers(3))]].copy()
        a, b = self._next_id, self._next_id + 1
        self._next_id += 2
        g = _Group([a, b], amp)
        self._groups[a] = g
        self._groups[b] = g
        return a, b

    # -- gates (repeater support) ------------------------------------------------

    def apply_pauli(self, qubit_id: int, x: int = 0, z: int = 0) -> None:
        """Apply X^x·Z^z to one qubit — the heralded correction after a swap."""
        if qubit_id not in self._groups:
            raise KeyError(f"unknown/at-rest qubit id {qubit_id}")
        if not (x or z):
            return
        u = np.eye(2, dtype=complex)
        if z:
            u = _PAULI_Z @ u
        if x:
            u = _PAULI_X @ u
        g = self._groups[qubit_id]
        g.amp = self._apply_1q(g.amp, u, g.ids.index(qubit_id), len(g.ids))

    def _merge(self, q1: int, q2: int) -> _Group:
        """Join the groups of two qubits into one product state (no-op if shared)."""
        g1, g2 = self._groups[q1], self._groups[q2]
        if g1 is g2:
            return g1
        merged = _Group(g1.ids + g2.ids, np.kron(g1.amp, g2.amp))
        for q in merged.ids:
            self._groups[q] = merged
        return merged

    def bell_measure(self, q1: int, q2: int,
                     samp1: float | None = None,
                     samp2: float | None = None) -> tuple[int, int]:
        """Bell-state measurement on two qubits — the repeater *swap* operation.

        Standard analyzer circuit: CNOT(q1→q2), H(q1), then measure both in Z.
        Returns the herald bits (m1, m2) identifying the Bell state:
            Φ+ → (0,0)   Φ− → (1,0)   Ψ+ → (0,1)   Ψ− → (1,1)
        Both qubits are consumed; any partners they were entangled with stay in
        the register, now joined in one group. Swapping A–B1 with B2–C via a BSM
        on (B1,B2) projects A–C onto the heralded Bell state — applying
        X^m2·Z^m1 to either survivor restores Φ+.
        """
        if q1 == q2:
            raise ValueError("bell_measure needs two distinct qubits")
        for q in (q1, q2):
            if q not in self._groups:
                raise KeyError(f"unknown/at-rest qubit id {q}")
        g = self._merge(q1, q2)
        i, j, n = g.ids.index(q1), g.ids.index(q2), len(g.ids)
        amp = self._apply_2q(g.amp, _CNOT, i, j, n)
        g.amp = self._apply_1q(amp, _HADAMARD, i, n)
        m1 = self.measure(q1, 0.0, samp=samp1)
        m2 = self.measure(q2, 0.0, samp=samp2)
        return m1, m2

    # -- measurement -----------------------------------------------------------

    def measure(self, qubit_id: int, angle: float, samp: float | None = None) -> int:
        """Projectively measure a qubit at X–Z-plane ``angle``; collapse & return bit.

        ``samp`` in [0,1) drives the outcome (reproducible). The measured qubit is
        removed from its group; the remaining qubits keep the collapsed state, so a
        peer measuring the other half sees the correlated result.
        """
        if qubit_id not in self._groups:
            raise KeyError(f"unknown/at-rest qubit id {qubit_id}")
        if samp is None:
            samp = float(self._rng.random())
        g = self._groups[qubit_id]
        idx = g.ids.index(qubit_id)
        n = len(g.ids)

        # rotate the target qubit so the desired basis becomes Z
        u = _rot_meas_unitary(angle)
        amp = self._apply_1q(g.amp, u, idx, n)

        # P(outcome 0) = sum of |amp|^2 over basis states with target bit == 0
        mask0 = self._bit_mask(idx, n, want=0)
        p0 = float(np.sum(np.abs(amp[mask0]) ** 2))
        outcome = 0 if samp < p0 else 1

        # collapse onto the outcome subspace and renormalize
        keep = self._bit_mask(idx, n, want=outcome)
        collapsed = np.zeros_like(amp)
        collapsed[keep] = amp[keep]
        norm = np.linalg.norm(collapsed)
        collapsed = collapsed / norm if norm > 0 else collapsed

        # trace out the measured qubit (exact for a post-measurement pure state)
        remaining_ids = [q for q in g.ids if q != qubit_id]
        if remaining_ids:
            reduced = self._drop_qubit(collapsed, idx, n, outcome)
            ng = _Group(remaining_ids, reduced)
            for q in remaining_ids:
                self._groups[q] = ng
        del self._groups[qubit_id]
        return outcome

    # -- linear-algebra helpers (small n; qubit id 0 = most significant bit) ----

    @staticmethod
    def _apply_1q(amp: np.ndarray, u: np.ndarray, idx: int, n: int) -> np.ndarray:
        t = amp.reshape([2] * n)
        t = np.tensordot(u, t, axes=([1], [idx]))
        return np.moveaxis(t, 0, idx).reshape(-1)

    @staticmethod
    def _apply_2q(amp: np.ndarray, u: np.ndarray, i: int, j: int, n: int) -> np.ndarray:
        """Apply a 4x4 unitary over qubits (i, j) of an n-qubit amplitude vector."""
        t = amp.reshape([2] * n)
        u4 = u.reshape(2, 2, 2, 2)          # (out_i, out_j, in_i, in_j)
        t = np.tensordot(u4, t, axes=([2, 3], [i, j]))
        return np.moveaxis(t, [0, 1], [i, j]).reshape(-1)

    @staticmethod
    def _bit_mask(idx: int, n: int, want: int) -> np.ndarray:
        bits = (np.arange(2 ** n) >> (n - 1 - idx)) & 1
        return bits == want

    @staticmethod
    def _drop_qubit(amp: np.ndarray, idx: int, n: int, outcome: int) -> np.ndarray:
        t = amp.reshape([2] * n)
        t = np.take(t, outcome, axis=idx)
        return t.reshape(-1)
