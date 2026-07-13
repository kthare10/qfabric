"""End-to-end: authenticated classical channel + finite-key PA over loopback.

Same two-process harness as test_two_node_bb84.py, with --auth-key on both sides
(every classical frame HMAC-tagged and sequence-checked) and --finite-key (the
Toeplitz output sized by the finite-key bound instead of the asymptotic fraction).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../qne-sequence
PORT = 57241
KEY_LENGTH = 512


def _spawn(role: str, name: str, peer: str, seed: int, extra: list[str]) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", name, "--peer", peer,
         "--host", "127.0.0.1", "--port", str(PORT),
         "--key-length", str(KEY_LENGTH), "--seed", str(seed),
         "--num-pulses", "6000", "--fidelity", "0.97", *extra],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc: subprocess.Popen, timeout: float):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def test_two_node_bb84_authenticated_finite_key():
    extra = ["--auth-key", "test-psk", "--finite-key"]
    bob = _spawn("bob", "bob", "alice", seed=2, extra=extra)      # listener first
    alice = _spawn("alice", "alice", "bob", seed=1, extra=extra)
    try:
        ra = _result(alice, timeout=60)
        rb = _result(bob, timeout=60)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()

    # every classical frame crossed authenticated, none failed
    for r in (ra, rb):
        assert r["authenticated"] is True, r
        assert r["auth_failures"] == 0, r
        assert r["remote_access_errors"] == 0, r

    # reconciled + amplified: identical extracted secret on both sides
    assert ra["reconciled"] and rb["reconciled"]
    assert ra["key"] == rb["key"]

    # PA output was sized by the finite-key bound and both sides agree on it
    assert ra["finite_key"] is not None and rb["finite_key"] is not None
    assert ra["finite_key"] == rb["finite_key"]
    fk = ra["finite_key"]
    assert ra["secure_key_bits"] == fk["secret_bits"] > 0
    assert ra["key"] is not None
    assert fk["secret_bits"] <= fk["asymptotic_bits"]
    assert fk["qber_upper"] > ra["qber"]
