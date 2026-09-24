"""Distributed-computing primitives: teleportation and the non-local CNOT.

Validates ``qne_sequence.dqc`` on BOTH register backends — the numpy
``QStateRegister`` the entanglement stack already runs on, and the Qiskit-backed
``QiskitRegister`` that adds arbitrary circuits. Running the identical assertions
against both is the evidence that swapping the quantum layer (the point
``qstate_sequence.py`` made for SeQUeNCe) holds for computation too.

The laws asserted here tie a distributed *gate* to the distributed *key* qfabric
is already validated against: with a Werner-weight-w pair both primitives fail at
rate (1-w)/2 — the same curve as the E91 QBER — because a non-Phi+ pair lands a
random Pauli on the output. Concretely, per shot the pair is wrong with
probability 3(1-w)/4 and the error is one of Z, X, XZ uniformly, which maps onto
the telegate as: Z -> phase error on the control, X -> bit error on the target,
XZ -> both. Hence bit-error rate = phase-error rate = (2/3)(3(1-w)/4) = (1-w)/2.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from qne_sequence import dqc
from qne_sequence.qstate_core import QStateRegister

try:
    from qne_sequence.qstate_qiskit import QiskitRegister
except ImportError:                                   # qiskit is an optional extra
    QiskitRegister = None

REGISTERS = [
    pytest.param(QStateRegister, id="numpy"),
    pytest.param(QiskitRegister, id="qiskit",
                 marks=pytest.mark.skipif(QiskitRegister is None,
                                          reason="qiskit not installed")),
]

Z_BASIS, X_BASIS = 0.0, math.pi / 2
PLUS = np.array([1, 1], dtype=complex) / math.sqrt(2)


def ket(theta: float) -> np.ndarray:
    """The X-Z-plane state measured as outcome 0 at angle ``theta``."""
    return np.array([math.cos(theta / 2), math.sin(theta / 2)], dtype=complex)


# -- register plumbing --------------------------------------------------------

@pytest.mark.parametrize("Register", REGISTERS)
@pytest.mark.parametrize("c,t", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_cnot_truth_table(Register, c, t):
    """apply_gate's operand order: ids[0] is the control. Guards the endianness
    flip between qstate_core's MSB-first groups and Qiskit's little-endian qubits."""
    reg = Register(seed=1)
    a = reg.alloc(ket(math.pi * c))
    b = reg.alloc(ket(math.pi * t))
    reg.apply_gate([a, b], dqc.CNOT)
    assert (reg.measure(a, Z_BASIS), reg.measure(b, Z_BASIS)) == (c, t ^ c)


@pytest.mark.parametrize("Register", REGISTERS)
def test_two_qubit_gate_entangles_across_groups(Register):
    """A gate spanning two groups merges them — that merge IS the entanglement."""
    reg = Register(seed=2)
    a, b = reg.alloc(PLUS), reg.alloc()
    reg.apply_gate([a, b], dqc.CNOT)
    assert np.allclose(reg.statevector([a, b]), [1 / math.sqrt(2), 0, 0, 1 / math.sqrt(2)])


# -- teleportation ------------------------------------------------------------

@pytest.mark.parametrize("Register", REGISTERS)
@pytest.mark.parametrize("theta", [0.0, math.pi / 3, math.pi / 2, 2.1, math.pi])
def test_teleport_transfers_an_arbitrary_state(Register, theta):
    """A perfect pair teleports any X-Z-plane state exactly: measuring the receiver's
    qubit at the state's own angle always yields 0."""
    reg = Register(seed=5)
    for _ in range(200):
        data = reg.alloc(ket(theta))
        epr_a, epr_b = reg.create_bell_pair(1.0)
        out, bits = dqc.teleport(reg, data, epr_a, epr_b)
        assert all(b in (0, 1) for b in bits)      # exactly 2 classical bits, A -> B
        assert reg.measure(out, theta) == 0


@pytest.mark.parametrize("Register", REGISTERS)
def test_teleport_consumes_the_data_qubit_and_the_pair(Register):
    """No-cloning bookkeeping: the input and the sender's half are gone; the state
    now lives on the receiver's half, which is the qubit teleport() returns."""
    reg = Register(seed=5)
    data = reg.alloc(PLUS)
    epr_a, epr_b = reg.create_bell_pair(1.0)
    out, _ = dqc.teleport(reg, data, epr_a, epr_b)
    assert out == epr_b
    for gone in (data, epr_a):
        with pytest.raises(KeyError):
            reg.measure(gone, Z_BASIS)


@pytest.mark.parametrize("Register", REGISTERS)
@pytest.mark.parametrize("w", [1.0, 0.9, 0.8, 0.6])
def test_teleport_error_follows_the_werner_law(Register, w):
    """Teleporting through a Werner-w pair errs at (1-w)/2 — the E91 QBER curve."""
    reg = Register(seed=6)
    n, err = 4000, 0
    for _ in range(n):
        data = reg.alloc()                                   # |0>
        epr_a, epr_b = reg.create_bell_pair(w)
        out, _ = dqc.teleport(reg, data, epr_a, epr_b)
        err += reg.measure(out, Z_BASIS) != 0
    assert abs(err / n - (1 - w) / 2) < 0.02


# -- non-local CNOT -----------------------------------------------------------

@pytest.mark.parametrize("Register", REGISTERS)
@pytest.mark.parametrize("c,t", [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_telegate_reproduces_the_cnot_truth_table(Register, c, t):
    reg = Register(seed=7)
    control, target = reg.alloc(ket(math.pi * c)), reg.alloc(ket(math.pi * t))
    epr_a, epr_b = reg.create_bell_pair(1.0)
    m1, m2 = dqc.telegate_cnot(reg, control, target, epr_a, epr_b)
    assert (m1, m2) == (m1 & 1, m2 & 1)            # one classical bit each way
    assert (reg.measure(control, Z_BASIS), reg.measure(target, Z_BASIS)) == (c, t ^ c)


@pytest.mark.parametrize("Register", REGISTERS)
def test_telegate_entangles_two_qpus(Register):
    """|+>|0> -> Phi+ across the two nodes: the gate is coherent, not a classical
    copy of a measured bit. Correlated in Z *and* in X is what rules that out."""
    reg = Register(seed=8)
    control, target = reg.alloc(PLUS), reg.alloc()
    epr_a, epr_b = reg.create_bell_pair(1.0)
    dqc.telegate_cnot(reg, control, target, epr_a, epr_b)
    amp = reg.statevector([control, target])
    assert np.allclose(np.abs(amp), [1 / math.sqrt(2), 0, 0, 1 / math.sqrt(2)])
    for basis in (Z_BASIS, X_BASIS):
        reg2 = Register(seed=8)
        eq = 0
        for _ in range(200):
            c2, t2 = reg2.alloc(PLUS), reg2.alloc()
            a2, b2 = reg2.create_bell_pair(1.0)
            dqc.telegate_cnot(reg2, c2, t2, a2, b2)
            eq += reg2.measure(c2, basis) == reg2.measure(t2, basis)
        assert eq == 200


@pytest.mark.parametrize("Register", REGISTERS)
def test_telegate_leaves_the_control_in_place(Register):
    """The reason a compiler emits telegate rather than teleport: the control stays
    on its own node and is still usable, while only the pair is consumed."""
    reg = Register(seed=8)
    control, target = reg.alloc(), reg.alloc()
    epr_a, epr_b = reg.create_bell_pair(1.0)
    dqc.telegate_cnot(reg, control, target, epr_a, epr_b)
    reg.apply_gate([control, target], dqc.CNOT)     # control still live: no KeyError
    for gone in (epr_a, epr_b):
        with pytest.raises(KeyError):
            reg.measure(gone, Z_BASIS)


@pytest.mark.parametrize("Register", REGISTERS)
@pytest.mark.parametrize("w", [1.0, 0.9, 0.8, 0.6])
def test_telegate_bit_and_phase_errors_follow_the_werner_law(Register, w):
    """Both error channels sit on the same (1-w)/2 curve: X-type errors land on the
    target (visible in Z), Z-type on the control (visible only in X)."""
    n = 4000
    reg = Register(seed=9)
    bit_err = 0
    for _ in range(n):                                       # |0>|0> -> target bit error
        control, target = reg.alloc(), reg.alloc()
        epr_a, epr_b = reg.create_bell_pair(w)
        dqc.telegate_cnot(reg, control, target, epr_a, epr_b)
        reg.measure(control, Z_BASIS)
        bit_err += reg.measure(target, Z_BASIS) != 0
    assert abs(bit_err / n - (1 - w) / 2) < 0.02

    reg = Register(seed=11)
    phase_err = 0
    for _ in range(n):                                       # |+>|0> -> Phi+, X-correlated
        control, target = reg.alloc(PLUS), reg.alloc()
        epr_a, epr_b = reg.create_bell_pair(w)
        dqc.telegate_cnot(reg, control, target, epr_a, epr_b)
        phase_err += reg.measure(control, X_BASIS) != reg.measure(target, X_BASIS)
    assert abs(phase_err / n - (1 - w) / 2) < 0.02


@pytest.mark.parametrize("Register", REGISTERS)
def test_a_permanently_lost_correction_bit_costs_half_the_shots(Register):
    """An unrecoverable correction is a Pauli error: losing m1 (A->B) flips the
    target half the time, losing m2 (B->A) dephases the control half the time.
    This is the *permanent loss* regime — see the next test for a late bit, which
    is a different thing entirely."""
    n = 2000
    reg = Register(seed=13)
    bit_err = 0
    for _ in range(n):
        control, target = reg.alloc(), reg.alloc()
        epr_a, epr_b = reg.create_bell_pair(1.0)
        dqc.telegate_cnot_send(reg, control, epr_a)
        m2 = dqc.telegate_cnot_apply(reg, epr_b, target, 0)      # m1 never arrives
        dqc.telegate_cnot_finish(reg, control, m2)
        reg.measure(control, Z_BASIS)
        bit_err += reg.measure(target, Z_BASIS) != 0
    assert abs(bit_err / n - 0.5) < 0.05

    reg = Register(seed=17)
    phase_err = 0
    for _ in range(n):
        control, target = reg.alloc(PLUS), reg.alloc()
        epr_a, epr_b = reg.create_bell_pair(1.0)
        m1 = dqc.telegate_cnot_send(reg, control, epr_a)
        dqc.telegate_cnot_apply(reg, epr_b, target, m1)          # m2 never arrives
        phase_err += reg.measure(control, X_BASIS) != reg.measure(target, X_BASIS)
    assert abs(phase_err / n - 0.5) < 0.05


@pytest.mark.parametrize("Register", REGISTERS)
def test_a_late_correction_bit_is_recovered_not_lost(Register):
    """Latency is NOT an error source for a Pauli correction. A bit that misses the
    gate can still be folded into the recorded outcome by XOR — a tracked Pauli
    frame, the same move repeater.py makes when it XOR-composes L heralds and
    corrects once at the end. Both channels come back exactly clean."""
    n = 500
    reg = Register(seed=13)
    bit_err = 0
    for _ in range(n):
        control, target = reg.alloc(), reg.alloc()
        epr_a, epr_b = reg.create_bell_pair(1.0)
        m1 = dqc.telegate_cnot_send(reg, control, epr_a)
        m2 = dqc.telegate_cnot_apply(reg, epr_b, target, 0)      # m1 arrives late...
        dqc.telegate_cnot_finish(reg, control, m2)
        reg.measure(control, Z_BASIS)
        out = dqc.correct_outcome(reg.measure(target, Z_BASIS), Z_BASIS, x=m1)
        bit_err += out != 0                                      # ...and is folded in
    assert bit_err == 0

    reg = Register(seed=17)
    phase_err = 0
    for _ in range(n):
        control, target = reg.alloc(PLUS), reg.alloc()
        epr_a, epr_b = reg.create_bell_pair(1.0)
        m1 = dqc.telegate_cnot_send(reg, control, epr_a)
        m2 = dqc.telegate_cnot_apply(reg, epr_b, target, m1)     # m2 arrives late
        c_out = dqc.correct_outcome(reg.measure(control, X_BASIS), X_BASIS, z=m2)
        phase_err += c_out != reg.measure(target, X_BASIS)
    assert phase_err == 0


@pytest.mark.parametrize("Register", REGISTERS)
def test_a_late_teleport_correction_is_recovered(Register):
    """Same for teleportation, on both Pauli channels: X^m2 relabels a Z-basis
    readout, Z^m1 relabels an X-basis one."""
    n = 500
    for state, readout, pauli in [(None, Z_BASIS, "x"), (PLUS, X_BASIS, "z")]:
        reg = Register(seed=19)
        err = 0
        for _ in range(n):
            data = reg.alloc(state)
            epr_a, epr_b = reg.create_bell_pair(1.0)
            m1, m2 = dqc.teleport_send(reg, data, epr_a)
            dqc.teleport_recv(reg, epr_b, 0, 0)                  # neither bit made it
            late = {pauli: m2 if pauli == "x" else m1}
            err += dqc.correct_outcome(reg.measure(epr_b, readout), readout, **late) != 0
        assert err == 0


def test_correct_outcome_only_relabels_the_anticommuting_pauli():
    assert dqc.correct_outcome(0, Z_BASIS, x=1) == 1     # X flips a Z-basis result
    assert dqc.correct_outcome(0, Z_BASIS, z=1) == 0     # Z does not
    assert dqc.correct_outcome(1, X_BASIS, z=1) == 0     # Z flips an X-basis result
    assert dqc.correct_outcome(1, X_BASIS, x=1) == 1     # X does not
    with pytest.raises(ValueError):                      # no clean relabelling here
        dqc.correct_outcome(0, math.pi / 4, x=1)


# -- backend equivalence ------------------------------------------------------

@pytest.mark.skipif(QiskitRegister is None, reason="qiskit not installed")
def test_qiskit_and_numpy_backends_agree_shot_for_shot():
    """Same seed, same script: identical heralds and identical joint amplitudes.
    The Qiskit register is a drop-in, not an approximation."""
    out = []
    for Register in (QStateRegister, QiskitRegister):
        reg = Register(seed=3)
        control, target = reg.alloc(PLUS), reg.alloc()
        epr_a, epr_b = reg.create_bell_pair(0.85)
        bits = dqc.telegate_cnot(reg, control, target, epr_a, epr_b)
        out.append((bits, reg.statevector([control, target])))
    assert out[0][0] == out[1][0]
    assert np.allclose(out[0][1], out[1][1])


@pytest.mark.skipif(QiskitRegister is None, reason="qiskit not installed")
def test_qiskit_register_runs_a_real_circuit():
    """The capability the numpy register cannot offer: a node hands in an ordinary
    QuantumCircuit over its local qubits and the shared register evolves under it."""
    from qiskit import QuantumCircuit
    from qiskit.quantum_info import Statevector

    circ = QuantumCircuit(2)
    circ.h(0)
    circ.t(0)
    circ.cx(0, 1)
    circ.ry(0.7, 1)

    reg = QiskitRegister(seed=4)
    q0, q1 = reg.alloc(), reg.alloc()
    reg.apply_circuit([q0, q1], circ)
    # reg.statevector is MSB-first over [q0, q1]; Statevector.from_instruction is
    # little-endian over the circuit's qubits, so reverse to compare.
    want = np.asarray(Statevector.from_instruction(circ).data).reshape(2, 2).T.reshape(-1)
    assert np.allclose(reg.statevector([q0, q1]), want)


# -- session runner -----------------------------------------------------------

@pytest.mark.parametrize("backend", ["numpy", "qiskit"])
@pytest.mark.parametrize("primitive", ["teleport", "telegate"])
def test_session_runner_reports_the_law_on_both_channels(backend, primitive):
    if backend == "qiskit" and QiskitRegister is None:
        pytest.skip("qiskit not installed")
    res = dqc.run_dqc_session(primitive, shots=2000, fidelity=0.9,
                              backend=backend, seed=21)
    assert res.error_pred == pytest.approx(0.05)
    assert res.bit_error == pytest.approx(res.error_pred, abs=0.02)
    assert res.phase_error == pytest.approx(res.error_pred, abs=0.02)
    # 2 channels x 2000 gates, each spending 1 pair and 2 classical bits
    assert (res.pairs_consumed, res.classical_bits) == (4000, 8000)


@pytest.mark.parametrize("primitive,dropped,channel", [("teleport", "m2", "bit"),
                                                       ("teleport", "m1", "phase"),
                                                       ("telegate", "m1", "bit"),
                                                       ("telegate", "m2", "phase")])
def test_session_runner_drop_control_breaks_exactly_one_channel(primitive, dropped, channel):
    """Guards the correction-bit wiring (m1 and m2 are NOT interchangeable): each
    bit protects one Pauli channel, and losing it for good costs half the shots on
    that channel and nothing on the other. A control run reports error_pred=None
    because the Werner law does not describe it."""
    clean = dqc.run_dqc_session(primitive, shots=2000, fidelity=1.0, seed=23)
    assert (clean.bit_error, clean.phase_error) == (0.0, 0.0)

    broken = dqc.run_dqc_session(primitive, shots=2000, fidelity=1.0, seed=23,
                                 dropped=dropped)
    assert broken.error_pred is None
    hit = broken.bit_error if channel == "bit" else broken.phase_error
    spared = broken.phase_error if channel == "bit" else broken.bit_error
    assert hit == pytest.approx(0.5, abs=0.05)
    assert spared == 0.0


@pytest.mark.parametrize("primitive", ["teleport", "telegate"])
@pytest.mark.parametrize("late", ["m1", "m2"])
def test_session_runner_late_bit_recovers_the_clean_result(primitive, late):
    """The distinction the drop control must not blur: a delayed correction folded
    into the outcome leaves no error at all, so herald latency is a memory-time
    cost rather than a fidelity cost."""
    res = dqc.run_dqc_session(primitive, shots=1000, fidelity=1.0, seed=23, late=late)
    assert (res.bit_error, res.phase_error) == (0.0, 0.0)
    assert res.error_pred == 0.0          # the law still applies: nothing was lost


@pytest.mark.parametrize("kwargs", [
    {"shots": 0}, {"shots": -1}, {"shots": 2.5}, {"shots": True},
    {"fidelity": 1.5}, {"fidelity": -0.1},
    {"primitive": "swap"}, {"backend": "aer"},
    {"dropped": "m3"}, {"late": "m0"}, {"dropped": "m1", "late": "m2"},
])
def test_session_runner_rejects_invalid_input(kwargs):
    """Reject rather than silently clamp: create_bell_pair clamps w internally, so
    an out-of-range fidelity would otherwise simulate w=1 while reporting a Bell
    fidelity above 1 and a negative predicted error."""
    with pytest.raises(ValueError):
        dqc.run_dqc_session(**{"primitive": "telegate", "shots": 10, **kwargs})
