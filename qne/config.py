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

"""Scenario configuration and YAML loading for QFabric experiments."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DetectorConfig:
    """Detector model parameters."""
    efficiency: float = 0.8
    dark_count_rate: float = 10.0  # Hz
    dead_time: float = 0.0  # nanoseconds
    timing_jitter: float = 0.0  # nanoseconds


@dataclass
class ChannelConfig:
    """Quantum channel parameters."""
    distance_km: float = 1.0
    attenuation_db_per_km: float = 0.2
    polarization_fidelity: float = 1.0


@dataclass
class ProtocolConfig:
    """BB84 protocol parameters."""
    num_photons: int = 100_000
    send_rate_hz: float = 1_000_000.0  # photons per second
    sample_fraction: float = 0.1  # fraction of sifted bits used for QBER estimation
    wavelength: int = 0  # channel tag


@dataclass
class ScenarioConfig:
    """Complete experiment scenario configuration.

    Attributes:
        name: Human-readable scenario name.
        channel: Quantum channel parameters.
        detector: Detector model parameters.
        protocol: BB84 protocol parameters.
        seed: Random seed for reproducibility.
    """
    name: str = "default"
    channel: ChannelConfig = field(default_factory=ChannelConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    protocol: ProtocolConfig = field(default_factory=ProtocolConfig)
    seed: int = 42

    @property
    def loss_probability(self) -> float:
        """Compute fiber loss probability: P(loss) = 1 - 10^(-alpha*L/10)."""
        alpha = self.channel.attenuation_db_per_km
        distance = self.channel.distance_km
        return 1.0 - 10 ** (-(alpha * distance) / 10.0)

    @property
    def loss_threshold_u32(self) -> int:
        """Compute 32-bit loss threshold for P4 random comparison.

        Returns an integer in [0, 2^32) such that if a uniform random
        32-bit number is less than this threshold, the photon is dropped.
        """
        return int(self.loss_probability * (2**32))

    @classmethod
    def from_yaml(cls, path: str | Path) -> ScenarioConfig:
        """Load a scenario configuration from a YAML file."""
        path = Path(path)
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioConfig:
        """Create a ScenarioConfig from a dictionary."""
        channel_data = data.get("channel", {})
        detector_data = data.get("detector", {})
        protocol_data = data.get("protocol", {})

        return cls(
            name=data.get("name", "default"),
            channel=ChannelConfig(
                distance_km=channel_data.get("distance_km", 1.0),
                attenuation_db_per_km=channel_data.get("attenuation_db_per_km", 0.2),
                polarization_fidelity=channel_data.get("polarization_fidelity", 1.0),
            ),
            detector=DetectorConfig(
                efficiency=detector_data.get("efficiency", 0.8),
                dark_count_rate=detector_data.get("dark_count_rate", 10.0),
                dead_time=detector_data.get("dead_time", 0.0),
                timing_jitter=detector_data.get("timing_jitter", 0.0),
            ),
            protocol=ProtocolConfig(
                num_photons=protocol_data.get("num_photons", 100_000),
                send_rate_hz=protocol_data.get("send_rate_hz", 1_000_000.0),
                sample_fraction=protocol_data.get("sample_fraction", 0.1),
                wavelength=protocol_data.get("wavelength", 0),
            ),
            seed=data.get("seed", 42),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dictionary suitable for YAML output."""
        return {
            "name": self.name,
            "channel": {
                "distance_km": self.channel.distance_km,
                "attenuation_db_per_km": self.channel.attenuation_db_per_km,
                "polarization_fidelity": self.channel.polarization_fidelity,
            },
            "detector": {
                "efficiency": self.detector.efficiency,
                "dark_count_rate": self.detector.dark_count_rate,
                "dead_time": self.detector.dead_time,
                "timing_jitter": self.detector.timing_jitter,
            },
            "protocol": {
                "num_photons": self.protocol.num_photons,
                "send_rate_hz": self.protocol.send_rate_hz,
                "sample_fraction": self.protocol.sample_fraction,
                "wavelength": self.protocol.wavelength,
            },
            "seed": self.seed,
        }
