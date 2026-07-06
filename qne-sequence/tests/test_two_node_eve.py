"""Distributed BB84 with an intercept-resend eavesdropper — security demonstration.

A full tap (f=1) drives the sifted QBER to ~25%, past the ~11% threshold, so the
secure key rate collapses and the keys no longer match. With no Eve the channel is
clean. This is the two-process analogue of tests/test_eve.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn(role, name, peer, port, eve_fraction):
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", name, "--peer", peer,
         "--host", "127.0.0.1", "--port", str(port),
         "--num-pulses", "12000", "--fidelity", "1.0",   # no channel noise -> pure Eve
         "--sample-fraction", "0.2", "--seed", "42",
         "--eve-fraction", str(eve_fraction)],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc, timeout=90.0):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def _run(eve_fraction, port):
    bob = _spawn("bob", "bob", "alice", port, eve_fraction)
    alice = _spawn("alice", "alice", "bob", port, eve_fraction)
    return _result(alice), _result(bob)


def test_no_eve_clean_channel():
    a, b = _run(0.0, 57481)
    assert a["qber"] == 0.0
    assert a["secure_fraction"] == 1.0
    assert a["key"] == b["key"]                 # clean -> identical keys


def test_full_intercept_breaks_bb84():
    a, b = _run(1.0, 57482)
    assert a["qber"] > 0.2                       # ~0.25, the intercept-resend signature
    assert a["secure_fraction"] == 0.0           # past the ~11% threshold -> abort
    assert a["key"] != b["key"]                  # disturbance corrupts the key
    assert b["eve_photons_intercepted"] > 0      # Eve stats surfaced in the result
