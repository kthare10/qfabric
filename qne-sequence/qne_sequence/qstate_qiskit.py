"""QiskitRegister — a QStateRegister backed by Qiskit, so a node can run *circuits*.

Why this exists (ROADMAP Phase 6, distributed quantum computing). ``qstate_core``
is a register for *entanglement protocols*: create a Bell pair, measure it at an
angle, Bell-measure two halves, apply a Pauli correction. That is everything E91
and a repeater chain need and nothing a computation needs — there is no H, no T,
no way to say "run this circuit on my two local qubits". This backend keeps the
identical public interface (so ``RemoteQuantumManager`` and the whole distributed
stack above it are unchanged — same argument as ``qstate_sequence.py`` made for
SeQUeNCe) and adds :meth:`QiskitRegister.apply_circuit`: a node hands in a real
``qiskit.QuantumCircuit`` over its own qubits and Qiskit evolves the joint state.

That single addition turns the register into *two QPUs sharing one state vector*,
with the network supplying Bell pairs between them — which is exactly the
centralized-quantum-manager model qfabric already runs E91 on.

What Qiskit actually does here: gate application (``Operator`` / ``QuantumCircuit``
via ``Statevector.evolve``) and outcome probabilities. The *group* bookkeeping —
which qubit ids currently share a joint state, and merging two groups when a gate
spans them — stays in this module, because it is the thing that makes the register
a cross-process authority rather than a simulator, and it must behave identically
across backends.

Endianness. ``qstate_core`` orders a group's amplitudes MSB-first over ``group.ids``
(``ids[0]`` is the most significant bit); Qiskit is little-endian (qubit 0 is the
least significant). The two coincide under ``ids[k] <-> qiskit qubit n-1-k``, which
is what :func:`_qi` computes, so a group's raw amplitude vector is byte-for-byte
comparable with the numpy register's. Tests assert that agreement rather than
trusting this paragraph.

No Aer needed: ``qiskit.quantum_info.Statevector`` is the engine, which keeps the
dependency light and exact (no shot noise on the state itself). Werner mixedness
is carried as a per-shot stochastic unravelling, identical to ``qstate_core`` —
see :meth:`create_bell_pair`.
"""

from __future__ import annotations

import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import Operator, Statevector

from .qstate_core import _BELL, _OTHER_BELL, _rot_meas_unitary


class _QGroup:
    """A pure joint state over an ordered list of qubit ids (``ids[0]`` = MSB)."""

    __slots__ = ("ids", "sv")

    def __init__(self, ids: list[int], sv: Statevector):
        self.ids = ids
        self.sv = sv


def _qi(group: _QGroup, qubit_id: int) -> int:
    """Register qubit id -> Qiskit qubit index within its group (endianness flip)."""
    return len(group.ids) - 1 - group.ids.index(qubit_id)


class QiskitRegister:
    """Qiskit-backed drop-in for :class:`qstate_core.QStateRegister`, plus circuits."""

    def __init__(self, seed: int = 0):
        self._rng = np.random.default_rng(seed)
        self._groups: dict[int, _QGroup] = {}
        self._next_id = 0

    # -- allocation ------------------------------------------------------------

    def create_bell_pair(self, fidelity: float = 1.0) -> tuple[int, int]:
        """Allocate two qubits in a Werner state of weight w = ``fidelity``.

        Same stochastic unravelling as ``QStateRegister``: with probability
        w + (1-w)/4 emit |Phi+>, else one of the other three Bell states uniformly.
        Averaged over shots that reproduces rho = w|Phi+><Phi+| + (1-w)I/4, and it
        keeps the state *pure* per shot -- so a faulty pair shows up downstream as a
        random Pauli error on the computation, which is what makes a statevector
        backend sufficient for a noisy distributed circuit.
        """
        f = min(max(fidelity, 0.0), 1.0)
        if self._rng.random() < f + (1.0 - f) / 4.0:
            amp = _BELL["phi_plus"].copy()
        else:
            amp = _BELL[_OTHER_BELL[int(self._rng.integers(3))]].copy()
        a, b = self._next_id, self._next_id + 1
        self._next_id += 2
        self._bind(_QGroup([a, b], Statevector(amp)))
        return a, b

    def alloc(self, amp: np.ndarray | None = None) -> int:
        """Allocate one fresh qubit -- |0> by default, or in the given 2-amplitude state."""
        vec = np.array([1, 0], dtype=complex) if amp is None else np.asarray(amp, dtype=complex)
        if vec.shape != (2,):
            raise ValueError("a single-qubit state needs exactly 2 amplitudes")
        norm = np.linalg.norm(vec)
        if norm == 0:
            raise ValueError("state vector must be non-zero")
        q = self._next_id
        self._next_id += 1
        self._bind(_QGroup([q], Statevector(vec / norm)))
        return q

    # -- gates -----------------------------------------------------------------

    def apply_pauli(self, qubit_id: int, x: int = 0, z: int = 0) -> None:
        """Apply X^x.Z^z to one qubit -- the heralded correction after a swap."""
        self._require(qubit_id)
        if not (x or z):
            return
        circ = QuantumCircuit(1)
        if z:
            circ.z(0)
        if x:
            circ.x(0)
        self.apply_circuit([qubit_id], circ)

    def apply_gate(self, qubit_ids: list[int], matrix: np.ndarray) -> None:
        """Apply a 2x2 or 4x4 unitary; ``qubit_ids[0]`` is the matrix's MSB (the control)."""
        u = np.asarray(matrix, dtype=complex)
        want = (2 ** len(qubit_ids),) * 2
        if u.shape != want:
            raise ValueError(f"{len(qubit_ids)} qubit(s) need a {want[0]}x{want[0]} matrix")
        # Operator() reads a matrix little-endian, so reverse to keep ids[0] as the MSB.
        self.apply_circuit(list(reversed(qubit_ids)), Operator(u))

    def apply_circuit(self, qubit_ids: list[int], circuit: QuantumCircuit | Operator) -> None:
        """Run a Qiskit circuit whose qubit ``k`` is register qubit ``qubit_ids[k]``.

        This is the QPU entry point: a node builds an ordinary ``QuantumCircuit``
        over its local qubits (data qubits and/or its half of a network Bell pair)
        and evolves the shared register with it. Operands spanning several groups
        are merged first -- that merge *is* the entanglement the gate creates.
        The circuit must be unitary (no measure/reset); measurement goes through
        :meth:`measure`, which is the operation the distributed protocols proxy.
        """
        if len(set(qubit_ids)) != len(qubit_ids):
            raise ValueError("qubit_ids must be distinct")
        for q in qubit_ids:
            self._require(q)
        if isinstance(circuit, QuantumCircuit) and circuit.num_qubits != len(qubit_ids):
            raise ValueError(f"circuit has {circuit.num_qubits} qubits, got {len(qubit_ids)} ids")
        g = self._merge(qubit_ids)
        g.sv = g.sv.evolve(circuit, qargs=[_qi(g, q) for q in qubit_ids])

    # -- measurement -----------------------------------------------------------

    def bell_measure(self, q1: int, q2: int,
                     samp1: float | None = None,
                     samp2: float | None = None) -> tuple[int, int]:
        """Bell-state measurement on two qubits -- the repeater swap, and the sending
        half of a teleportation.

        CNOT(q1->q2), H(q1), then Z-measure both; herald (m1, m2) identifies the Bell
        state (Phi+ -> (0,0), Phi- -> (1,0), Psi+ -> (0,1), Psi- -> (1,1)), and
        X^m2.Z^m1 on a surviving partner restores Phi+.
        """
        if q1 == q2:
            raise ValueError("bell_measure needs two distinct qubits")
        for q in (q1, q2):
            self._require(q)
        analyzer = QuantumCircuit(2)
        analyzer.cx(0, 1)
        analyzer.h(0)
        self.apply_circuit([q1, q2], analyzer)
        m1 = self.measure(q1, 0.0, samp=samp1)
        m2 = self.measure(q2, 0.0, samp=samp2)
        return m1, m2

    def measure(self, qubit_id: int, angle: float, samp: float | None = None) -> int:
        """Projectively measure a qubit at X-Z-plane ``angle``; collapse & return the bit.

        ``samp`` in [0,1) drives the outcome (reproducible). The measured qubit leaves
        its group; the survivors keep the collapsed state, so a peer measuring the
        other half of a pair sees the correlated result.
        """
        self._require(qubit_id)
        if samp is None:
            samp = float(self._rng.random())
        g = self._groups[qubit_id]
        idx, n = g.ids.index(qubit_id), len(g.ids)

        # rotate the target so the requested basis becomes Z, then read p(0) off Qiskit
        rotated = g.sv.evolve(Operator(_rot_meas_unitary(angle)), qargs=[_qi(g, qubit_id)])
        p0 = float(rotated.probabilities([_qi(g, qubit_id)])[0])
        outcome = 0 if samp < p0 else 1

        # collapse onto the outcome subspace and trace the measured qubit out.
        # axis k of the MSB-first reshape is ids[k] (see the endianness note above).
        t = np.asarray(rotated.data).reshape([2] * n)
        kept = np.take(t, outcome, axis=idx).reshape(-1)
        norm = np.linalg.norm(kept)
        if norm > 0:
            kept = kept / norm

        remaining = [q for q in g.ids if q != qubit_id]
        if remaining:
            self._bind(_QGroup(remaining, Statevector(kept)))
        del self._groups[qubit_id]
        return outcome

    # -- inspection (validation only -- no physical node can read this out) -----

    def statevector(self, qubit_ids: list[int]) -> np.ndarray:
        """Joint amplitudes of ``qubit_ids`` (which must share one group), MSB first."""
        g = self._groups[qubit_ids[0]]
        if any(self._groups.get(q) is not g for q in qubit_ids):
            raise ValueError("qubits are not in a single joint state")
        if sorted(g.ids) != sorted(qubit_ids):
            raise ValueError("group holds qubits beyond the ones requested")
        order = [g.ids.index(q) for q in qubit_ids]
        t = np.asarray(g.sv.data).reshape([2] * len(g.ids))
        return np.transpose(t, order).reshape(-1)

    # -- internals -------------------------------------------------------------

    def _require(self, qubit_id: int) -> None:
        if qubit_id not in self._groups:
            raise KeyError(f"unknown/at-rest qubit id {qubit_id}")

    def _bind(self, group: _QGroup) -> _QGroup:
        for q in group.ids:
            self._groups[q] = group
        return group

    def _merge(self, qubit_ids: list[int]) -> _QGroup:
        """Join the groups of the given qubits into one product state."""
        merged = self._groups[qubit_ids[0]]
        for q in qubit_ids[1:]:
            g = self._groups[q]
            if g is merged:
                continue
            # kron puts `merged`'s qubits in the high bits, matching ids order MSB-first
            merged = self._bind(_QGroup(merged.ids + g.ids,
                                        Statevector(np.kron(merged.sv.data, g.sv.data))))
        return merged
