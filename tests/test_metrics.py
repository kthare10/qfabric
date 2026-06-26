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

"""Tests for metrics collection and serialization."""

import tempfile
from pathlib import Path


from qne.metrics import ExperimentMetrics, MetricsCollector


class TestExperimentMetrics:
    """Test metrics data structure."""

    def test_loss_rate(self):
        m = ExperimentMetrics(photons_sent=1000, photons_lost=100)
        assert abs(m.loss_rate - 0.1) < 1e-10

    def test_loss_rate_zero_sent(self):
        m = ExperimentMetrics(photons_sent=0, photons_lost=0)
        assert m.loss_rate == 0.0

    def test_detection_rate(self):
        m = ExperimentMetrics(photons_sent=1000, photons_received=800)
        assert abs(m.detection_rate - 0.8) < 1e-10

    def test_json_round_trip(self):
        m = ExperimentMetrics(
            scenario_name="test",
            photons_sent=1000,
            photons_received=800,
            photons_lost=200,
            sifted_bits=400,
            qber=0.05,
            qber_confidence=(0.03, 0.07),
            raw_key_rate=0.4,
            secure_key_rate=0.3,
            final_key_bits=300,
        )

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name

        m.to_json(path)
        loaded = ExperimentMetrics.from_json(path)

        assert loaded.scenario_name == "test"
        assert loaded.photons_sent == 1000
        assert loaded.qber == 0.05
        assert loaded.qber_confidence == (0.03, 0.07)
        Path(path).unlink()


class TestMetricsCollector:
    """Test metrics collection during an experiment."""

    def test_basic_collection(self):
        collector = MetricsCollector("test_scenario")
        collector.start()

        for _ in range(100):
            collector.record_sent()
        for _ in range(80):
            collector.record_received()
        collector.record_dark_count(2)

        collector.set_sifting_results(
            sifted_bits=40, qber=0.05, confidence=(0.03, 0.07)
        )
        collector.set_key_rate(raw_rate=0.4, secure_rate=0.3, final_bits=30)

        metrics = collector.finalize()

        assert metrics.photons_sent == 100
        assert metrics.photons_received == 80
        assert metrics.photons_lost == 20
        assert metrics.dark_counts == 2
        assert metrics.sifted_bits == 40
        assert metrics.qber == 0.05
        assert metrics.elapsed_seconds >= 0
