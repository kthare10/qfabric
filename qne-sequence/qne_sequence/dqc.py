"""Distributed quantum computing primitives — teleportation and the non-local CNOT.

ROADMAP Phase 6. QKD *consumes* entanglement to agree on a secret; distributed
computing consumes it to run a gate between two QPUs that hold no qubit in common.
Both primitives below take one Bell pair from the network and a small number of
classical bits over the real link, so the cost of a distributed circuit is quoted
in exactly the two currencies qfabric already measures: pairs and classical frames.

Each primitive is split into the steps a *single node* can execute locally, with
the classical bits returned explicitly, because those bits are the thing that has
to cross the wire (``RemoteQuantumManager`` / ``ReliableLink``) in the distributed
runner.

**Late is not lost.** A correction that has not arrived yet does *not* have to become
an error: the corrections here are Paulis, so the receiver can hold the qubit and
apply the Pauli when the bit lands, propagate it as a tracked *Pauli frame* through
subsequent Clifford gates, or — if the qubit has already been measured — fold it into
the recorded outcome (:func:`correct_outcome`), since an X correction simply flips a
Z-basis result and a Z correction flips an X-basis one. That is not a new trick: it is
exactly what ``repeater.py`` already does when it XOR-composes L heralds and corrects
once at the end.

So herald latency is a **memory-time** cost, not an error source — the qubit (or the
frame) has to survive until the bit arrives. An error appears only when the correction
is *permanently* unavailable, or arrives after the result has been irreversibly
consumed. Both regimes are modelled here (``dropped`` vs ``late`` in
:func:`run_dqc_session`) and pinned by tests, because conflating them overstates what
the classical channel costs.

**teleport** (state transfer, 2 classical bits, A -> B)::

    m1, m2 = teleport_send(reg, data_q, epr_a)        # on A: BSM, consumes data_q
    #   ---- (m1, m2) cross the classical channel ----
    teleport_recv(reg, epr_b, m1, m2)                 # on B: X^m2 Z^m1; epr_b IS the state

The send step is literally ``bell_measure(data, epr_a)`` — the repeater's swap
operation. Entanglement swapping *is* teleportation of half a Bell pair, so the
validated repeater code already contained this primitive.

**telegate** (non-local CNOT, control on A, target on B, 1 bit each way)::

    m1 = telegate_cnot_send(reg, control, epr_a)      # on A: CNOT(c->a), Z-measure a
    #   ---- m1 crosses A -> B ----
    m2 = telegate_cnot_apply(reg, epr_b, target, m1)  # on B: X^m1, CNOT(b->t), X-measure b
    #   ---- m2 crosses B -> A ----
    telegate_cnot_finish(reg, control, m2)            # on A: Z^m2

This is the cat-entangler / cat-disentangler construction (Eisert et al. 2000):
the control's *computational-basis value* is copied into B's half of the pair
(legal — it copies a basis value, not an unknown state), used as the local control
there, then uncopied to remove the residual entanglement. The control qubit stays
on A and is still available for the rest of A's circuit, which is why telegate,
not teleportation, is the primitive a distributed compiler actually emits.

Noise. With a Werner-weight-w pair the register emits a non-Phi+ Bell state with
probability 3(1-w)/4, and each such shot lands a Pauli error on the output — which
is why a statevector backend is enough for a *noisy* distributed circuit. The three
faulty Bell states map onto the telegate as

    Z  -> phase error on the control      X  -> bit error on the target
    XZ -> both

so bit-error rate = phase-error rate = (2/3)(3(1-w)/4) = **(1-w)/2**, and a teleported
qubit errs at (1-w)/2 in its own basis. That is the same law the E91 QBER is validated
against: a distributed gate and a distributed key degrade with distance on one curve.
``tests/test_dqc.py`` asserts all of it against both register backends.

Cost, in the units qfabric measures: 1 Bell pair + 2 classical bits per teleport,
1 Bell pair + 1 bit each way per non-local CNOT.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass

import numpy as np

# Gate constants, kept here so the primitives run on any register exposing
# apply_gate/measure/apply_pauli (numpy QStateRegister or QiskitRegister).
# 4x4 matrices are over |00>,|01>,|10>,|11> with qubit_ids[0] as the MSB/control.
CNOT = np.array([[1, 0, 0, 0],
                 [0, 1, 0, 0],
                 [0, 0, 0, 1],
                 [0, 0, 1, 0]], dtype=complex)

_Z_BASIS = 0.0
_X_BASIS = math.pi / 2


# -- teleportation ------------------------------------------------------------

def teleport_send(reg, data_q: int, epr_local: int) -> tuple[int, int]:
    """On the sender: Bell-measure the data qubit against its half of the pair.

    Consumes both qubits and returns the 2 herald bits to ship to the receiver.
    """
    return reg.bell_measure(data_q, epr_local)


def teleport_recv(reg, epr_remote: int, m1: int, m2: int) -> int:
    """On the receiver: apply X^m2.Z^m1 — after which ``epr_remote`` *is* the state."""
    reg.apply_pauli(epr_remote, x=m2, z=m1)
    return epr_remote


def teleport(reg, data_q: int, epr_local: int, epr_remote: int) -> tuple[int, tuple[int, int]]:
    """Both halves in one call (in-process demo). Returns (qubit id, classical bits)."""
    m1, m2 = teleport_send(reg, data_q, epr_local)
    return teleport_recv(reg, epr_remote, m1, m2), (m1, m2)


# -- non-local CNOT (cat-entangler / cat-disentangler) ------------------------

def telegate_cnot_send(reg, control: int, epr_local: int) -> int:
    """On the control's node: CNOT(control -> epr half), then Z-measure that half.

    Consumes ``epr_local``; ``control`` survives. Returns the bit for the peer.
    """
    reg.apply_gate([control, epr_local], CNOT)
    return reg.measure(epr_local, _Z_BASIS)


def telegate_cnot_apply(reg, epr_remote: int, target: int, m1: int) -> int:
    """On the target's node: complete the cat copy, apply the CNOT, disentangle.

    X^m1 makes ``epr_remote`` carry the control's basis value; it then controls a
    local CNOT onto ``target``; an X-basis measurement removes it again. Consumes
    ``epr_remote`` and returns the bit the control's node needs.
    """
    if m1:
        reg.apply_pauli(epr_remote, x=1)
    reg.apply_gate([epr_remote, target], CNOT)
    return reg.measure(epr_remote, _X_BASIS)


def telegate_cnot_finish(reg, control: int, m2: int) -> None:
    """On the control's node: Z^m2 clears the phase kickback from the disentangler."""
    if m2:
        reg.apply_pauli(control, z=1)


def telegate_cnot(reg, control: int, target: int,
                  epr_local: int, epr_remote: int) -> tuple[int, int]:
    """All three steps in one call (in-process demo). Returns the 2 classical bits."""
    m1 = telegate_cnot_send(reg, control, epr_local)
    m2 = telegate_cnot_apply(reg, epr_remote, target, m1)
    telegate_cnot_finish(reg, control, m2)
    return m1, m2


# -- deferred corrections (Pauli-frame tracking) ------------------------------

def correct_outcome(bit: int, basis: float, x: int = 0, z: int = 0) -> int:
    """Fold a Pauli correction that arrived *after* the qubit was measured.

    A Pauli correction commutes with a projective measurement up to relabelling the
    outcome: X flips a Z-basis result and leaves an X-basis one alone; Z does the
    reverse. So a late herald is repaired classically, by XOR, with no quantum
    operation at all — the mechanism ``repeater.py`` uses to correct once at the end
    of a chain instead of after every swap.

    ``basis`` is the X-Z-plane angle the qubit was measured at; only the Z (0) and
    X (pi/2) poles have a pure bit-flip relabelling, so anything else is rejected
    rather than silently mis-corrected.
    """
    if abs(basis) < 1e-9:
        flip = x
    elif abs(basis - math.pi / 2) < 1e-9:
        flip = z
    else:
        raise ValueError("a deferred Pauli only relabels outcomes in the Z or X basis")
    return bit ^ (flip & 1)


# -- session runner (mirrors repeater.py: a local demo + the law it must obey) --

@dataclass
class DQCResult:
    """One batch of distributed gates and the Werner law it is measured against."""

    primitive: str            # "teleport" | "telegate"
    backend: str              # "numpy" | "qiskit"
    shots: int                # per error channel; the run measures two channels
    werner_w: float
    bell_fidelity: float      # (1 + 3w)/4
    pairs_consumed: int
    classical_bits: int       # total bits that would cross the link
    bit_error: float          # X-type channel  (Z-basis readout)
    phase_error: float        # Z-type channel  (X-basis readout)
    error_pred: float | None  # (1 - w)/2 — None for a `dropped` control run, where
                              # the Werner law does not describe the outcome
    dropped: str | None       # correction permanently lost ("m1" | "m2")
    late: str | None          # correction delayed past measurement, then folded in


def _register(backend: str, seed: int):
    if backend == "qiskit":
        from .qstate_qiskit import QiskitRegister
        return QiskitRegister(seed=seed)
    if backend != "numpy":
        raise ValueError(f"unknown backend {backend!r} (expected 'numpy' or 'qiskit')")
    from .qstate_core import QStateRegister
    return QStateRegister(seed=seed)


def run_dqc_session(primitive: str = "telegate", shots: int = 2000,
                    fidelity: float = 1.0, backend: str = "numpy",
                    seed: int = 0, dropped: str | None = None,
                    late: str | None = None) -> DQCResult:
    """Run ``shots`` distributed gates per error channel over Werner-w pairs.

    Both Pauli channels are measured for both primitives: the X-type channel from a
    computational-basis input read out in Z, the Z-type channel from a |+> input read
    out in X (a Z error is invisible in the Z basis, so reporting only one channel
    would hide half the failures).

    ``dropped`` withholds a correction bit *permanently* — a control run showing what
    an unrecoverable correction costs. ``late`` withholds it during the gate and then
    folds it into the recorded outcome (:func:`correct_outcome`), which is what a
    resend or a tracked Pauli frame achieves: the result is clean again, so latency
    shows up as memory time rather than error. The two are mutually exclusive.
    """
    if primitive not in ("teleport", "telegate"):
        raise ValueError(f"unknown primitive {primitive!r}")
    if not isinstance(shots, int) or isinstance(shots, bool) or shots < 1:
        raise ValueError(f"shots must be a positive integer, got {shots!r}")
    if not 0.0 <= fidelity <= 1.0:
        raise ValueError(f"fidelity (Werner w) must lie in [0, 1], got {fidelity!r}")
    for name, value in (("dropped", dropped), ("late", late)):
        if value not in (None, "m1", "m2"):
            raise ValueError(f"{name} must be None, 'm1' or 'm2', got {value!r}")
    if dropped is not None and late is not None:
        raise ValueError("a correction bit is either dropped or late, not both")

    reg = _register(backend, seed)
    plus = np.array([1, 1], dtype=complex) / math.sqrt(2)
    withheld = dropped or late
    bit_err = phase_err = 0

    def sent(name: str, value: int) -> int:
        """The bit as the peer sees it: 0 if this one never made it in time."""
        return 0 if withheld == name else value

    for channel in ("bit", "phase"):
        readout = _Z_BASIS if channel == "bit" else _X_BASIS
        errors = 0
        for _ in range(shots):
            epr_a, epr_b = reg.create_bell_pair(fidelity)
            if primitive == "teleport":
                data = reg.alloc(plus if channel == "phase" else None)
                m1, m2 = teleport_send(reg, data, epr_a)
                teleport_recv(reg, epr_b, sent("m1", m1), sent("m2", m2))
                out = reg.measure(epr_b, readout)
                if late:
                    # the missed correction was X^m2 Z^m1; only the Pauli that
                    # anticommutes with the readout basis relabels the outcome
                    out = correct_outcome(out, readout, x=sent("m2", m2) ^ m2,
                                          z=sent("m1", m1) ^ m1)
                errors += out != 0
            else:
                control = reg.alloc(plus if channel == "phase" else None)
                target = reg.alloc()
                m1 = telegate_cnot_send(reg, control, epr_a)
                m2 = telegate_cnot_apply(reg, epr_b, target, sent("m1", m1))
                telegate_cnot_finish(reg, control, sent("m2", m2))
                c_out, t_out = reg.measure(control, readout), reg.measure(target, readout)
                if late:
                    # a missed m1 lands an X on the target; a missed m2 a Z on the control
                    t_out = correct_outcome(t_out, readout, x=sent("m1", m1) ^ m1)
                    c_out = correct_outcome(c_out, readout, z=sent("m2", m2) ^ m2)
                # bit channel: |0>|0> -> target must read 0. phase channel: |+>|0> ->
                # Phi+, whose X-basis outcomes agree.
                errors += (t_out != 0) if channel == "bit" else (c_out != t_out)
        if channel == "bit":
            bit_err = errors
        else:
            phase_err = errors

    # 2 channels x `shots` gates; every gate spends 1 pair and 2 classical bits
    # (teleport: 2 bits A->B; telegate: 1 bit each way)
    gates = 2 * shots
    return DQCResult(
        primitive=primitive,
        backend=backend,
        shots=shots,
        werner_w=fidelity,
        bell_fidelity=(1.0 + 3.0 * fidelity) / 4.0,
        pairs_consumed=gates,
        classical_bits=2 * gates,
        bit_error=bit_err / shots,
        phase_error=phase_err / shots,
        error_pred=None if dropped else (1.0 - fidelity) / 2.0,
        dropped=dropped,
        late=late,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Local distributed-computing demo: spend Bell pairs on a "
                    "teleport or a non-local CNOT and compare the error rate "
                    "against the Werner law (1-w)/2.")
    ap.add_argument("--primitive", choices=["teleport", "telegate"], default="telegate")
    ap.add_argument("--shots", type=int, default=2000)
    ap.add_argument("--fidelity", type=float, default=0.95,
                    help="Werner parameter w of each pair (register knob)")
    ap.add_argument("--backend", choices=["numpy", "qiskit"], default="numpy")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--drop", choices=["m1", "m2"], default=None,
                    help="withhold a correction bit permanently (control run: an "
                         "unrecoverable correction costs half the shots)")
    ap.add_argument("--late", choices=["m1", "m2"], default=None,
                    help="deliver a correction bit after measurement and fold it "
                         "into the outcome (a resend / tracked Pauli frame: the "
                         "result comes back clean, so latency costs memory time)")
    args = ap.parse_args(argv)

    result = run_dqc_session(args.primitive, args.shots, fidelity=args.fidelity,
                             backend=args.backend, seed=args.seed,
                             dropped=args.drop, late=args.late)
    print(json.dumps(asdict(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
