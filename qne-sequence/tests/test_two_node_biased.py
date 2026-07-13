"""End-to-end efficient (biased-basis) BB84 over loopback.

With P(Z) = p on both sides the sift ratio is p² + (1−p)² > 1/2 — the whole point
of efficient BB84 — while the key comes from Z–Z matches only and ALL X–X matches
are disclosed to estimate the phase error (secure fraction 1 − h(e_z) − h(e_x)).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../qne-sequence
PORT = 57251
BIAS = 0.9


def _spawn(role: str, name: str, peer: str, seed: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", name, "--peer", peer,
         "--host", "127.0.0.1", "--port", str(PORT),
         "--key-length", "256", "--seed", str(seed),
         "--num-pulses", "8000", "--fidelity", "0.98",
         "--basis-bias", str(BIAS)],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc: subprocess.Popen, timeout: float):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def test_two_node_biased_bb84():
    bob = _spawn("bob", "bob", "alice", seed=2)      # listener first
    alice = _spawn("alice", "alice", "bob", seed=1)
    try:
        ra = _result(alice, timeout=60)
        rb = _result(bob, timeout=60)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()

    # sift efficiency beats the unbiased 50% and matches p^2 + (1-p)^2
    expected_ratio = BIAS ** 2 + (1 - BIAS) ** 2      # 0.82
    assert rb["sift_ratio"] is not None
    assert abs(rb["sift_ratio"] - expected_ratio) < 0.03
    assert rb["sift_ratio"] > 0.5

    # both sides report the phase-error estimate and a sane secure fraction
    for r in (ra, rb):
        assert r["basis_bias"] == BIAS
        assert r["qber_x"] is not None
        assert 0 < r["secure_fraction"] <= 1.0

    # reconciled: identical extracted secret from the Z-basis key
    assert ra["reconciled"] and rb["reconciled"]
    assert ra["key"] == rb["key"]
    assert ra["secure_key_bits"] > 0
