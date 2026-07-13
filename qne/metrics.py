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

"""Metrics collection and export for QFabric experiments."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ExperimentMetrics:
    """Collected metrics from a BB84 experiment run."""
    scenario_name: str = ""
    photons_sent: int = 0
    photons_received: int = 0
    photons_lost: int = 0
    dark_counts: int = 0
    sifted_bits: int = 0
    qber: float = 0.0
    qber_confidence: tuple[float, float] = (0.0, 0.0)
    raw_key_rate: float = 0.0
    secure_key_rate: float = 0.0
    final_key_bits: int = 0
    reconciled: bool = False
    corrections: int = 0
    bits_leaked: int = 0
    secure_key_bits: int = 0     # extracted secret length after Cascade + PA
    elapsed_seconds: float = 0.0
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def loss_rate(self) -> float:
        if self.photons_sent == 0:
            return 0.0
        return self.photons_lost / self.photons_sent

    @property
    def detection_rate(self) -> float:
        if self.photons_sent == 0:
            return 0.0
        return self.photons_received / self.photons_sent

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["loss_rate"] = self.loss_rate
        d["detection_rate"] = self.detection_rate
        return d

    def to_json(self, path: str | Path) -> None:
        """Write metrics to a JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def from_json(cls, path: str | Path) -> ExperimentMetrics:
        """Load metrics from a JSON file."""
        with open(path) as f:
            data = json.load(f)
        # Remove computed properties that aren't constructor args
        data.pop("loss_rate", None)
        data.pop("detection_rate", None)
        # Convert tuple
        if "qber_confidence" in data:
            data["qber_confidence"] = tuple(data["qber_confidence"])
        return cls(**data)


class MetricsCollector:
    """Accumulates metrics during an experiment run."""

    def __init__(self, scenario_name: str = ""):
        self.metrics = ExperimentMetrics(scenario_name=scenario_name)
        self._start_time: Optional[float] = None

    def start(self) -> None:
        self._start_time = time.time()
        self.metrics.start_time = time.strftime("%Y-%m-%dT%H:%M:%S")

    def stop(self) -> None:
        if self._start_time is not None:
            self.metrics.elapsed_seconds = time.time() - self._start_time
        self.metrics.end_time = time.strftime("%Y-%m-%dT%H:%M:%S")

    def record_sent(self, count: int = 1) -> None:
        self.metrics.photons_sent += count

    def record_received(self, count: int = 1) -> None:
        self.metrics.photons_received += count

    def record_lost(self, count: int = 1) -> None:
        self.metrics.photons_lost += count

    def record_dark_count(self, count: int = 1) -> None:
        self.metrics.dark_counts += count

    def set_sifting_results(
        self, sifted_bits: int, qber: float, confidence: tuple[float, float]
    ) -> None:
        self.metrics.sifted_bits = sifted_bits
        self.metrics.qber = qber
        self.metrics.qber_confidence = confidence

    def set_key_rate(
        self, raw_rate: float, secure_rate: float, final_bits: int
    ) -> None:
        self.metrics.raw_key_rate = raw_rate
        self.metrics.secure_key_rate = secure_rate
        self.metrics.final_key_bits = final_bits

    def set_reconciliation(
        self, reconciled: bool, corrections: int, bits_leaked: int,
        secure_key_bits: int,
    ) -> None:
        self.metrics.reconciled = reconciled
        self.metrics.corrections = corrections
        self.metrics.bits_leaked = bits_leaked
        self.metrics.secure_key_bits = secure_key_bits

    def set_config(self, config: dict[str, Any]) -> None:
        self.metrics.config = config

    def finalize(self) -> ExperimentMetrics:
        self.stop()
        self.metrics.photons_lost = (
            self.metrics.photons_sent - self.metrics.photons_received
        )
        return self.metrics
