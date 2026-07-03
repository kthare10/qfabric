#!/usr/bin/env python
"""Phase C throughput benchmark (DESIGN.md §4.3): PerPhotonEvent vs BulkStream.

Runs the two-node BB84 emulator over loopback for both photon strategies across a
sweep of pulse counts and prints time-to-key and photons/s. This is the decision
artifact: pick PerPhotonEvent where it sustains the required rate (max fidelity),
fall back to BulkStream above its ceiling.

    python bench_throughput.py            # default sweep
    python bench_throughput.py 2000 20000 80000

Note: over loopback TCP (no P4). On FABRIC the photon path becomes the raw-socket /
0x7101 / BMv2 fast path (Phase C2); these numbers bound the pure Python/event cost.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

PKG = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run(mode: str, num_pulses: int, port: int, seed: int = 1) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG + os.pathsep + env.get("PYTHONPATH", "")
    phys = ["--fidelity", "0.95", "--distance-km", "10", "--attenuation", "0.2",
            "--efficiency", "0.8", "--dark-count-rate", "10",
            "--num-pulses", str(num_pulses), "--key-length", "128",
            "--photon-mode", mode]

    def spawn(role, name, peer, sd):
        return subprocess.Popen(
            [PY, "-m", "qne_sequence.node_runner", "--role", role, "--name", name,
             "--peer", peer, "--port", str(port), "--seed", str(sd)] + phys,
            cwd=PKG, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    bob = spawn("bob", "bob", "alice", seed + 1)
    time.sleep(0.4)
    alice = spawn("alice", "alice", "bob", seed)
    ao, ae = alice.communicate(timeout=120)
    bob.communicate(timeout=120)
    if not ao.strip():
        raise RuntimeError(f"{mode} failed: {ae[-800:]}")
    return json.loads(ao)


def main(argv):
    counts = [int(x) for x in argv] or [2000, 10000, 40000, 100000]
    print(f"{'pulses':>8} | {'mode':10} | {'elapsed_s':>9} | {'photons/s':>11} | "
          f"{'qber':>6} | {'sifted':>6} | {'errs':>4}")
    print("-" * 72)
    port = 57900
    for n in counts:
        for mode in ("per_event", "bulk"):
            port += 1
            r = run(mode, n, port)
            print(f"{n:>8} | {mode:10} | {r['elapsed_s']:>9.3f} | "
                  f"{r['photons_per_s']:>11.0f} | {r['qber']:>6.4f} | "
                  f"{r['sifted_bits']:>6} | {r['remote_access_errors']:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
