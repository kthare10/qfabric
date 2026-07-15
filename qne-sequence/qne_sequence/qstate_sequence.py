"""SeQUeNCeRegister — a QStateRegister backed by SeQUeNCe's own QuantumManager.

PROOF OF CONCEPT (2026-07-14). qfabric's entanglement path (E91, repeaters) runs on
``qstate_core.QStateRegister``, a small numpy state store — *not* SeQUeNCe's quantum
layer (see DESIGN §6.2). This module shows the alternative is feasible: the same
public interface, backed by ``sequence.kernel.quantum_manager.QuantumManagerKet``.

Why it matters: ``RemoteQuantumManager`` (remote_qm.py) proxies measurement ops over
the wire and is **agnostic to what backs the register**. So the distribution
mechanism qfabric built is exactly the hook a distributed SeQUeNCe would plug into —
swap QStateRegister for this class and the whole E91/repeater stack runs on
SeQUeNCe's QuantumManager, unchanged above the register. This is the concrete
version of the slide-10 question for the SeQUeNCe team.

Mapping (qfabric op -> SeQUeNCe QuantumManager):
  * create_bell_pair(f)  -> two qm.new() keys + qm.set([a,b], <Werner-sampled Bell
    amplitudes>). The Werner mixture is sampled per pair, identical to QStateRegister.
  * measure(id, angle)   -> run_circuit of an X-Z-plane measurement:
    Ry(-angle) then measure Z, built from SeQUeNCe's phase(theta)+h gates as
    P(pi/2) . H . P(-angle) . H . P(-pi/2)  (S . Rx . S-dagger = Ry).
  * bell_measure(q1,q2)  -> run_circuit(cx(0,1); h(0); measure both) — a Bell-state
    measurement, the swap op — returning the (m1, m2) herald.
  * apply_pauli(id,x,z)  -> run_circuit of x/z gates, no measurement.

Validated (tests/test_qstate_sequence_backed.py) to reproduce, against theory and
against the numpy register: matching-basis QBER = (1-f)/2, CHSH S = 2sqrt(2)*f, the
BSM herald table, and a swapped-chain correlation.
"""

from __future__ import annotations

import math

import numpy as np

from sequence.kernel.quantum_manager import QuantumManagerKet
from sequence.components.circuit import Circuit

_INV = 1.0 / math.sqrt(2.0)
# Bell states as ket amplitudes over |00>,|01>,|10>,|11>
_BELL = {
    "phi_plus":  [_INV, 0, 0, _INV],
    "phi_minus": [_INV, 0, 0, -_INV],
    "psi_plus":  [0, _INV, _INV, 0],
    "psi_minus": [0, _INV, -_INV, 0],
}
_OTHER = ["phi_minus", "psi_plus", "psi_minus"]


def _meas_gates(c: Circuit, angle: float) -> None:
    """Append an X-Z-plane angle measurement rotation: Ry(-angle) = S.Rx(-angle).S+."""
    c.phase(0, -math.pi / 2)
    c.h(0)
    c.phase(0, -angle)
    c.h(0)
    c.phase(0, math.pi / 2)


class SeQUeNCeRegister:
    """QStateRegister-compatible register backed by SeQUeNCe's QuantumManagerKet.

    Public interface matches qstate_core.QStateRegister exactly, so it drops in
    behind QuantumStateService / RemoteQuantumManager with no other changes.
    """

    def __init__(self, seed: int = 0):
        self.qm = QuantumManagerKet()
        self._rng = np.random.default_rng(seed)

    def create_bell_pair(self, fidelity: float = 1.0) -> tuple[int, int]:
        f = min(max(fidelity, 0.0), 1.0)
        if self._rng.random() < f + (1.0 - f) / 4.0:
            amp = _BELL["phi_plus"]
        else:
            amp = _BELL[_OTHER[int(self._rng.integers(3))]]
        a, b = self.qm.new(), self.qm.new()
        self.qm.set([a, b], amp)
        return a, b

    def measure(self, qubit_id: int, angle: float, samp: float | None = None) -> int:
        if samp is None:
            samp = float(self._rng.random())
        c = Circuit(1)
        _meas_gates(c, angle)
        c.measure(0)
        return int(self.qm.run_circuit(c, [qubit_id], samp)[qubit_id])

    def bell_measure(self, q1: int, q2: int, samp1: float | None = None,
                     samp2: float | None = None) -> tuple[int, int]:
        if q1 == q2:
            raise ValueError("bell_measure needs two distinct qubits")
        c = Circuit(2)
        c.cx(0, 1)
        c.h(0)
        c.measure(0)
        c.measure(1)
        samp = samp1 if samp1 is not None else float(self._rng.random())
        res = self.qm.run_circuit(c, [q1, q2], samp)
        return int(res[q1]), int(res[q2])

    def apply_pauli(self, qubit_id: int, x: int = 0, z: int = 0) -> None:
        if not (x or z):
            return
        c = Circuit(1)
        if z:
            c.z(0)
        if x:
            c.x(0)
        self.qm.run_circuit(c, [qubit_id])
