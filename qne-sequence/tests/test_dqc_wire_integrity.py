"""The distributed gate must depend on the bits that actually crossed the link.

``test_two_node_dqc.py`` shows the protocol works when the channel is honest. This
file attacks the claim from the other side: it runs ``_run_control`` / ``_run_target``
against an in-memory RPC pair that **corrupts a correction bit in flight**, and
asserts the run notices.

The failure mode being guarded is specific and was real: if the control node folds a
deferred correction from its own local copy of m1/m2 rather than from the value the
peer reported receiving, then a corrupted wire bit gets silently repaired out of
clean local state and ``--dqc-late`` reports a perfect recovery that never happened.
A late correction may only ever be folded from the bits the link delivered.
"""

from __future__ import annotations

import queue
from concurrent.futures import ThreadPoolExecutor

import pytest

from qne_sequence.distributed_dqc import _run_control, _run_target

GATES = 200


class _FakeRpc:
    """One endpoint of an in-memory RPC pair, with an optional in-flight mangler."""

    def __init__(self, inbox, outbox, corrupt=None):
        self._in, self._out, self._corrupt = inbox, outbox, corrupt

    def send(self, kind, body):
        self._out.put((kind, self._corrupt(kind, body) if self._corrupt else body))

    def recv(self, expected, timeout=60.0):
        kind, body = self._in.get(timeout=timeout)
        assert kind == expected, f"expected {expected}, got {kind}"
        return body

    def call(self, kind, body, expected, timeout=60.0):
        self.send(kind, body)
        return self.recv(expected, timeout=timeout)


def _flip(field):
    """Invert every bit of ``field`` in one message kind as it leaves the sender."""
    def mangle(kind, body):
        if field in body and kind in ("PLAN", "ACK"):
            return {**body, field: [1 - int(v) for v in body[field]]}
        return body
    return mangle


def _session(primitive="telegate", *, late=None, dropped=None,
             corrupt_at=None, corrupt=None, fidelity=1.0):
    """Run both roles over the fake pair; ``corrupt_at`` is 'control' or 'target'."""
    a2b, b2a = queue.Queue(), queue.Queue()
    control = _FakeRpc(b2a, a2b, corrupt if corrupt_at == "control" else None)
    target = _FakeRpc(a2b, b2a, corrupt if corrupt_at == "target" else None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_b = pool.submit(_run_target, target, primitive, dropped, late)
        fut_a = pool.submit(_run_control, control, GATES, primitive, fidelity,
                            0.0, 5, dropped, late)
        return fut_a.result(timeout=120), fut_b.result(timeout=120)


def test_harness_reproduces_a_clean_run():
    a, b = _session()
    assert (a["bit_error"], a["phase_error"]) == (0.0, 0.0)
    assert a == b                               # both sides report the same summary


@pytest.mark.parametrize("late", [None, "m2"])
def test_corrupting_m2_on_the_wire_breaks_the_phase_channel(late):
    """ACK carries m2 back to the control node. Inverting it must break the phase
    channel whether or not the correction was deferred — with ``late='m2'`` the
    fold has to use the corrupted value it received, not a pristine local copy."""
    a, _ = _session(late=late, corrupt_at="target", corrupt=_flip("m2"))
    assert a["phase_error"] == 1.0
    assert a["bit_error"] == 0.0                # m2 protects only the phase channel


@pytest.mark.parametrize("late", [None, "m1"])
def test_corrupting_m1_on_the_wire_breaks_the_bit_channel(late):
    """PLAN carries m1 to the target node; the mirror-image assertion."""
    a, _ = _session(late=late, corrupt_at="control", corrupt=_flip("m1"))
    assert a["bit_error"] == 1.0
    assert a["phase_error"] == 0.0


@pytest.mark.parametrize("late", [None, "m1", "m2"])
def test_teleport_corruption_is_not_repaired_from_local_state(late):
    """Teleport folds both Paulis on bob's qubit, so a corrupted PLAN must show up
    on the channel that Pauli protects (m2 -> X -> bit, m1 -> Z -> phase)."""
    field = "m2" if late == "m2" else "m1"
    a, _ = _session("teleport", late=late, corrupt_at="control", corrupt=_flip(field))
    broken = "bit_error" if field == "m2" else "phase_error"
    assert a[broken] == 1.0


def test_an_honest_wire_still_recovers_a_late_bit():
    """The control: with nothing corrupted, deferring either bit recovers exactly.
    Without this the tests above would pass on a protocol that simply never
    recovers anything."""
    for primitive in ("telegate", "teleport"):
        for late in ("m1", "m2"):
            a, _ = _session(primitive, late=late)
            assert (a["bit_error"], a["phase_error"]) == (0.0, 0.0), (primitive, late)
