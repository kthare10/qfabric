"""Phase B exit criterion (DESIGN.md §11):

A real key is produced over the wire, and the **QBER** — now estimated by sample
disclosure over the classical channel (no peer-secret-key read) — matches an
in-process reference within statistical noise on the same physics.

The distributed emulator (two subprocesses, lossy descriptor-on-wire channel, qfabric
Detector at Bob) is compared against an in-process reference that runs the same
qfabric physics directly. Both must agree with each other and with the analytical
intrinsic QBER (1-F)/2.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types

import numpy as np

from qne.detector import Detector

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# physics for the QBER test (chosen for a clearly non-zero, stable QBER)
FIDELITY = 0.95          # intrinsic QBER ~ (1-F)/2 = 0.025
DISTANCE_KM = 10.0
ATTENUATION = 0.2        # dB/km
EFFICIENCY = 0.8
DARK = 10.0
NUM_PULSES = 30000
SAMPLE_FRACTION = 0.2
KEY_LENGTH = 128
SEEDS = (1, 2, 3)

_LOSS = 1.0 - 10 ** (-(ATTENUATION * DISTANCE_KM) / 10.0)


def _reference_qber(seed: int) -> float:
    """Full-population QBER from the same qfabric physics, in one process.

    Mirrors the runner's sub-seeding: alice bits=seed, detector=seed+1, loss=seed+2.
    """
    alice = np.random.default_rng(seed)
    loss = np.random.default_rng(seed + 2)
    det = Detector(efficiency=EFFICIENCY, dark_count_rate=DARK,
                   polarization_error=1.0 - FIDELITY, seed=seed + 1)
    a_basis = alice.integers(0, 2, NUM_PULSES)
    a_bit = alice.integers(0, 2, NUM_PULSES)
    errors = sifted = 0
    for i in range(NUM_PULSES):
        if loss.random() < _LOSS:
            continue                                   # fiber loss
        ev = det.detect(types.SimpleNamespace(
            basis=int(a_basis[i]), state=int(a_bit[i]), sequence_num=i))
        if not ev.detected:
            continue
        if ev.basis == a_basis[i]:                     # sifted
            sifted += 1
            if ev.bit_value != a_bit[i]:
                errors += 1
    return errors / sifted if sifted else 0.0


def _run_distributed(seed: int, port: int) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    phys = ["--fidelity", str(FIDELITY), "--distance-km", str(DISTANCE_KM),
            "--attenuation", str(ATTENUATION), "--efficiency", str(EFFICIENCY),
            "--dark-count-rate", str(DARK), "--num-pulses", str(NUM_PULSES),
            "--sample-fraction", str(SAMPLE_FRACTION), "--key-length", str(KEY_LENGTH)]

    def spawn(role, name, peer):
        return subprocess.Popen(
            [sys.executable, "-m", "qne_sequence.node_runner",
             "--role", role, "--name", name, "--peer", peer,
             "--host", "127.0.0.1", "--port", str(port), "--seed", str(seed)] + phys,
            cwd=PKG_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    import time
    bob = spawn("bob", "bob", "alice")
    time.sleep(0.4)
    alice = spawn("alice", "alice", "bob")
    try:
        ao, ae = alice.communicate(timeout=60)
        bo, be = bob.communicate(timeout=60)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()
    la = next((ln for ln in ao.strip().splitlines() if ln.startswith("{")), "")
    lb = next((ln for ln in bo.strip().splitlines() if ln.startswith("{")), "")
    assert la, f"alice no result.\n{ao}\n{ae}"
    assert lb, f"bob no result.\n{bo}\n{be}"
    return {"alice": json.loads(la), "bob": json.loads(lb)}


def test_qber_matches_reference_within_noise():
    ref = [_reference_qber(s) for s in SEEDS]
    dist = [_run_distributed(s, 57250 + s) for s in SEEDS]

    ref_mean = float(np.mean(ref))
    dist_qber = [d["alice"]["qber"] for d in dist]
    dist_mean = float(np.mean(dist_qber))

    # every run produced a key with no cross-process state access
    for d in dist:
        assert d["alice"]["key"] is not None and d["bob"]["key"] is not None
        assert d["alice"]["remote_access_errors"] == 0
        assert d["bob"]["remote_access_errors"] == 0
        assert d["alice"]["qber"] == d["bob"]["qber"]          # disclosed value agrees
        assert 0.0 < d["alice"]["secure_fraction"] <= 1.0      # below 11% threshold

    # distributed QBER agrees with the in-process reference and the analytical value
    analytical = (1.0 - FIDELITY) / 2.0
    assert abs(dist_mean - ref_mean) < 0.01, (dist_mean, ref_mean)
    assert abs(dist_mean - analytical) < 0.01, (dist_mean, analytical)


def test_lossless_perfect_channel_qber_is_zero():
    """Sanity: ideal physics -> ~zero QBER and identical keys (defaults)."""
    res = _run_distributed_ideal(57260)
    assert res["alice"]["qber"] == 0.0
    assert res["alice"]["key"] == res["bob"]["key"]            # no errors -> keys match
    assert res["alice"]["remote_access_errors"] == 0


def _run_distributed_ideal(port: int) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")

    def spawn(role, name, peer):
        return subprocess.Popen(
            [sys.executable, "-m", "qne_sequence.node_runner",
             "--role", role, "--name", name, "--peer", peer,
             "--host", "127.0.0.1", "--port", str(port),
             "--seed", "5", "--key-length", "128", "--num-pulses", "4000"],
            cwd=PKG_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    import time
    bob = spawn("bob", "bob", "alice")
    time.sleep(0.4)
    alice = spawn("alice", "alice", "bob")
    try:
        ao, _ = alice.communicate(timeout=30)
        bo, _ = bob.communicate(timeout=30)
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()
    return {"alice": json.loads(ao.strip().splitlines()[-1]),
            "bob": json.loads(bo.strip().splitlines()[-1])}
