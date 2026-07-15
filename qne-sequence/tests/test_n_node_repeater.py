"""N-node repeater chains — multiple stations, real processes over loopback.

Generalizes the 3-node tests: K stations (K+2 processes, K+1 links, 2K+1 TCP
connections). Each station swaps its own segment and heralds to Bob, who
XOR-composes the Pauli corrections. Validated against the Werner-chain law
with L = K+1.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../qne-sequence


def _spawn(role: str, port: int, k: int, *, pairs, fidelity, extra=(), seed=0,
           station_index=1) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", f"{role}{station_index if role=='repeater' else ''}",
         "--protocol", "repeater", "--host", "127.0.0.1",
         "--port", str(port), "--num-stations", str(k),
         "--station-index", str(station_index),
         "--num-pairs", str(pairs), "--fidelity", str(fidelity),
         "--sample-fraction", "0.2", "--seed", str(seed), *extra],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _run_chain(port: int, k: int, *, pairs=3000, fidelity=0.95, extra=()):
    procs = {"bob": _spawn("bob", port, k, pairs=pairs, fidelity=fidelity,
                           extra=extra, seed=2)}
    for i in range(1, k + 1):
        procs[f"st{i}"] = _spawn("repeater", port, k, pairs=pairs,
                                 fidelity=fidelity, extra=extra, seed=2 + i,
                                 station_index=i)
    procs["alice"] = _spawn("alice", port, k, pairs=pairs, fidelity=fidelity,
                            extra=extra, seed=1)
    results = {}
    try:
        for who, p in procs.items():
            out, err = p.communicate(timeout=120)
            line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
            assert line, f"no JSON from {who}.\nstdout:\n{out}\nstderr:\n{err}"
            results[who] = json.loads(line)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
    return results


def test_four_node_chain_follows_the_l3_law():
    f = 0.95
    r = _run_chain(57511, k=2, pairs=4000, fidelity=f)
    a, b = r["alice"], r["bob"]

    assert a["num_nodes"] == 4 and a["num_links"] == 3 and a["num_stations"] == 2
    pred = (1 - f ** 3) / 2                     # ~0.0713
    assert abs(a["qber_pred"] - pred) < 1e-12
    assert abs(a["qber"] - pred) < 0.05

    # every station swapped every delivered attempt; bob composed both streams
    for i in (1, 2):
        st = r[f"st{i}"]
        assert st["swaps"] == 4000
        assert st["station_index"] == i
    assert len(b["heralds_per_station"]) == 2
    assert sum(b["heralds"].values()) == 8000
    assert a["swaps"] == 8000

    # identical extracted secret across the 4-node chain
    assert a["reconciled"] and b["reconciled"]
    assert a["key"] == b["key"] is not None
    assert a["secure_key_bits"] == b["secure_key_bits"] > 0


def test_five_node_chain_chsh_violation():
    # 3 stations, f=0.97: w = 0.97^4 ~ 0.885 -> S ~ 2.50 — violation with margin
    f = 0.97
    r = _run_chain(57531, k=3, pairs=6000, fidelity=f, extra=("--chain-mode", "e91"))
    a = r["alice"]
    assert a["num_links"] == 4
    pred = 2 * math.sqrt(2) * f ** 4
    assert abs(a["chsh_pred"] - pred) < 1e-12
    assert a["chsh_s"] is not None
    assert abs(a["chsh_s"] - pred) < 0.25
    assert a["chsh_s"] > 2.0                    # Bell violation across 5 processes
    assert a["key"] == r["bob"]["key"] is not None


def test_four_node_without_heralds_is_noise():
    r = _run_chain(57551, k=2, pairs=1500, fidelity=1.0, extra=("--no-correction",))
    a = r["alice"]
    assert abs(a["qber"] - 0.5) < 0.1           # even Bell mixture
    assert a["secure_fraction"] == 0.0
    assert not a["reconciled"]
