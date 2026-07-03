"""Phase A exit criterion (DESIGN.md §11):

1. Two runners on loopback exchange BB84 control messages and produce a shared key.
2. GuardedRemoteStub passes with zero RemoteAccessErrors (every `another` access in
   DistributedBB84 has been converted to a message).
3. Sanity: the guard genuinely fires on *stock* BB84 (the §8.1 violation is real).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../qne-sequence
PORT = 57231
KEY_LENGTH = 128


def _spawn(role: str, name: str, peer: str, seed: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", name, "--peer", peer,
         "--host", "127.0.0.1", "--port", str(PORT),
         "--key-length", str(KEY_LENGTH), "--seed", str(seed)],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc: subprocess.Popen, timeout: float):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def test_two_node_bb84_over_loopback():
    bob = _spawn("bob", "bob", "alice", seed=2)      # listener first
    alice = _spawn("alice", "alice", "bob", seed=1)
    try:
        ra = _result(alice, timeout=45)
        rb = _result(bob, timeout=45)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()

    # (1) both sides produced a key
    assert ra["key"] is not None, f"alice produced no key: {ra}"
    assert rb["key"] is not None, f"bob produced no key: {rb}"

    # (2) zero illegal cross-process accesses — the §8.1 proof
    assert ra["remote_access_errors"] == 0, ra
    assert rb["remote_access_errors"] == 0, rb

    # real traffic crossed the wire in both directions
    assert ra["tx_frames"] > 0 and ra["rx_frames"] > 0, ra

    # lossless stub channel => Alice and Bob agree on the key
    assert ra["key"] == rb["key"], f"key mismatch: alice={ra['key']} bob={rb['key']}"


def test_guard_catches_stock_bb84_violation():
    """Stock BB84.push reaches into another.key_lengths — the guard must catch it."""
    from sequence.kernel.timeline import Timeline
    from sequence.topology.node import QKDNode
    from qne_sequence.guarded_stub import GuardedRemoteStub, RemoteAccessError

    tl = Timeline(1e12)
    node = QKDNode("solo", tl, stack_size=1)
    bb84 = node.protocol_stack[0]          # genuine, unmodified sequence.qkd.BB84
    bb84.role = 0
    bb84.another = GuardedRemoteStub("peer.BB84", "peer")

    with pytest.raises(RemoteAccessError):
        bb84.push(KEY_LENGTH, 1)
