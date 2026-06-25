# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Cross-validation comparison across QFabric, SeQUeNCe, and NetSquid.

Loads results from all three platforms, computes statistical agreement,
and generates comparison plots.

Usage:
    python -m validation.compare validation/scenarios/baseline_1km.yml
    python -m validation.compare validation/scenarios/sweep_distance.yml
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

from validation.scenario import ValidationResult, ValidationScenario
from validation.run_qfabric import run_qfabric_bb84_simulated

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Conditional in-process imports for simulators (used when they share this env).
try:
    from validation.run_sequence import run_sequence_bb84
    HAS_SEQUENCE = True
except ImportError:
    HAS_SEQUENCE = False

try:
    from validation.run_netsquid import run_netsquid_bb84
    HAS_NETSQUID = True
except ImportError:
    HAS_NETSQUID = False

# A simulator can instead run in its OWN Python environment (e.g. SeQUeNCe 1.0
# needs Python >=3.12, NetSquid needs 3.10/3.11 — they can't share an interpreter).
# Point these at the venv python for each backend; if unset, the backend runs
# in-process in the current interpreter.
SEQUENCE_PYTHON = os.environ.get("QFABRIC_SEQUENCE_PYTHON")
NETSQUID_PYTHON = os.environ.get("QFABRIC_NETSQUID_PYTHON")

_RESULT_SENTINEL = "___QFABRIC_RESULT___"


def run_backend_subprocess(
    python_exe: str, module: str, platform: str, scenario: ValidationScenario,
    timeout: float = 900.0,
) -> ValidationResult:
    """Run a backend adapter in a separate Python interpreter and parse its result.

    The adapter is invoked as `python_exe -m <module> <scenario.yml> --json -`
    with PYTHONPATH set to the project root, so the venv only needs the simulator
    plus numpy/pyyaml — not an install of qfabric.
    """
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
    try:
        yaml.safe_dump(scenario.to_flat_dict(), tmp)
        tmp.close()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_DIR) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [python_exe, "-m", module, tmp.name, "--json", "-"],
            cwd=str(PROJECT_DIR), env=env, capture_output=True, text=True, timeout=timeout,
        )
        return _result_from_stdout(proc.stdout, proc.stderr, proc.returncode,
                                   platform, scenario.name)
    except Exception as e:
        return ValidationResult(
            platform=platform, scenario_name=scenario.name,
            extra={"error": f"{platform} subprocess error: {e}"},
        )
    finally:
        os.unlink(tmp.name)


def _result_from_stdout(stdout: str, stderr: str, rc, platform: str,
                        scenario_name: str) -> ValidationResult:
    """Extract a sentinel-wrapped ValidationResult from an adapter's stdout.

    Adapters print `___QFABRIC_RESULT___<json>` (the --json - mode). If no such
    line is present the backend is reported as errored (→ SKIPPED, never a pass).
    """
    # Find the sentinel anywhere in stdout (robust to line prefixes / CRLF that
    # remote shells sometimes add), then parse the JSON to end of that line.
    text = stdout or ""
    idx = text.find(_RESULT_SENTINEL)
    if idx != -1:
        rest = text[idx + len(_RESULT_SENTINEL):]
        payload = rest.splitlines()[0] if rest.splitlines() else rest
        try:
            return ValidationResult.from_payload(json.loads(payload))
        except Exception:
            pass  # fall through to error reporting with detail below
    detail = (stderr or stdout or "no output").strip()[-600:]
    return ValidationResult(
        platform=platform, scenario_name=scenario_name,
        extra={"error": f"{platform} produced no result (rc={rc})", "detail": detail},
    )


def run_backend_on_node(node, venv_python: str, module: str, platform: str,
                        scenario_name: str = "fabric",
                        remote_dir: str = "~/qfabric",
                        scenario_file: str = "scenario.yml") -> ValidationResult:
    """Run a backend adapter ON a FABRIC node (via fablib `node.execute`).

    The node must already have the qfabric repo at `remote_dir` (qne + validation)
    and `scenario_file` present, plus a venv at `venv_python` with the simulator +
    numpy/pyyaml. The adapter emits a sentinel-wrapped JSON result on stdout, which
    we parse back into a ValidationResult. `node` is any object with an
    `.execute(cmd, quiet=...)` method returning (stdout, stderr).
    """
    cmd = (f"cd {remote_dir} && PYTHONPATH=. {venv_python} -m {module} "
           f"{scenario_file} --json -")
    try:
        stdout, stderr = node.execute(cmd, quiet=True)
    except Exception as e:
        return ValidationResult(platform=platform, scenario_name=scenario_name,
                                extra={"error": f"{platform} on-node execute failed: {e}"})
    return _result_from_stdout(stdout, stderr, "n/a", platform, scenario_name)


def qber_tolerance(qber: float, n_sifted: int, num_sigma: float = 2.0) -> float:
    """Compute statistical tolerance for QBER comparison.

    Returns the tolerance: num_sigma * sqrt(QBER * (1-QBER) / N)
    """
    if n_sifted == 0:
        return float("inf")
    return num_sigma * math.sqrt(max(qber * (1 - qber), 1e-10) / n_sifted)


def compare_results(
    results: list[ValidationResult],
    num_sigma: float = 2.0,
) -> dict:
    """Compare results across platforms.

    Returns a dict with comparison details and pass/fail for each pair.
    """
    comparisons = []

    for i, res_a in enumerate(results):
        for res_b in results[i + 1:]:
            delta_qber = abs(res_a.qber - res_b.qber)
            avg_qber = (res_a.qber + res_b.qber) / 2
            # Tolerance on the DIFFERENCE of two independent QBER estimates:
            #   sigma^2(ΔQBER) = p(1-p) (1/N_a + 1/N_b)
            # where N is the number of bits each QBER was estimated from (the
            # QBER sample, not necessarily the full sifted key). This correctly
            # widens the bound when a backend (e.g. the live FABRIC run) estimates
            # QBER from a small sample.
            n_a = res_a.extra.get("qber_sample_bits") or res_a.sifted_bits or 1
            n_b = res_b.extra.get("qber_sample_bits") or res_b.sifted_bits or 1
            var = max(avg_qber * (1 - avg_qber), 1e-10) * (1.0 / n_a + 1.0 / n_b)
            tol = num_sigma * math.sqrt(var)

            passed = delta_qber < tol

            comparisons.append({
                "platform_a": res_a.platform,
                "platform_b": res_b.platform,
                "qber_a": res_a.qber,
                "qber_b": res_b.qber,
                "delta_qber": delta_qber,
                "tolerance": tol,
                "passed": passed,
                "sifted_a": res_a.sifted_bits,
                "sifted_b": res_b.sifted_bits,
            })

    return {"comparisons": comparisons, "all_passed": all(c["passed"] for c in comparisons)}


def backend_status(result: ValidationResult) -> tuple[str, str]:
    """Classify a backend result for honest reporting.

    Returns (status, detail) where status is one of:
        "ok"          — backend ran and produced sifted key material
        "unavailable" — backend not installed / errored (won't be compared)
        "no_data"     — backend ran but produced 0 sifted bits (inconclusive)

    Only "ok" backends take part in the pass/fail comparison; the others are
    reported as SKIPPED so a missing simulator never masquerades as a PASS.
    """
    if result.extra.get("error"):
        return "unavailable", str(result.extra["error"])
    if result.sifted_bits <= 0:
        return "no_data", "produced 0 sifted bits"
    return "ok", ""


def print_backend_summary(results: list[ValidationResult]) -> list[ValidationResult]:
    """Print availability of each backend; return only the comparable ones."""
    ok_results = []
    print("\n--- Backends ---")
    for r in results:
        status, detail = backend_status(r)
        if status == "ok":
            print(f"  [OK]      {r.platform}: QBER={r.qber:.4f}, sifted={r.sifted_bits}")
            ok_results.append(r)
        elif status == "unavailable":
            print(f"  [SKIPPED] {r.platform}: unavailable ({detail})")
            node_detail = r.extra.get("detail")
            if node_detail:
                print(f"            ↳ {node_detail.strip().splitlines()[-1][:300]}")
        else:  # no_data
            print(f"  [SKIPPED] {r.platform}: {detail} — cannot cross-validate")
    if len(ok_results) < 2:
        print("\n  NOTE: fewer than 2 backends produced data — cross-validation is "
              "inconclusive.\n        Install SeQUeNCe (pip install sequence) and/or "
              "NetSquid to compare.")
    return ok_results


def load_fabric_result(path: str | Path) -> Optional[ValidationResult]:
    """Build the QFabric data point from a FABRIC testbed run (notebook 02).

    Reads an ExperimentMetrics JSON (results/fabric_bob_results.json) and returns
    it as a ValidationResult tagged platform="qfabric" so it can be compared
    against the simulators. Returns None if the file is missing.
    """
    path = Path(path)
    if not path.exists():
        return None
    text = path.read_text().strip()
    if not text:
        return None  # empty file (a failed/empty download) — not a stale reuse
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return None
    sifted = d.get("sifted_bits", 0)
    # The live BB84 run estimates QBER from a sample_fraction of the sifted key,
    # so its QBER has the variance of that smaller sample — record it for the
    # tolerance computation (fall back to a 10% sample if not stated).
    sample_frac = d.get("config", {}).get("protocol", {}).get("sample_fraction", 0.1)
    qber_sample_bits = d.get("num_sampled") or max(1, int(sifted * sample_frac))
    return ValidationResult(
        platform="qfabric",
        scenario_name=d.get("scenario_name", "fabric"),
        photons_sent=d.get("photons_sent", 0),
        photons_received=d.get("photons_received", 0),
        sifted_bits=sifted,
        qber=d.get("qber", 0.0),
        raw_key_rate=d.get("raw_key_rate", 0.0),
        secure_key_rate=d.get("secure_key_rate", 0.0),
        elapsed_seconds=d.get("elapsed_seconds", 0.0),
        extra={"source": "FABRIC testbed", "qber_sample_bits": qber_sample_bits},
    )


def scenario_from_fabric_result(path: str | Path) -> ValidationScenario:
    """Reconstruct the scenario that was run on FABRIC, so the simulators use
    matching parameters."""
    d = json.loads(Path(path).read_text())
    cfg = d.get("config", {})
    ch, det, proto = cfg.get("channel", {}), cfg.get("detector", {}), cfg.get("protocol", {})
    return ValidationScenario(
        name=d.get("scenario_name", "fabric"),
        distance_km=ch.get("distance_km", 1.0),
        attenuation_db_per_km=ch.get("attenuation_db_per_km", 0.2),
        polarization_fidelity=ch.get("polarization_fidelity", 1.0),
        detector_efficiency=det.get("efficiency", 0.8),
        dark_count_rate_hz=det.get("dark_count_rate", 10.0),
        num_photons=proto.get("num_photons", d.get("photons_sent", 100_000)),
        sample_fraction=proto.get("sample_fraction", 0.1),
        seed=cfg.get("seed", 42),
    )


def run_all_platforms(
    scenario: ValidationScenario,
    qfabric_result: Optional[ValidationResult] = None,
) -> list[ValidationResult]:
    """Run scenario on all platforms.

    The QFabric data point is the measured FABRIC result when `qfabric_result` is
    provided (the real cross-validation), otherwise the pure-Python simulation.
    SeQUeNCe and NetSquid run in their own Python environment if
    QFABRIC_SEQUENCE_PYTHON / QFABRIC_NETSQUID_PYTHON are set (recommended — they
    have conflicting Python requirements); otherwise they run in-process if
    importable here.
    """
    if qfabric_result is not None:
        print("  Using QFabric FABRIC testbed result...")
        results = [qfabric_result]
    else:
        print(f"  Running QFabric (simulated)...")
        results = [run_qfabric_bb84_simulated(scenario)]

    # SeQUeNCe
    if SEQUENCE_PYTHON:
        print(f"  Running SeQUeNCe (env: {SEQUENCE_PYTHON})...")
        results.append(run_backend_subprocess(
            SEQUENCE_PYTHON, "validation.run_sequence", "sequence", scenario))
    elif HAS_SEQUENCE:
        print(f"  Running SeQUeNCe (in-process)...")
        results.append(run_sequence_bb84(scenario))

    # NetSquid
    if NETSQUID_PYTHON:
        print(f"  Running NetSquid (env: {NETSQUID_PYTHON})...")
        results.append(run_backend_subprocess(
            NETSQUID_PYTHON, "validation.run_netsquid", "netsquid", scenario))
    elif HAS_NETSQUID:
        print(f"  Running NetSquid (in-process)...")
        results.append(run_netsquid_bb84(scenario))

    return results


def plot_sweep_comparison(
    all_results: dict[str, list[ValidationResult]],
    sweep_param: str,
    sweep_values: list[float],
    output_path: Optional[str] = None,
) -> None:
    """Generate comparison plots for a parameter sweep."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    # Group results by platform
    platforms = set()
    for results in all_results.values():
        for r in results:
            platforms.add(r.platform)

    colors = {"qfabric": "blue", "sequence": "red", "netsquid": "green", "qfabric_bmv2": "purple"}
    markers = {"qfabric": "o", "sequence": "s", "netsquid": "^", "qfabric_bmv2": "D"}

    def _xval(name: str) -> float:
        """Numeric x for a scenario. Sweep names are '<param>=<value>'."""
        try:
            return float(name.split("=")[-1])
        except ValueError:
            return float("nan")

    def _series(platform: str, metric):
        """Sorted (x, y) points for a platform, skipping backends with no data."""
        pts = []
        for name, results in all_results.items():
            for r in results:
                if r.platform == platform and backend_status(r)[0] == "ok":
                    pts.append((_xval(name), metric(r)))
        return sorted(p for p in pts if p[0] == p[0])  # drop NaN x

    # Plot QBER vs sweep parameter
    ax = axes[0]
    for platform in sorted(platforms):
        pts = _series(platform, lambda r: r.qber)
        if pts:
            xs, ys = zip(*pts)
            ax.plot(xs, ys, marker=markers.get(platform, "x"),
                    color=colors.get(platform, "gray"), label=platform, linewidth=1.5)

    ax.set_xlabel(sweep_param)
    ax.set_ylabel("QBER")
    ax.set_title(f"QBER vs {sweep_param}")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # Plot key rate vs sweep parameter
    ax = axes[1]
    for platform in sorted(platforms):
        pts = _series(platform, lambda r: r.secure_key_rate)
        if pts:
            xs, ys = zip(*pts)
            ax.plot(xs, ys, marker=markers.get(platform, "x"),
                    color=colors.get(platform, "gray"), label=platform, linewidth=1.5)

    ax.set_xlabel(sweep_param)
    ax.set_ylabel("Secure Key Rate (bits/photon)")
    ax.set_title(f"Secure Key Rate vs {sweep_param}")
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    if output_path:
        plt.savefig(output_path, dpi=150, bbox_inches="tight")
        print(f"Plot saved to {output_path}")
    else:
        plt.show()


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m validation.compare <scenario_or_sweep.yml> [--plot output.png]")
        sys.exit(1)

    path = Path(sys.argv[1])
    plot_output = None
    if "--plot" in sys.argv:
        plot_idx = sys.argv.index("--plot")
        if plot_idx + 1 < len(sys.argv):
            plot_output = sys.argv[plot_idx + 1]

    if "sweep" in path.stem:
        scenarios = ValidationScenario.load_sweep(path)
        all_results = {}

        for scenario in scenarios:
            print(f"\n=== Scenario: {scenario.name} ===")
            results = run_all_platforms(scenario)
            all_results[scenario.name] = results

            ok_results = print_backend_summary(results)
            comp = compare_results(ok_results)
            if not comp["comparisons"]:
                print("  (no backend pair to compare)")
            for c in comp["comparisons"]:
                status = "PASS" if c["passed"] else "FAIL"
                print(
                    f"  [{status}] {c['platform_a']} vs {c['platform_b']}: "
                    f"ΔQBER={c['delta_qber']:.4f} (tol={c['tolerance']:.4f})"
                )

        if plot_output:
            # Determine sweep parameter from filename
            sweep_param = path.stem.replace("sweep_", "")
            values = [s.name for s in scenarios]
            plot_sweep_comparison(all_results, sweep_param, values, plot_output)

    else:
        scenario = ValidationScenario.from_yaml(path)
        print(f"\n=== Scenario: {scenario.name} ===")
        results = run_all_platforms(scenario)

        ok_results = print_backend_summary(results)
        comp = compare_results(ok_results)

        print(f"\n--- Comparisons ---")
        if not comp["comparisons"]:
            print("  (need at least 2 backends with data to cross-validate)")
        for c in comp["comparisons"]:
            status = "PASS" if c["passed"] else "FAIL"
            print(
                f"  [{status}] {c['platform_a']} vs {c['platform_b']}: "
                f"ΔQBER={c['delta_qber']:.4f} (tol={c['tolerance']:.4f})"
            )

        if not comp["comparisons"]:
            print("\nOverall: INCONCLUSIVE (no cross-validation performed)")
        else:
            all_pass = comp["all_passed"]
            print(f"\nOverall: {'ALL PASSED' if all_pass else 'SOME FAILED'}")


if __name__ == "__main__":
    main()
