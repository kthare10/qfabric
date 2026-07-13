"""Distributed repeater chain — three real processes over loopback TCP.

The alice process hosts the register and generates both link pairs, the repeater
process performs the Bell-state measurements via RPC and forwards the heralds
over its OWN link to bob, and bob applies the Pauli corrections before measuring
— entanglement swapping with the classical herald traffic on real sockets.
Validated against the same Werner-chain law as the in-process version
(F = (1+3·f^L)/4), plus BBM92 key extraction and the CHSH test end-to-end.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../qne-sequence
F = 0.95
W2 = F ** 2                             # chain Werner parameter, L = 2 links


def _spawn(role: str, port: int, *, pairs=4000, fidelity=F, extra=(),
           seed=None) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_DIR + os.pathsep + env.get("PYTHONPATH", "")
    seeds = {"alice": 1, "bob": 2, "repeater": 3}
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", role, "--protocol", "repeater",
         "--host", "127.0.0.1", "--port", str(port),
         "--num-pairs", str(pairs), "--fidelity", str(fidelity),
         "--sample-fraction", "0.2",
         "--seed", str(seed if seed is not None else seeds[role]), *extra],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _run_chain(port: int, *, pairs=4000, fidelity=F, extra=()) -> dict[str, dict]:
    procs = {r: _spawn(r, port, pairs=pairs, fidelity=fidelity, extra=extra)
             for r in ("bob", "repeater", "alice")}
    results = {}
    try:
        for who, p in procs.items():
            out, err = p.communicate(timeout=90)
            line = next((ln for ln in out.strip().splitlines() if ln.startswith("{")), "")
            assert line, f"no JSON from {who}.\nstdout:\n{out}\nstderr:\n{err}"
            results[who] = json.loads(line)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
    return results


def test_bbm92_key_over_swapped_chain_matches_werner_law():
    r = _run_chain(57311, extra=("--auth-key", "chain-psk"))
    a, b, m = r["alice"], r["bob"], r["repeater"]

    # every attempt swapped once at the middle node; heralds ~uniform
    assert m["swaps"] == 4000
    assert set(m["heralds"]) == {"00", "01", "10", "11"}
    for c in m["heralds"].values():
        assert 0.18 < c / 4000 < 0.32
    assert b["heralds"] == m["heralds"]        # bob got them over the R->B link

    # QBER follows the chain law (1 - f^2)/2 ~ 0.0488
    pred = (1 - W2) / 2
    assert a["qber_pred"] == b["qber_pred"]
    assert abs(a["qber_pred"] - pred) < 1e-12
    assert abs(a["qber"] - pred) < 0.05        # ~400-bit sample, >3sigma
    assert a["qber"] == b["qber"]

    # authenticated links, identical extracted secret (asymptotic PA sizing)
    for who in (a, b, m):
        assert who["authenticated"] is True
        assert who["auth_failures"] == 0
    assert a["reconciled"] and b["reconciled"]
    assert a["key"] == b["key"] is not None
    assert a["secure_key_bits"] == b["secure_key_bits"] > 0

    # real traffic on all three links
    assert m["tx_frames"] > 0 and m["rx_frames"] > 0
    assert a["tx_frames"] > 0 and b["rx_frames"] > 0


def test_finite_key_over_the_chain_needs_a_real_block():
    # At the f=0.95 chain QBER (~4.9%) a ~6k-bit block finite-keys to ZERO
    # (Serfling penalty + measured Cascade leak) — so run the finite-key chain
    # at f=0.97 with 16k pairs, where a positive finite length is achievable.
    f = 0.97
    r = _run_chain(57351, pairs=16000, fidelity=f, extra=("--finite-key",))
    a, b = r["alice"], r["bob"]
    pred = (1 - f ** 2) / 2                    # ~0.0296
    assert abs(a["qber"] - pred) < 0.02
    assert a["reconciled"] and b["reconciled"]
    assert a["finite_key"] == b["finite_key"] is not None
    fk = a["finite_key"]
    assert a["secure_key_bits"] == fk["secret_bits"] > 0
    assert fk["secret_bits"] < fk["asymptotic_bits"]
    assert a["key"] == b["key"] is not None


def test_chsh_violation_survives_the_distributed_swap():
    r = _run_chain(57321, extra=("--chain-mode", "e91"))
    a = r["alice"]
    pred = 2 * math.sqrt(2) * W2               # ~2.55
    assert abs(a["chsh_pred"] - pred) < 1e-12
    assert a["chsh_s"] is not None
    assert abs(a["chsh_s"] - pred) < 0.25
    assert a["chsh_s"] > 2.0                   # Bell violation across 3 processes
    assert a["key"] == r["bob"]["key"] is not None


def test_without_heralded_correction_no_key_survives():
    r = _run_chain(57331, pairs=1500, extra=("--no-correction",))
    a, b = r["alice"], r["bob"]
    assert not a["corrected"] and not b["corrected"]
    assert abs(a["qber"] - 0.5) < 0.1          # even Bell mixture
    assert a["secure_fraction"] == 0.0
    assert not a["reconciled"] and not b["reconciled"]
    assert a["secure_key_bits"] == b["secure_key_bits"] == 0


def test_per_link_loss_gates_delivery():
    # 20 km @ 0.2 dB/km per link -> p_loss ~ 0.602; both links must survive
    r = _run_chain(57341, pairs=3000,
                   extra=("--distance-km", "20", "--attenuation", "0.2"))
    a = r["alice"]
    p_survive = 10 ** (-(0.2 * 20) / 10.0)     # ~0.398
    expected = p_survive ** 2                  # ~0.158
    assert a["attempts"] == 3000
    assert abs(a["delivered"] / 3000 - expected) < 0.03
    assert r["repeater"]["swaps"] == a["delivered"]
    assert a["key"] == r["bob"]["key"] is not None   # loss heralds, not corrupts
