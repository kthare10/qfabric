"""Phase 6 — distributed quantum computing over a real loopback link.

Two runners execute a gate between QPUs that share no qubit: alice is the control
node and register authority, bob the target node whose half of each gate is an op
over the wire. The corrections cross as real protocol messages (m1 in PLAN, m2 in
ACK) and each side applies the value **as it arrived**, which is what makes
``--dqc-drop`` a genuine control instead of a simulated one.

Mirrors the E91 two-node smoke test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# which Pauli channel each correction bit protects, per primitive. NOT symmetric:
# telegate sends m1 -> X on bob's target and m2 -> Z on alice's control, while a
# teleport correction is X^m2.Z^m1, so m2 is the X and m1 the Z.
PROTECTS = {("telegate", "m1"): "bit", ("telegate", "m2"): "phase",
            ("teleport", "m1"): "phase", ("teleport", "m2"): "bit"}


def _spawn(role, peer, port, *, gates, fidelity, seed, primitive="telegate",
           loss_km=0.0, extra=()):
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", role, "--peer", peer,
         "--protocol", "dqc", "--dqc-primitive", primitive,
         "--host", "127.0.0.1", "--port", str(port),
         "--num-gates", str(gates), "--fidelity", str(fidelity),
         "--distance-km", str(loss_km), "--attenuation", "0.2",
         "--loss", "none" if loss_km == 0.0 else "model",
         "--seed", str(seed), *extra],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc, timeout=120.0):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def _run(port, **kw):
    bob = _spawn("bob", "alice", port, **kw)
    alice = _spawn("alice", "bob", port, **kw)
    return _result(alice), _result(bob)


def test_distributed_telegate_is_exact_over_the_link():
    """A perfect pair and an intact classical channel: the non-local CNOT is exact
    on both Pauli channels, and both processes report the same run."""
    a, b = _run(57601, gates=400, fidelity=1.0, seed=7)
    assert (a["bit_error"], a["phase_error"]) == (0.0, 0.0)
    assert a["gates_completed"] == b["gates_completed"] == 800
    assert a["pairs_consumed"] == 800 and a["classical_bits"] == 1600
    assert a["bit_error"] == b["bit_error"] and a["phase_error"] == b["phase_error"]
    assert a["role"] == 0 and b["role"] == 1
    assert a["tx_frames"] > 0 and b["tx_frames"] > 0


def test_distributed_telegate_entangles_the_two_nodes():
    """The phase channel is the demonstrable part: |+>|0> through the gate leaves
    the two nodes holding |Phi+>, so their X-basis readouts agree exactly. A
    classical copy of a measured bit could not do that (the bit channel alone
    could)."""
    a, _ = _run(57602, gates=600, fidelity=1.0, seed=8)
    assert a["phase_error"] == 0.0


@pytest.mark.parametrize("primitive", ["telegate", "teleport"])
def test_distributed_gate_follows_the_werner_law(primitive):
    a, b = _run(57603 if primitive == "telegate" else 57604,
                gates=4000, fidelity=0.9, seed=11, primitive=primitive)
    assert a["error_pred"] == pytest.approx(0.05)
    assert a["bit_error"] == pytest.approx(0.05, abs=0.02)
    assert a["phase_error"] == pytest.approx(0.05, abs=0.02)
    assert b["bit_error"] == a["bit_error"]


@pytest.mark.parametrize("primitive,bit", [("telegate", "m1"), ("telegate", "m2"),
                                           ("teleport", "m1"), ("teleport", "m2")])
def test_dropping_a_correction_on_the_wire_breaks_exactly_one_channel(primitive, bit):
    """The corrections are load-bearing *as network traffic*: each side applies the
    value that arrived, so withholding one costs half the shots on the single Pauli
    channel it protects and nothing on the other."""
    port = 57610 + 2 * ["m1", "m2"].index(bit) + ["telegate", "teleport"].index(primitive)
    a, _ = _run(port, gates=600, fidelity=1.0, seed=13, primitive=primitive,
                extra=("--dqc-drop", bit))
    assert a["error_pred"] is None                  # the Werner law does not apply
    hit, spared = PROTECTS[(primitive, bit)], (
        "phase" if PROTECTS[(primitive, bit)] == "bit" else "bit")
    assert a[f"{hit}_error"] == pytest.approx(0.5, abs=0.06)
    assert a[f"{spared}_error"] == 0.0


@pytest.mark.parametrize("primitive,bit", [("telegate", "m1"), ("telegate", "m2"),
                                           ("teleport", "m1"), ("teleport", "m2")])
def test_a_late_correction_is_recovered_over_the_link(primitive, bit):
    """The distinction the drop control must not blur: the bit misses the gate, then
    gets folded into the recorded outcome (tracked Pauli frame). Nothing is lost, so
    herald latency is a memory-time cost, not a fidelity cost."""
    port = 57620 + 2 * ["m1", "m2"].index(bit) + ["telegate", "teleport"].index(primitive)
    a, _ = _run(port, gates=500, fidelity=1.0, seed=13, primitive=primitive,
                extra=("--dqc-late", bit))
    assert (a["bit_error"], a["phase_error"]) == (0.0, 0.0)
    assert a["error_pred"] == 0.0                   # nothing was actually lost


def test_channel_loss_costs_gates_not_fidelity():
    """A pair whose transported half is lost yields no gate at all — it must not
    silently degrade the gates that did run."""
    a, b = _run(57630, gates=1500, fidelity=1.0, seed=3, loss_km=10.0)
    assert a["gates_completed"] < a["gates_attempted"]
    assert a["gates_completed"] == b["gates_completed"]
    assert (a["bit_error"], a["phase_error"]) == (0.0, 0.0)


def test_global_timeline_certifies_the_run_at_a_nonzero_delay():
    """Same certificate the other three protocols carry: on the global timeline no
    correction can arrive late whatever the wire does."""
    a, b = _run(57631, gates=300, fidelity=1.0, seed=4, loss_km=100.0,
                extra=("--channel-delay", "auto", "--time-authority", "unused:0"))
    for side in (a, b):
        assert side["lookahead"]["applicable"] is True      # nonzero modeled delay
        assert side["lookahead"]["late_events"] == 0
        assert side["lookahead"]["certified"] is True
        assert side["timeline"] == "logical"
        assert side["sim_elapsed_ps"] > 0
    assert (a["bit_error"], a["phase_error"]) == (0.0, 0.0)


def test_authenticated_channel_carries_the_corrections():
    a, _ = _run(57632, gates=300, fidelity=1.0, seed=4, extra=("--auth-key", "s3cret"))
    assert a["authenticated"] and a["auth_failures"] == 0
    assert (a["bit_error"], a["phase_error"]) == (0.0, 0.0)
