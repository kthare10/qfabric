"""Phase E2 — distributed E91/BBM92 over a real loopback link.

Two runners exchange entanglement-based QKD traffic (basis announcement, remote
measurement RPC, QBER-sample + CHSH disclosure) and produce a shared key. Mirrors
the BB84 two-node smoke test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _spawn(role, name, peer, port, protocol, fidelity, loss_km, seed,
           num_pairs=8000, reconcile=True):
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", name, "--peer", peer,
         "--protocol", protocol, "--host", "127.0.0.1", "--port", str(port),
         "--num-pairs", str(num_pairs), "--fidelity", str(fidelity),
         "--distance-km", str(loss_km), "--attenuation", "0.2",
         "--sample-fraction", "0.2", "--seed", str(seed),
         "--reconcile" if reconcile else "--no-reconcile"],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc, timeout=90.0):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def _run(protocol, fidelity, loss_km, port, reconcile=True):
    bob = _spawn("bob", "bob", "alice", port, protocol, fidelity, loss_km,
                 seed=42, reconcile=reconcile)
    alice = _spawn("alice", "alice", "bob", port, protocol, fidelity, loss_km,
                   seed=42, reconcile=reconcile)
    a = _result(alice)
    b = _result(bob)
    return a, b


def test_distributed_bbm92_lossless_keys_agree():
    a, b = _run("bbm92", 1.0, 0.0, port=57401)
    assert a["key"] is not None and b["key"] is not None
    assert a["qber"] == 0.0
    assert a["sifted_bits"] == b["sifted_bits"]      # both sides agree
    assert a["key"] == b["key"]                       # perfect correlation -> identical
    assert a["chsh_s"] is None                        # bbm92 has no Bell test


def test_distributed_e91_secure_and_bell_violation():
    a, b = _run("e91", 0.98, 1.0, port=57402)         # 1 km @ 0.2 dB/km -> ~4.5% loss
    assert a["key"] is not None and b["key"] is not None
    assert 0.0 <= a["qber"] < 0.03                     # ~ (1-F)/2 = 0.01
    assert a["chsh_s"] is not None and a["chsh_s"] > 2.0
    assert a["secure_fraction"] > 0.0
    assert a["detected_pairs"] < a["num_pairs"]        # loss removed pairs
    assert a["sifted_bits"] == b["sifted_bits"]
    # Cascade reconciliation ran and made the keys match bit-for-bit
    assert a["reconciled"] and b["reconciled"]
    assert a["key"] == b["key"]
    assert a["corrections"] > 0 and a["bits_leaked"] > 0


def test_no_reconcile_leaves_errors_tracking_qber():
    """With --no-reconcile the keys differ only on error positions (mismatch ~ QBER),
    confirming Cascade is what closes that gap."""
    a, b = _run("e91", 0.95, 1.0, port=57403, reconcile=False)  # F=0.95 -> QBER~2.5%
    assert not a["reconciled"]
    assert a["key"] != b["key"]                        # errors remain without Cascade
    mism = bin(a["key"] ^ b["key"]).count("1") / max(a["key_bits"], 1)
    assert 0 < mism < 0.1                              # tracks QBER, not ~50%
