"""Phase C1 (DESIGN.md §4.3): both photon throughput strategies are correct, and
the benchmark ordering holds (BulkStream > PerPhotonEvent).

The full sweep lives in ../bench_throughput.py; this keeps counts modest for CI.
The raw-socket / 0x7101 / BMv2 data plane (Phase C2) needs Linux/FABRIC and is not
exercised here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIDELITY = 0.95


def _run(mode: str, num_pulses: int, port: int, seed: int = 1) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    phys = ["--fidelity", str(FIDELITY), "--distance-km", "10", "--attenuation", "0.2",
            "--efficiency", "0.8", "--dark-count-rate", "10",
            "--num-pulses", str(num_pulses), "--key-length", "128",
            "--photon-mode", mode]

    def spawn(role, name, peer, sd):
        return subprocess.Popen(
            [sys.executable, "-m", "qne_sequence.node_runner",
             "--role", role, "--name", name, "--peer", peer,
             "--host", "127.0.0.1", "--port", str(port), "--seed", str(sd)] + phys,
            cwd=PKG_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    bob = spawn("bob", "bob", "alice", seed + 1)
    time.sleep(0.4)
    alice = spawn("alice", "alice", "bob", seed)
    try:
        ao, ae = alice.communicate(timeout=60)
        bo, be = bob.communicate(timeout=60)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()
    la = next((ln for ln in ao.strip().splitlines() if ln.startswith("{")), "")
    assert la, f"{mode} alice no result.\n{ao}\n{ae}"
    return json.loads(la)


@pytest.mark.parametrize("mode", ["bulk", "per_event"])
def test_mode_produces_correct_key(mode):
    r = _run(mode, num_pulses=8000, port=57280 + (0 if mode == "bulk" else 1))
    assert r["photon_mode"] == mode
    assert r["key"] is not None
    assert r["remote_access_errors"] == 0
    assert r["sifted_bits"] > 128                       # enough to form a key
    assert 0.0 <= r["qber"] < 0.06                      # near (1-F)/2 = 0.025
    assert 0.0 < r["secure_fraction"] <= 1.0
    assert r["photons_per_s"] > 0


def test_bulk_outperforms_per_event():
    bulk = _run("bulk", num_pulses=30000, port=57284)
    per = _run("per_event", num_pulses=30000, port=57285)
    # BulkStream amortizes per-photon framing/event cost -> strictly higher rate
    # (observed ~3-4x; assert a conservative >1.5x to avoid CI-load flakiness).
    assert bulk["photons_per_s"] > 1.5 * per["photons_per_s"], (
        bulk["photons_per_s"], per["photons_per_s"])
