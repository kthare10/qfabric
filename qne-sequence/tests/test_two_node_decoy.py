"""End-to-end decoy-state BB84 on the live (descriptor-on-wire) transport.

Alice's weak-coherent source draws Poisson(μ) photons per pulse at three
intensities; fiber loss thins them per photon; Bob's detector fires on
1 − (1−η)^n. The measured per-intensity gains and error rates — not analytic
formulas — feed the Lo–Ma–Chen Y1/e1 bounds and the GLLP rate, closing the
"decoy is analysis-only" gap: the whole decoy pipeline now runs on live traffic.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../qne-sequence
PORT = 57261

DIST_KM, ATTEN = 10.0, 0.2          # p_loss = 1 - 10^(-0.2) ~ 0.369
EFF = 0.8                            # eta_total = 0.8 * 0.631 ~ 0.505
MU_S, MU_D = 0.6, 0.1


def _spawn(role: str, name: str, peer: str, seed: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", name, "--peer", peer,
         "--host", "127.0.0.1", "--port", str(PORT),
         "--key-length", "64", "--seed", str(seed),
         "--num-pulses", "20000", "--fidelity", "0.98",
         "--distance-km", str(DIST_KM), "--attenuation", str(ATTEN),
         "--efficiency", str(EFF),
         "--decoy", "--mu-signal", str(MU_S), "--mu-decoy", str(MU_D)],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _result(proc: subprocess.Popen, timeout: float):
    out, err = proc.communicate(timeout=timeout)
    line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
    assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
    return json.loads(line)


def test_two_node_decoy_bb84():
    bob = _spawn("bob", "bob", "alice", seed=2)      # listener first
    alice = _spawn("alice", "alice", "bob", seed=1)
    try:
        ra = _result(alice, timeout=90)
        rb = _result(bob, timeout=90)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()

    d = ra["decoy"]
    assert d is not None, ra
    assert rb["decoy"] == d                    # Alice's analysis echoed to Bob

    # measured gains ordered by intensity and near the analytic weak-coherent gain
    g = d["gains"]
    assert g["signal"] > g["decoy"] > g["vacuum"] >= 0.0
    p_loss = 1.0 - 10 ** (-(ATTEN * DIST_KM) / 10.0)
    eta_tot = EFF * (1.0 - p_loss)
    assert abs(g["signal"] - (1 - math.exp(-eta_tot * MU_S))) < 0.03
    assert abs(g["decoy"] - (1 - math.exp(-eta_tot * MU_D))) < 0.02

    # Lo-Ma-Chen bounds on live data: single-photon yield certified, low e1
    assert d["Y1_lower"] > 0.0
    assert d["e1_upper"] < 0.15
    assert d["secure_key_rate"] > 0.0
    assert d["decoy_key_bits"] > 0

    # the signal-pulse key still reconciles to an identical secret
    assert ra["reconciled"] and rb["reconciled"]
    assert ra["key"] == rb["key"]
    assert ra["secure_key_bits"] > 0
