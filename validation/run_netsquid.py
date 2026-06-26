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

"""Run BB84 scenario using the NetSquid simulator for cross-validation.

Requires NetSquid (register + install via https://netsquid.org). If it is not
installed, the backend reports itself unavailable and is SKIPPED by
validation.compare — never silently treated as a pass.

This adapter uses NetSquid's *actual* qubit machinery: it prepares real qubits,
applies NetSquid's DepolarNoiseModel for polarization imperfection, and measures
with netsquid.qubits.qubitapi in the chosen basis. The QBER therefore comes from
NetSquid's own physics, not a re-implementation. Fiber loss uses the Beer-Lambert
law 10^(-alpha*L/10) — identical in NetSquid's FibreLossModel and in QFabric — so
it is computed directly to decide which qubits survive.

Tested against NetSquid 1.1.x. If a future version renames an API, this adapter
fails cleanly and the backend is reported as SKIPPED with the error (it never
fakes a pass). Validate on FABRIC after installing NetSquid.

Usage:
    python -m validation.run_netsquid validation/scenarios/baseline_1km.yml
"""

from __future__ import annotations

from pathlib import Path

from validation.scenario import ValidationResult, ValidationScenario


def run_netsquid_bb84(scenario: ValidationScenario) -> ValidationResult:
    """Execute BB84 using NetSquid qubits + DepolarNoiseModel."""
    try:
        import netsquid as ns
        from netsquid.qubits import qubitapi as qapi
        from netsquid.components.models.qerrormodels import DepolarNoiseModel
    except ImportError:
        print("NetSquid not installed. See https://netsquid.org for installation.")
        return ValidationResult(
            platform="netsquid",
            scenario_name=scenario.name,
            extra={"error": "netsquid not installed"},
        )

    try:
        import numpy as np

        # Depolarizing requires a density-matrix formalism.
        ns.set_qstate_formalism(ns.QFormalism.DM)
        ns.sim_reset()
        try:
            ns.set_random_state(seed=scenario.seed)
        except Exception:
            pass
        rng = np.random.default_rng(scenario.seed)

        n = scenario.num_photons
        loss_prob = scenario.expected_loss_probability
        eta = scenario.detector_efficiency
        p_dc = scenario.dark_count_rate_hz * 1e-9  # per 1 ns window

        # NetSquid's own depolarizing channel models polarization imperfection.
        # depolar prob d on a matched basis yields QBER = d/2, so set d = 1 - F.
        depolar = DepolarNoiseModel(depolar_rate=1.0 - scenario.polarization_fidelity,
                                    time_independent=True)

        alice_bases = rng.integers(0, 2, size=n)
        alice_bits = rng.integers(0, 2, size=n)
        bob_bases = rng.integers(0, 2, size=n)

        sifted_a, sifted_b = [], []
        received = 0

        for i in range(n):
            detected_real = (rng.random() >= loss_prob) and (rng.random() < eta)

            if not detected_real:
                # Empty slot: a dark count may still fire (random bit).
                if rng.random() < p_dc:
                    received += 1
                    if alice_bases[i] == bob_bases[i]:
                        sifted_a.append(int(alice_bits[i]))
                        sifted_b.append(int(rng.integers(0, 2)))
                continue

            # Prepare Alice's qubit: Z basis -> |0>/|1>, X basis -> |+>/|->.
            q = qapi.create_qubits(1)[0]
            if alice_bases[i] == 0:
                if alice_bits[i] == 1:
                    qapi.operate(q, ns.X)
            else:
                if alice_bits[i] == 1:
                    qapi.operate(q, ns.X)
                qapi.operate(q, ns.H)

            # Apply NetSquid's depolarizing noise (the real QBER source).
            depolar.error_operation([q])

            # Bob measures in his basis: Z -> observable Z, X -> observable X.
            observable = ns.Z if bob_bases[i] == 0 else ns.X
            outcome, _prob = qapi.measure(q, observable=observable)
            received += 1

            if alice_bases[i] == bob_bases[i]:
                sifted_a.append(int(alice_bits[i]))
                sifted_b.append(int(outcome))

        n_sifted = len(sifted_a)
        errors = sum(a != b for a, b in zip(sifted_a, sifted_b))
        qber = errors / n_sifted if n_sifted > 0 else 0.0

        from qne.bb84 import BB84Protocol
        secure_fraction = BB84Protocol.secure_key_fraction(qber)

        return ValidationResult(
            platform="netsquid",
            scenario_name=scenario.name,
            photons_sent=n,
            photons_received=received,
            sifted_bits=n_sifted,
            qber=qber,
            raw_key_rate=n_sifted / n if n > 0 else 0.0,
            secure_key_rate=(n_sifted * secure_fraction) / n if n > 0 else 0.0,
            extra={"netsquid_version": getattr(ns, "__version__", "unknown"),
                   "qber_sample_bits": n_sifted},  # QBER over the full key
        )
    except Exception as e:  # API drift / runtime issue → honest SKIP, never fake pass
        import traceback
        return ValidationResult(
            platform="netsquid",
            scenario_name=scenario.name,
            extra={"error": f"netsquid run failed: {e}",
                   "traceback": traceback.format_exc()},
        )


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run BB84 on NetSquid for one scenario")
    parser.add_argument("scenario", help="Path to a scenario YAML (single scenario)")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="Write the ValidationResult as JSON to this path ('-' for stdout). "
                             "Used by validation.compare to call this backend in its own Python env.")
    args = parser.parse_args()

    scenario = ValidationScenario.from_yaml(Path(args.scenario))
    result = run_netsquid_bb84(scenario)

    if args.json_out is not None:
        payload = json.dumps(result.to_payload())
        if args.json_out == "-":
            print("___QFABRIC_RESULT___" + payload)
        else:
            Path(args.json_out).write_text(payload)
    else:
        print(f"=== NetSquid: {scenario.name} ===")
        print(f"  QBER: {result.qber:.4f}")
        print(f"  Sifted bits: {result.sifted_bits}")
        if result.extra.get("error"):
            print(f"  ERROR: {result.extra['error']}")


if __name__ == "__main__":
    main()
