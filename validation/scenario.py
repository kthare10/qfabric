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

"""Shared validation scenario definition.

Provides a platform-neutral scenario description that can be mapped
to QFabric, SeQUeNCe, and NetSquid parameter spaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ValidationScenario:
    """Platform-independent scenario for cross-validation.

    This maps to equivalent configurations in QFabric (P4 + Python),
    SeQUeNCe, and NetSquid.
    """
    name: str
    distance_km: float
    attenuation_db_per_km: float = 0.2
    detector_efficiency: float = 0.8
    dark_count_rate_hz: float = 10.0
    polarization_fidelity: float = 1.0
    num_photons: int = 100_000
    sample_fraction: float = 0.1
    seed: int = 42

    @property
    def expected_loss_probability(self) -> float:
        """Fiber loss: P(loss) = 1 - 10^(-alpha*L/10)."""
        return 1.0 - 10 ** (-(self.attenuation_db_per_km * self.distance_km) / 10.0)

    def to_flat_dict(self) -> dict[str, Any]:
        """Flat dict (keys == field names) so it round-trips through from_yaml().

        Used to hand a scenario to a backend running in a separate Python env.
        """
        return {
            "name": self.name,
            "distance_km": self.distance_km,
            "attenuation_db_per_km": self.attenuation_db_per_km,
            "detector_efficiency": self.detector_efficiency,
            "dark_count_rate_hz": self.dark_count_rate_hz,
            "polarization_fidelity": self.polarization_fidelity,
            "num_photons": self.num_photons,
            "sample_fraction": self.sample_fraction,
            "seed": self.seed,
        }

    @classmethod
    def from_yaml(cls, path: str | Path) -> ValidationScenario:
        """Load from a YAML file.

        Supports both flat format (ValidationScenario fields directly)
        and nested format (ScenarioConfig-style with channel/detector/protocol sections).
        """
        with open(path) as f:
            data = yaml.safe_load(f)

        # Detect nested ScenarioConfig format
        if "channel" in data or "detector" in data or "protocol" in data:
            channel = data.get("channel", {})
            detector = data.get("detector", {})
            protocol = data.get("protocol", {})
            return cls(
                name=data.get("name", "default"),
                distance_km=channel.get("distance_km", 1.0),
                attenuation_db_per_km=channel.get("attenuation_db_per_km", 0.2),
                polarization_fidelity=channel.get("polarization_fidelity", 1.0),
                detector_efficiency=detector.get("efficiency", 0.8),
                dark_count_rate_hz=detector.get("dark_count_rate", 10.0),
                num_photons=protocol.get("num_photons", 100_000),
                sample_fraction=protocol.get("sample_fraction", 0.1),
                seed=data.get("seed", 42),
            )

        return cls(**data)

    @classmethod
    def load_sweep(cls, path: str | Path) -> list[ValidationScenario]:
        """Load a sweep YAML that defines multiple scenarios.

        Expected format:
            sweep:
                parameter: distance_km
                values: [1, 5, 10, 20, 50, 100]
            base:
                attenuation_db_per_km: 0.2
                detector_efficiency: 0.8
                ...
        """
        with open(path) as f:
            data = yaml.safe_load(f)

        sweep_cfg = data["sweep"]
        param = sweep_cfg["parameter"]
        values = sweep_cfg["values"]
        base = data.get("base", {})

        scenarios = []
        for val in values:
            kwargs = dict(base)
            kwargs[param] = val
            kwargs.setdefault("name", f"{param}={val}")
            scenarios.append(cls(**kwargs))
        return scenarios


@dataclass
class ValidationResult:
    """Results from running a scenario on one platform."""
    platform: str  # "qfabric", "sequence", or "netsquid"
    scenario_name: str
    photons_sent: int = 0
    photons_received: int = 0
    sifted_bits: int = 0
    qber: float = 0.0
    raw_key_rate: float = 0.0
    secure_key_rate: float = 0.0
    elapsed_seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "scenario_name": self.scenario_name,
            "photons_sent": self.photons_sent,
            "photons_received": self.photons_received,
            "sifted_bits": self.sifted_bits,
            "qber": self.qber,
            "raw_key_rate": self.raw_key_rate,
            "secure_key_rate": self.secure_key_rate,
            "elapsed_seconds": self.elapsed_seconds,
            **self.extra,
        }

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe dict that keeps `extra` nested, for cross-process transfer."""
        return {
            "platform": self.platform,
            "scenario_name": self.scenario_name,
            "photons_sent": self.photons_sent,
            "photons_received": self.photons_received,
            "sifted_bits": self.sifted_bits,
            "qber": self.qber,
            "raw_key_rate": self.raw_key_rate,
            "secure_key_rate": self.secure_key_rate,
            "elapsed_seconds": self.elapsed_seconds,
            "extra": self.extra,
        }

    @classmethod
    def from_payload(cls, d: dict[str, Any]) -> ValidationResult:
        """Reconstruct from a to_payload() dict (e.g. a subprocess backend's output)."""
        d = dict(d)
        known = {
            "platform", "scenario_name", "photons_sent", "photons_received",
            "sifted_bits", "qber", "raw_key_rate", "secure_key_rate",
            "elapsed_seconds", "extra",
        }
        return cls(**{k: v for k, v in d.items() if k in known})
