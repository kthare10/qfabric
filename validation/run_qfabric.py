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

"""Run BB84 scenario using QFabric local BMv2 emulation.

Orchestrates: compile P4 → configure tables → launch Alice+Bob → collect results.

Usage:
    python -m validation.run_qfabric validation/scenarios/baseline_1km.yml
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from qne.bb84 import AliceRecord, BB84Protocol, BobRecord
from qne.detector import Detector
from qne.photon import PhotonPacket
from validation.scenario import ValidationResult, ValidationScenario


def run_qfabric_bb84_simulated(scenario: ValidationScenario) -> ValidationResult:
    """Run QFabric BB84 in pure-Python simulation mode.

    This bypasses BMv2 and raw sockets, but uses the same loss model,
    detector model, and BB84 protocol logic. Suitable for cross-validation
    without requiring root access or BMv2 installation.
    """
    rng = np.random.default_rng(scenario.seed)

    # Loss probability (same formula as P4 switch)
    loss_prob = scenario.expected_loss_probability

    # Detector. Polarization imperfection (1 - fidelity) is the intrinsic
    # QBER source; same mapping is used by the SeQUeNCe/NetSquid adapters.
    detector = Detector(
        efficiency=scenario.detector_efficiency,
        dark_count_rate=scenario.dark_count_rate_hz,
        polarization_error=1.0 - scenario.polarization_fidelity,
        seed=scenario.seed + 100,
    )

    # Generate photons (Alice)
    alice_log: list[AliceRecord] = []
    bob_log: list[BobRecord] = []

    for seq in range(scenario.num_photons):
        basis = int(rng.integers(0, 2))
        state = int(rng.integers(0, 2))

        alice_log.append(AliceRecord(
            sequence_num=seq,
            basis=basis,
            bit_value=state,
        ))

        # Channel loss (emulating P4 random drop)
        if rng.random() < loss_prob:
            continue  # Photon lost in fiber

        # Detector model
        photon = PhotonPacket(basis=basis, state=state, sequence_num=seq)
        event = detector.detect(photon)

        if event.detected:
            bob_log.append(BobRecord(
                sequence_num=event.sequence_num,
                basis=event.basis,
                bit_value=event.bit_value,
            ))

    # BB84 sifting
    protocol = BB84Protocol(
        sample_fraction=scenario.sample_fraction,
        seed=scenario.seed + 1,
    )
    sifted = protocol.sift(alice_log, bob_log)
    qber_est = protocol.estimate_qber(sifted)
    key_rate = protocol.compute_key_rate(sifted, qber_est, scenario.num_photons)

    # For cross-validation report QBER over the FULL sifted key (like the
    # simulators do) rather than the 10% protocol sample — this removes sampling
    # noise so the comparison reflects the model, not the estimator. The sampled
    # estimate is still used for the (realistic) key-rate accounting above.
    n_sifted = sifted.sifted_count
    if n_sifted > 0:
        full_errors = sum(1 for a, b in zip(sifted.alice_bits, sifted.bob_bits) if a != b)
        full_qber = full_errors / n_sifted
    else:
        full_qber = 0.0

    return ValidationResult(
        platform="qfabric",
        scenario_name=scenario.name,
        photons_sent=scenario.num_photons,
        photons_received=len(bob_log),
        sifted_bits=n_sifted,
        qber=full_qber,
        raw_key_rate=key_rate.raw_key_rate,
        secure_key_rate=key_rate.secure_key_rate,
        extra={
            "loss_probability": loss_prob,
            "final_key_bits": key_rate.final_key_bits,
            "qber_sample_bits": n_sifted,   # QBER computed over the full sifted key
            "sampled_qber": qber_est.qber,  # the 10% protocol-sample estimate
        },
    )


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run QFabric BB84 simulation for one scenario")
    parser.add_argument("scenario", help="Path to a scenario YAML (single scenario)")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="Write the ValidationResult as JSON to this path ('-' for stdout). "
                             "Lets validation.compare run QFabric-sim in a separate env / on a node.")
    args = parser.parse_args()

    scenario = ValidationScenario.from_yaml(Path(args.scenario))
    result = run_qfabric_bb84_simulated(scenario)
    # Tag as the simulation point so it's distinct from the FABRIC measurement.
    result.platform = "qfabric_sim"

    if args.json_out is not None:
        payload = json.dumps(result.to_payload())
        if args.json_out == "-":
            print("___QFABRIC_RESULT___" + payload)
        else:
            Path(args.json_out).write_text(payload)
    else:
        print(f"=== QFabric (simulated): {scenario.name} ===")
        print(f"  Photons received: {result.photons_received}")
        print(f"  Sifted bits: {result.sifted_bits}")
        print(f"  QBER: {result.qber:.4f}")
        print(f"  Secure key rate: {result.secure_key_rate:.4f}")


if __name__ == "__main__":
    main()
