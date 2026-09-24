# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
"""The global timeline: runs are exact regardless of wall-clock latency.

Two mechanisms, one contract (every message delivered at exactly t_send + delay in
simulation time, so nothing can be late):

* **BB84's protocol phase** free-runs on a local event queue (photon emission,
  detector events), so it needs LBTS grants from the central time authority.
* **BB84 post-processing, E91 and the repeater chain** never free-run -- every action
  waits on a receive -- so the shared per-node logical clock alone is enough and no
  coordinator process is involved.

Two node_runner processes drive BB84 over loopback with a modeled channel delay of
1 ns -- orders of magnitude below the real per-frame latency of the Python stack.
Under the wall-clock timeline such a delay is unmeetable (frames arrive "late").
Under the authority the clock is logical: every frame is delivered at exactly
t_send + delay and late_events is zero by construction.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

from qne_sequence.time_authority import TimeAuthority, TimeClient

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT_DIR = os.path.dirname(PKG_DIR)


def _spawn(role, name, peer, port, seed, extra):
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + ROOT_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner", "--role", role, "--name", name,
         "--peer", peer, "--host", "127.0.0.1", "--port", str(port), "--key-length", "128",
         "--seed", str(seed), "--num-pulses", "4000", "--fidelity", "0.97",
         "--distance-km", "5", "--attenuation", "0.2", *extra],
        cwd=PKG_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _result(proc, timeout=90):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def _run_pair(port, extra):
    bob = _spawn("bob", "bob", "alice", port, 2, extra)
    alice = _spawn("alice", "alice", "bob", port, 1, extra)
    try:
        return _result(alice), _result(bob)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()


def test_authority_run_is_exact_at_unmeetable_delay():
    ta = TimeAuthority("127.0.0.1", 0, num_nodes=2).start()
    try:
        extra = ["--channel-delay", "1000",                       # 1 ns
                 "--time-authority", f"127.0.0.1:{ta.port}"]
        ra, rb = _run_pair(57601, extra)
    finally:
        ta.stop()
    for r in (ra, rb):
        assert r["timeline"] == "authority", r
        assert r["remote_access_errors"] == 0
        assert r["lookahead"]["applicable"] is True
        assert r["lookahead"]["late_events"] == 0, r["lookahead"]
        assert r["lookahead"]["certified"] is True, r["lookahead"]
        assert r["authority"]["rounds"] >= 1
        assert not r["authority"]["timed_out"]
        # the certificate must cover the POST-PROCESSING phase too, not just the
        # handful of protocol-timeline frames: Cascade is ~one round trip per
        # block, so a reconciled run checks far more than the ~5 BB84 messages
        checked = r["lookahead"]["on_time_events"] + r["lookahead"]["late_events"]
        assert checked > 20, (checked, r["lookahead"])
        # and that phase costs deterministic simulation time (N round trips x 2 x delay)
        assert r["postprocess_sim_ps"] > 0, r["postprocess_sim_ps"]
    assert ra["postprocess_sim_ps"] == rb["postprocess_sim_ps"]
    assert ra["reconciled"] and rb["reconciled"]
    assert ra["key"] == rb["key"] is not None
    assert ra["qber"] == rb["qber"] and 0.0 <= ra["qber"] < 0.05
    assert ta.grants >= 1


def test_wall_clock_timeline_cannot_meet_the_same_delay():
    # the control: same run, wall-clock timeline -> the 1 ns lookahead is missed
    ra, rb = _run_pair(57611, ["--channel-delay", "1000"])
    assert ra["timeline"] == "wall-clock"
    assert rb["lookahead"]["late_events"] > 0
    assert rb["lookahead"]["certified"] is False
    # same coverage, different verdict: the wall-clock mode checks the same frames
    # (protocol timeline + Cascade) and misses the 1 ns deadline on them
    checked = rb["lookahead"]["on_time_events"] + rb["lookahead"]["late_events"]
    assert checked > 20, (checked, rb["lookahead"])
    assert rb["postprocess_sim_ps"] == 0     # wall-clock mode consumes no sim time


def test_authority_lbts_never_exceeds_in_flight_arrival():
    """Direct protocol check: while a message is in flight (sent > recv) the
    authority refuses to grant and asks everyone to report again."""
    from qne_sequence.time_authority import REREPORT
    ta = TimeAuthority("127.0.0.1", 0, num_nodes=2).start()
    try:
        a = TimeClient("127.0.0.1", ta.port, "a", lookahead_ps=100).connect()
        b = TimeClient("127.0.0.1", ta.port, "b", lookahead_ps=100).connect()
        got = {}

        def run_b():
            # b idle; it "receives" a's message only on its second report
            r = b.request(None, 0, 0)
            assert r == REREPORT
            got["b"] = b.request(None, 0, 1)

        t = threading.Thread(target=run_b)
        t.start()
        # a: next event at 50, one message sent that b has not received yet
        r = a.request(50, 1, 0)
        assert r == REREPORT
        lbts = a.request(50, 1, 0)
        t.join(timeout=10)
        assert lbts == 150 and got["b"] == 150       # min(50 + 100, inf)
        assert ta.rereports >= 1
        a.bye()
        b.bye()
    finally:
        ta.stop()


@pytest.mark.parametrize("n", [3])
def test_authority_global_end_when_all_idle(n):
    ta = TimeAuthority("127.0.0.1", 0, num_nodes=n).start()
    try:
        clients = [TimeClient("127.0.0.1", ta.port, f"n{i}", 10).connect() for i in range(n)]
        out = [None] * n
        ths = [threading.Thread(target=lambda i=i: out.__setitem__(
            i, clients[i].request(None, 0, 0))) for i in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=10)
        assert out == [None] * n                     # everyone idle -> run finished
    finally:
        ta.stop()


# --- protocols that need no coordinator: E91 and the repeater chain ---------------

def _e91_pair(port, delay_ps, logical):
    extra = ["--protocol", "bbm92", "--num-pairs", "1200", "--fidelity", "0.97",
             "--sample-fraction", "0.2", "--channel-delay", str(delay_ps)]
    if logical:
        extra += ["--time-authority", "unused:0"]   # endpoint unused: no grants needed
    bob = _spawn("bob", "bob", "alice", port, 2, extra)
    alice = _spawn("alice", "alice", "bob", port, 1, extra)
    try:
        return _result(alice), _result(bob)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()


def _chain(port, delay_ps, logical):
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + ROOT_DIR + os.pathsep + env.get("PYTHONPATH", "")
    extra = ["--time-authority", "unused:0"] if logical else []
    seeds = {"alice": 1, "bob": 2, "repeater": 3}
    procs = {}
    for role in ("bob", "repeater", "alice"):
        procs[role] = subprocess.Popen(
            [sys.executable, "-m", "qne_sequence.node_runner", "--role", role,
             "--name", role, "--protocol", "repeater", "--host", "127.0.0.1",
             "--port", str(port), "--num-pairs", "1200", "--fidelity", "0.97",
             "--sample-fraction", "0.2", "--seed", str(seeds[role]),
             "--channel-delay", str(delay_ps), *extra],
            cwd=PKG_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = {}
    try:
        for who, p in procs.items():
            out[who] = _result(p, timeout=180)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
    return out


_UNMEETABLE_PS = 1_000_000          # 1 us modeled delay: below real message latency


def test_e91_logical_clock_is_exact_at_unmeetable_delay():
    ra, rb = _e91_pair(57621, _UNMEETABLE_PS, logical=True)
    for r in (ra, rb):
        assert r["timeline"] == "logical", r["timeline"]
        assert r["lookahead"]["late_events"] == 0, r["lookahead"]
        assert r["lookahead"]["certified"] is True, r["lookahead"]
        assert r["lookahead"]["on_time_events"] > 20, r["lookahead"]
        assert r["sim_elapsed_ps"] > 0
    assert ra["key"] == rb["key"] is not None

    # same run on the wall clock: same physics, but the deadline is unmeetable
    wa, wb = _e91_pair(57631, _UNMEETABLE_PS, logical=False)
    assert wa["timeline"] == "wall-clock"
    assert wb["lookahead"]["late_events"] > 0
    assert wb["lookahead"]["certified"] is False
    assert wa["sim_elapsed_ps"] == 0
    for k in ("qber", "sifted_bits", "key_bits", "num_sampled", "detected_pairs"):
        assert ra[k] == wa[k], (k, ra[k], wa[k])


def test_repeater_chain_logical_clock_is_exact_at_unmeetable_delay():
    r = _chain(57641, _UNMEETABLE_PS, logical=True)
    for who in ("alice", "repeater", "bob"):
        lk = r[who]["lookahead"]
        assert r[who]["timeline"] == "logical", (who, r[who]["timeline"])
        assert lk["late_events"] == 0, (who, lk)
        assert lk["certified"] is True, (who, lk)
        assert lk["on_time_events"] > 0, (who, lk)
    # a station serves TWO links (Alice-side and Bob-side) off ONE node clock
    assert r["repeater"]["sim_elapsed_ps"] > 0
    assert r["alice"]["key"] == r["bob"]["key"] is not None
    assert r["repeater"]["swaps"] == r["alice"]["delivered"]

    w = _chain(57651, _UNMEETABLE_PS, logical=False)
    assert w["bob"]["lookahead"]["late_events"] > 0
    assert w["bob"]["lookahead"]["certified"] is False
    for k in ("qber", "sifted_bits", "key_bits", "delivered", "swaps"):
        assert r["alice"][k] == w["alice"][k], (k, r["alice"][k], w["alice"][k])
