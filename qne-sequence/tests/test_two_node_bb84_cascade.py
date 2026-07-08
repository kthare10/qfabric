"""Cascade reconciliation on the distributed BB84 path.

With a noisy channel (F<1) the sifted keys differ on the error bits. Cascade,
driven over the same TCP link after the protocol's timeline finishes, corrects
Bob's key to match Alice's bit-for-bit; --no-reconcile leaves the errors in place.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn(role, peer, port, reconcile):
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", role, "--peer", peer,
         "--host", "127.0.0.1", "--port", str(port),
         "--num-pulses", "12000", "--fidelity", "0.95",   # ~2.5% QBER → real errors
         "--sample-fraction", "0.2", "--seed", "7",
         "--reconcile" if reconcile else "--no-reconcile"],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc, timeout=90.0):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def _run(reconcile, port):
    bob = _spawn("bob", "alice", port, reconcile)
    alice = _spawn("alice", "bob", port, reconcile)
    return _result(alice), _result(bob)


def test_cascade_makes_bb84_keys_match():
    a, b = _run(reconcile=True, port=57711)
    assert a["qber"] > 0.0                       # noisy channel → real errors
    assert a["reconciled"] and b["reconciled"]
    assert a["key"] == b["key"]                  # corrected bit-for-bit
    assert a["corrections"] > 0 and a["bits_leaked"] > 0
    # privacy amplification extracts a shorter secret than the reconciled key
    assert 0 < a["secure_key_bits"] < a["key_bits"]


def test_no_reconcile_leaves_bb84_errors():
    a, b = _run(reconcile=False, port=57712)
    assert not a["reconciled"]
    assert a["key"] != b["key"]                  # errors remain
    mism = bin(a["key"] ^ b["key"]).count("1") / max(a["key_bits"], 1)
    assert 0 < mism < 0.1                        # tracks QBER, not ~50%
    assert a["secure_key_bits"] == 0             # no reconciliation → no distilled key
