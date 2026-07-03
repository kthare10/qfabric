#!/usr/bin/env python
"""Run the distributed-SeQUeNCe BB84 emulator across a scenario matrix.

Drives `qne_sequence.node_runner` (two processes over loopback, TCP descriptor
transport) across sweeps of the physics/protocol knobs and records one row per
scenario to JSON. Slice-free and reproducible — the same model the FABRIC raw/P4
path uses, so the curves are directly comparable. Pair with `plots.py` for figures.

    python sweep.py                      # full default matrix -> results/sequence_scenarios.json
    python sweep.py --quick              # small/fast matrix (smoke)
    python sweep.py --out results/x.json # custom output

Sweeps (each row tagged with `sweep` and the swept value `x`):
  * fidelity   — QBER vs polarization fidelity (validates QBER ~ (1-F)/2; Shor-Preskill cutoff)
  * distance   — loss / sifted / key-rate vs fiber distance
  * efficiency — sifted / key-rate vs detector efficiency
  * throughput — photons/s for bulk vs per_event across pulse counts
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PKG = Path(__file__).resolve().parent
PY = sys.executable
RESULTS = PKG / "results"

DEFAULTS = dict(
    num_pulses=12000, key_length=128, sample_fraction=0.2,
    fidelity=0.98, efficiency=0.8, dark_count_rate=10.0,
    distance_km=1.0, attenuation=0.2, photon_mode="bulk", seed=1,
)

# result fields pulled from node_runner's Alice JSON onto each row
_FIELDS = ("qber", "sifted_bits", "num_sampled", "final_key_bits", "secure_fraction",
           "photons_emitted", "photons_per_s", "elapsed_s", "loss_probability",
           "remote_access_errors")


def run_once(port: int, **overrides) -> dict:
    """Run one Alice+Bob emulation over loopback; return merged params + results."""
    p = dict(DEFAULTS, **overrides)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PKG) + os.pathsep + env.get("PYTHONPATH", "")
    common = [
        "--num-pulses", str(p["num_pulses"]), "--key-length", str(p["key_length"]),
        "--sample-fraction", str(p["sample_fraction"]), "--fidelity", str(p["fidelity"]),
        "--efficiency", str(p["efficiency"]), "--dark-count-rate", str(p["dark_count_rate"]),
        "--distance-km", str(p["distance_km"]), "--attenuation", str(p["attenuation"]),
        "--photon-mode", str(p["photon_mode"]), "--port", str(port),
    ]

    def spawn(role, name, peer, seed):
        return subprocess.Popen(
            [PY, "-m", "qne_sequence.node_runner", "--role", role, "--name", name,
             "--peer", peer, "--host", "127.0.0.1", "--seed", str(seed)] + common,
            cwd=PKG, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    bob = spawn("bob", "bob", "alice", p["seed"] + 1)
    time.sleep(0.4)
    alice = spawn("alice", "alice", "bob", p["seed"])
    ao, ae = "", ""
    try:
        ao, ae = alice.communicate(timeout=90)
        bob.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        ao, ae = "", "timeout"   # one bad run -> failed rep, not a sweep-wide crash
    finally:
        for proc in (alice, bob):
            if proc.poll() is None:
                proc.kill()

    def _parse(out):
        for line in reversed(str(out).splitlines()):
            line = line.strip()
            if line.startswith("{") and '"role"' in line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass
        return None

    a = _parse(ao)
    row = {k: p[k] for k in ("num_pulses", "key_length", "sample_fraction", "fidelity",
                             "efficiency", "dark_count_rate", "distance_km",
                             "attenuation", "photon_mode", "seed")}
    if a is None:
        row["error"] = (ae or "no result")[-300:]
    else:
        for f in _FIELDS:
            row[f] = a.get(f)
        row["key"] = a.get("key") is not None
    return row


_PARAM_KEYS = ("num_pulses", "key_length", "sample_fraction", "fidelity", "efficiency",
               "dark_count_rate", "distance_km", "attenuation", "photon_mode")
_AGG_METRICS = ("qber", "sifted_bits", "num_sampled", "final_key_bits", "secure_fraction",
                "photons_emitted", "photons_per_s", "elapsed_s", "loss_probability")


def default_matrix(quick: bool = False) -> list[dict]:
    """One-axis-at-a-time sweeps over every *behavioral* knob (others held at DEFAULTS).

    Each entry is {sweep, x, params}. A full cross-product would explode, so we vary
    one knob per sweep. `quick` shrinks it to a smoke run. Behavioral knobs NOT swept:
    `quantum_transport=raw` (needs FABRIC/P4 — see notebook 08 §B) and `key_num`
    (>1 not supported by DistributedBB84). `time_scale`/`channel_delay` affect only
    wall-clock pacing, not QBER/key yield.
    """
    if quick:
        return (
            [{"sweep": "fidelity", "x": F, "params": {"fidelity": F, "num_pulses": 6000}}
             for F in (0.99, 0.95, 0.90)]
            + [{"sweep": "sample_fraction", "x": s,
                "params": {"sample_fraction": s, "num_pulses": 6000}} for s in (0.1, 0.3)]
        )

    pts = []
    pts += [{"sweep": "fidelity", "x": F, "params": {"fidelity": F}}
            for F in (1.0, 0.99, 0.98, 0.96, 0.94, 0.92, 0.90, 0.88)]
    pts += [{"sweep": "distance", "x": d, "params": {"distance_km": d}}
            for d in (0, 1, 5, 10, 20, 40, 60)]
    pts += [{"sweep": "attenuation", "x": a, "params": {"attenuation": a}}
            for a in (0.1, 0.15, 0.2, 0.25, 0.3, 0.4)]
    pts += [{"sweep": "efficiency", "x": e, "params": {"efficiency": e}}
            for e in (1.0, 0.9, 0.8, 0.6, 0.4, 0.2)]
    # dark counts only bite when a real photon is missed; sweep rate high enough to
    # move the QBER floor (P(dark)=rate*1ns window), at the default ~1 km / eff 0.8.
    pts += [{"sweep": "dark_count", "x": r, "params": {"dark_count_rate": r}}
            for r in (0, 1e4, 1e5, 1e6, 1e7, 1e8)]
    # sample_fraction: mean QBER ~flat, but the rep-to-rep STD shrinks as it grows.
    pts += [{"sweep": "sample_fraction", "x": s, "params": {"sample_fraction": s}}
            for s in (0.05, 0.1, 0.2, 0.3, 0.5)]
    # key_length: at a deliberately small pulse budget, a too-long target stops a
    # key forming (the failure boundary is the point of this sweep).
    pts += [{"sweep": "key_length", "x": k, "params": {"key_length": k, "num_pulses": 2500}}
            for k in (64, 128, 256, 512, 1024, 2048)]
    pts += [{"sweep": "throughput", "x": n,
             "params": {"photon_mode": m, "num_pulses": n, "key_length": 64}}
            for m in ("bulk", "per_event") for n in (2000, 10000, 40000)]
    return pts


def aggregate(runs: list[dict]) -> dict:
    """Collapse `reps` runs of one point into mean/std per metric.

    Keeps the bare metric name = mean (so existing plots work) and adds `<metric>_std`
    for error bars. Aggregates only over reps that produced a key.
    """
    import statistics
    ok = [r for r in runs if r.get("key")]
    base = runs[0] if runs else {}
    row = {k: base.get(k) for k in _PARAM_KEYS}
    row["n_reps"] = len(runs)
    row["n_ok"] = len(ok)
    row["key"] = len(ok) > 0
    row["remote_access_errors"] = sum(int(r.get("remote_access_errors") or 0) for r in runs)
    for m in _AGG_METRICS:
        vals = [r[m] for r in ok if r.get(m) is not None]
        if vals:
            row[m] = statistics.fmean(vals)
            row[m + "_std"] = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    if not ok and runs:
        row["error"] = runs[0].get("error", "no key in any rep")
    return row


def run_sweep(matrix: list[dict], reps: int = 5, base_port: int = 58000,
              seed0: int = 1) -> list[dict]:
    import itertools
    ports = itertools.count(base_port)
    rows = []
    for i, pt in enumerate(matrix):
        params = pt.get("params", {})
        runs = [run_once(next(ports), seed=seed0 + j, **params) for j in range(reps)]
        row = aggregate(runs)
        row["sweep"], row["x"] = pt["sweep"], pt["x"]
        rows.append(row)
        q = row.get("qber")
        qs = row.get("qber_std")
        qtxt = f"{q:.4f}±{qs:.4f}" if isinstance(q, float) else "n/a"
        print(f"[{i + 1}/{len(matrix)}] {pt['sweep']:14} x={pt['x']:<8} reps={reps} "
              f"-> qber={qtxt} key={row['n_ok']}/{reps} errs={row['remote_access_errors']}",
              flush=True)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sweep the distributed-SeQUeNCe BB84 emulator.")
    ap.add_argument("--out", default=str(RESULTS / "sequence_scenarios.json"))
    ap.add_argument("--quick", action="store_true", help="small/fast matrix")
    ap.add_argument("--reps", type=int, default=5, help="repetitions per point (mean/std)")
    ap.add_argument("--base-port", type=int, default=58000)
    args = ap.parse_args(argv)

    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = run_sweep(default_matrix(args.quick), reps=args.reps, base_port=args.base_port)
    Path(args.out).write_text(json.dumps(rows, indent=2))
    ok = sum(1 for r in rows if r.get("key"))
    print(f"\nSaved {len(rows)} points x{args.reps} reps ({ok} formed a key) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
