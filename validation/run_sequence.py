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

"""Run BB84 scenario using the SeQUeNCe simulator for cross-validation.

Requires SeQUeNCe. **Pin to the 3.11-compatible line** (`pip install 'sequence>=0.8.1,<1.0'`):
SeQUeNCe 1.0 requires Python >=3.12, but NetSquid only supports 3.10/3.11 — so to
run both simulators in one environment, use Python 3.11 with sequence 0.8.x.

This adapter drives SeQUeNCe's *actual* discrete-event QKD stack, following the
canonical BB84 example (sequence-toolbox/SeQUeNCe, example/qkd): two QKDNodes
paired with `pair_bb84_protocols`, real QuantumChannels carrying
`polarization_fidelity` (which is the QBER source), Alice's light source and
Bob's two detectors configured, and key generation kicked off by scheduling the
protocol's `push` as a timeline Event. QBER comes from `protocol_stack[0].error_rates`.

Unit note: SeQUeNCe distances are in metres and attenuation in dB/m, so we pass
`distance_km*1000` and `attenuation_db_per_km/1000`.

If the installed version renames an API, the adapter fails cleanly and the backend
is reported SKIPPED with the error — it never fakes a pass.

Usage:
    python -m validation.run_sequence validation/scenarios/baseline_1km.yml
"""

from __future__ import annotations

from pathlib import Path

from validation.scenario import ValidationResult, ValidationScenario


class _KeyConsumer:
    """Minimal upper-layer protocol that records delivered keys (BB84 test pattern)."""

    def __init__(self, timeline):
        self.timeline = timeline
        self.lower_protocols = []
        self.upper_protocols = []
        self.keys = []

    def pop(self, info):  # SeQUeNCe BB84 calls pop(info) per generated key
        self.keys.append(info)

    def push(self, *args, **kwargs):
        pass

    def received_message(self, *args, **kwargs):
        pass

    def init(self):
        pass


def run_sequence_bb84(scenario: ValidationScenario) -> ValidationResult:
    """Execute BB84 on SeQUeNCe's native QKD stack."""
    try:
        import sequence
        from sequence.kernel.timeline import Timeline
        from sequence.kernel.event import Event
        from sequence.kernel.process import Process
        from sequence.topology.node import QKDNode
        from sequence.components.optical_channel import QuantumChannel, ClassicalChannel
        from sequence.qkd.BB84 import pair_bb84_protocols
    except ImportError as e:
        print("SeQUeNCe not installed. Install with: pip install 'sequence>=0.8.1,<1.0'")
        return ValidationResult(
            platform="sequence",
            scenario_name=scenario.name,
            extra={"error": f"sequence not installed ({e})"},
        )

    try:
        import numpy as np

        distance_m = scenario.distance_km * 1000.0
        attenuation_db_m = scenario.attenuation_db_per_km / 1000.0  # SeQUeNCe uses dB/m
        keysize = 256
        num_keys = 20  # ~5120 sifted bits — enough for a stable QBER comparison

        tl = Timeline(1e13)  # ps; finite num_keys stops the run early
        tl.show_progress = False

        alice = QKDNode("alice", tl, stack_size=1)
        bob = QKDNode("bob", tl, stack_size=1)
        alice.set_seed(scenario.seed)
        bob.set_seed(scenario.seed + 1)
        pair_bb84_protocols(alice.protocol_stack[0], bob.protocol_stack[0])

        # polarization_fidelity F drives the intrinsic QBER (~(1-F)/2), matching
        # QFabric's depolarizing model.
        qc0 = QuantumChannel("qc0", tl, distance=distance_m,
                             polarization_fidelity=scenario.polarization_fidelity,
                             attenuation=attenuation_db_m)
        qc1 = QuantumChannel("qc1", tl, distance=distance_m,
                             polarization_fidelity=scenario.polarization_fidelity,
                             attenuation=attenuation_db_m)
        qc0.set_ends(alice, bob.name)
        qc1.set_ends(bob, alice.name)
        cc0 = ClassicalChannel("cc0", tl, distance=distance_m)
        cc1 = ClassicalChannel("cc1", tl, distance=distance_m)
        cc0.set_ends(alice, bob.name)
        cc1.set_ends(bob, alice.name)

        # Alice's light source (without this, no photons are emitted!).
        alice.update_lightsource_params("frequency", 80e6)
        alice.update_lightsource_params("mean_photon_num", 0.1)
        # Bob's two detectors.
        det = {"efficiency": scenario.detector_efficiency,
               "dark_count": scenario.dark_count_rate_hz,
               "time_resolution": 10, "count_rate": 50e6}
        for i in range(2):
            for name, val in det.items():
                bob.update_detector_params(i, name, val)

        # Record keys so we can count sifted material.
        consumer = _KeyConsumer(tl)
        consumer.lower_protocols.append(alice.protocol_stack[0])
        alice.protocol_stack[0].upper_protocols.append(consumer)

        # Kick off key generation by scheduling push(keysize, num_keys) at t=0.
        process = Process(alice.protocol_stack[0], "push", [keysize, num_keys])
        tl.schedule(Event(0, process))

        tl.init()
        tl.run()

        # SeQUeNCe appends one entry to error_rates per generated key.
        error_rates = list(getattr(alice.protocol_stack[0], "error_rates", []) or [])
        qber = float(np.mean(error_rates)) if error_rates else 0.0
        num_generated = len(error_rates) or len(consumer.keys)
        sifted_bits = keysize * num_generated

        from qne.bb84 import BB84Protocol
        secure_fraction = BB84Protocol.secure_key_fraction(qber)

        n = scenario.num_photons
        return ValidationResult(
            platform="sequence",
            scenario_name=scenario.name,
            photons_sent=n,
            sifted_bits=sifted_bits,
            qber=qber,
            raw_key_rate=sifted_bits / n if n > 0 else 0.0,
            secure_key_rate=(sifted_bits * secure_fraction) / n if n > 0 else 0.0,
            extra={"sequence_version": getattr(sequence, "__version__", "unknown"),
                   "num_keys": num_generated,
                   "qber_sample_bits": sifted_bits},  # QBER over the full key
        )
    except Exception as e:  # API drift / runtime issue → honest SKIP, never fake pass
        import traceback
        return ValidationResult(
            platform="sequence",
            scenario_name=scenario.name,
            extra={"error": f"sequence run failed: {e}",
                   "traceback": traceback.format_exc()},
        )


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run BB84 on SeQUeNCe for one scenario")
    parser.add_argument("scenario", help="Path to a scenario YAML (single scenario)")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="Write the ValidationResult as JSON to this path ('-' for stdout). "
                             "Used by validation.compare to call this backend in its own Python env.")
    args = parser.parse_args()

    scenario = ValidationScenario.from_yaml(Path(args.scenario))
    result = run_sequence_bb84(scenario)

    if args.json_out is not None:
        payload = json.dumps(result.to_payload())
        if args.json_out == "-":
            # Sentinel-wrapped so the caller can extract it cleanly from stdout.
            print("___QFABRIC_RESULT___" + payload)
        else:
            Path(args.json_out).write_text(payload)
    else:
        print(f"=== SeQUeNCe: {scenario.name} ===")
        print(f"  QBER: {result.qber:.4f}")
        print(f"  Sifted bits: {result.sifted_bits}")
        if result.extra.get("error"):
            print(f"  ERROR: {result.extra['error']}")


if __name__ == "__main__":
    main()
