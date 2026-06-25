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

"""Tests for the cross-validation framework and reference BB84 model."""

from validation.compare import backend_status, compare_results
from validation.reference_bb84 import reference_bb84
from validation.run_qfabric import run_qfabric_bb84_simulated
from validation.scenario import ValidationResult, ValidationScenario


def _scenario(**kw):
    base = dict(name="t", distance_km=1.0, attenuation_db_per_km=0.2,
                detector_efficiency=0.8, dark_count_rate_hz=10.0,
                polarization_fidelity=0.98, num_photons=50_000, seed=7)
    base.update(kw)
    return ValidationScenario(**base)


class TestReferenceModel:
    def test_qber_matches_misalignment(self):
        """Reference QBER ≈ (1 - fidelity)/2."""
        r = reference_bb84(_scenario(polarization_fidelity=0.96), platform="ref")
        assert r.sifted_bits > 0
        assert abs(r.qber - 0.02) < 0.005  # expected 0.02

    def test_ideal_fidelity_zero_qber(self):
        r = reference_bb84(_scenario(polarization_fidelity=1.0), platform="ref")
        assert r.qber < 0.002


class TestQFabricAgreesWithReference:
    def test_qfabric_matches_reference_qber(self):
        """The QFabric emulator and the independent reference agree on QBER."""
        sc = _scenario()
        qf = run_qfabric_bb84_simulated(sc)
        ref = reference_bb84(sc, platform="ref")
        # Two independently-written codepaths, same physics → close QBER.
        assert abs(qf.qber - ref.qber) < 0.01


class TestBackendStatus:
    def test_unavailable_is_skipped(self):
        r = ValidationResult(platform="sequence", scenario_name="t",
                             extra={"error": "not installed"})
        assert backend_status(r)[0] == "unavailable"

    def test_zero_sifted_is_no_data(self):
        r = ValidationResult(platform="sequence", scenario_name="t", sifted_bits=0)
        assert backend_status(r)[0] == "no_data"

    def test_real_result_is_ok(self):
        r = ValidationResult(platform="qfabric", scenario_name="t",
                             sifted_bits=100, qber=0.01)
        assert backend_status(r)[0] == "ok"


class TestThreeWayComparison:
    def test_three_ok_backends_produce_three_pairs(self):
        results = [
            ValidationResult(platform="qfabric", scenario_name="t", sifted_bits=1000, qber=0.010),
            ValidationResult(platform="sequence", scenario_name="t", sifted_bits=1000, qber=0.011),
            ValidationResult(platform="netsquid", scenario_name="t", sifted_bits=1000, qber=0.009),
        ]
        comp = compare_results(results)
        assert len(comp["comparisons"]) == 3
        assert comp["all_passed"]
