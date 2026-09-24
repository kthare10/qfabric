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

import json
from unittest.mock import Mock

import pytest

from qne.config import ChannelConfig, ScenarioConfig
from scripts import deploy_fabric
from validation import compare
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
    @pytest.mark.parametrize("results", [
        [],
        [ValidationResult(platform="qfabric", scenario_name="t", sifted_bits=100)],
    ])
    def test_no_pairs_is_inconclusive(self, results):
        comp = compare_results(results)
        assert comp["comparisons"] == []
        assert comp["inconclusive"]
        assert not comp["all_passed"]

    def test_three_ok_backends_produce_three_pairs(self):
        results = [
            ValidationResult(platform="qfabric", scenario_name="t", sifted_bits=1000, qber=0.010),
            ValidationResult(platform="sequence", scenario_name="t", sifted_bits=1000, qber=0.011),
            ValidationResult(platform="netsquid", scenario_name="t", sifted_bits=1000, qber=0.009),
        ]
        comp = compare_results(results)
        assert len(comp["comparisons"]) == 3
        assert not comp["inconclusive"]
        assert comp["all_passed"]

    @pytest.mark.parametrize("scenarios", [[], [_scenario()]])
    def test_inconclusive_sweep_exits_nonzero(self, scenarios, monkeypatch, capsys):
        monkeypatch.setattr(compare.sys, "argv", ["compare", "sweep_distance.yml"])
        monkeypatch.setattr(ValidationScenario, "load_sweep", Mock(return_value=scenarios))
        monkeypatch.setattr(compare, "run_all_platforms", Mock(return_value=[]))
        with pytest.raises(SystemExit) as error:
            compare.main()
        assert error.value.code == 1
        assert "INCONCLUSIVE" in capsys.readouterr().out


class TestSharedLossModel:
    @pytest.mark.parametrize("distance_km", [0, 0.01, 1, 5, 10, 100, 10_000])
    def test_helpers_match_config(self, distance_km):
        config = ScenarioConfig(channel=ChannelConfig(
            distance_km=distance_km, attenuation_db_per_km=0.2,
        ))
        assert deploy_fabric.loss_probability(distance_km, 0.2) == config.loss_probability
        assert _scenario(distance_km=distance_km).expected_loss_probability == config.loss_probability


class TestFabricLossUpdates:
    @pytest.mark.parametrize("action_data, threshold", [
        ("0x0, 0x1", 0),
        ("0xabc, 0x1", 0xABC),
        ("0XABC, 0x1", 0xABC),
        ("193273528, 1", 193273528),
        ("0xffffffff, 0x1", 2**32 - 1),
    ])
    def test_verify_numeric_action_data(self, action_data, threshold):
        slice_obj = Mock()
        switch = slice_obj.get_node.return_value
        switch.get_interface.return_value.get_mac.return_value = "02:00:00:00:00:02"
        switch.execute.side_effect = [
            ("bmv2\n", ""),
            ("", ""),
            (f"Action entry: Ingress.set_channel_params - {action_data}\n", ""),
        ]
        deploy_fabric.set_channel_loss(slice_obj, threshold)
        assert "table_modify" in switch.execute.call_args_list[1].args[0]
        assert "sudo docker exec -i bmv2 simple_switch_CLI" in switch.execute.call_args.args[0]

    @pytest.mark.parametrize("dump", [
        "Action entry: port_forward - 0xabc\n",
        "Action entry: set_channel_params - 0x1abc, 1\n",
        "Entry handle: 2748\nAction entry: set_channel_params - 0, 1\n",
        "",
    ])
    def test_missing_threshold_fails(self, dump):
        slice_obj = Mock()
        switch = slice_obj.get_node.return_value
        switch.get_interface.return_value.get_mac.return_value = "02:00:00:00:00:02"
        switch.execute.side_effect = [("", ""), ("", ""), (dump, "")]
        with pytest.raises(RuntimeError, match="did not take"):
            deploy_fabric.set_channel_loss(slice_obj, 0xABC)

    def test_running_container_is_reused(self, monkeypatch):
        monkeypatch.delenv("QFABRIC_BMV2_IMAGE", raising=False)
        slice_obj = Mock()
        switch = slice_obj.get_node.return_value
        switch.get_interface.return_value.get_mac.return_value = "02:00:00:00:00:02"
        switch.execute.side_effect = lambda command, **kwargs: (
            "bmv2\n" if "docker ps" in command else "/home/test\n", "",
        )
        deploy_fabric.configure_switch(slice_obj, 123)
        commands = [call.args[0] for call in switch.execute.call_args_list]
        assert not any(
            forbidden in command for command in commands
            for forbidden in ("docker run", "pkill", "p4c-bm2-ss", "systemctl")
        )
        table_commands = [command for command in commands if "table_add" in command]
        assert len(table_commands) == 5
        assert all("sudo docker exec -i bmv2 simple_switch_CLI" in cmd for cmd in table_commands)

    def test_sweep_configures_once_and_records_failures(self, tmp_path, monkeypatch):
        scenarios_dir = tmp_path / "validation" / "scenarios"
        scenarios_dir.mkdir(parents=True)
        (scenarios_dir / "sweep_distance.yml").write_text(
            "sweep:\n  parameter: distance_km\n  values: [1, 10]\n",
        )
        monkeypatch.setattr(deploy_fabric, "PROJECT_DIR", tmp_path)
        calls = Mock()
        calls.configure.return_value = ("alice", "bob", "switch_a", "switch_b", "eth0", "eth1")
        calls.run.side_effect = [RuntimeError("BB84 failed"), None]
        monkeypatch.setattr(deploy_fabric, "configure_switch", calls.configure)
        monkeypatch.setattr(deploy_fabric, "set_channel_loss", calls.loss)
        monkeypatch.setattr(deploy_fabric, "run_bb84", calls.run)
        rows = deploy_fabric.run_all_scenarios_on_fabric(Mock(), cross_validate=False)

        assert [call[0] for call in calls.mock_calls] == [
            "configure", "loss", "run", "loss", "run",
        ]
        thresholds = [ScenarioConfig(channel=ChannelConfig(distance_km=distance)).loss_threshold_u32
                      for distance in (1, 10)]
        assert calls.configure.call_args.args[1] == thresholds[0]
        assert [call.args[1] for call in calls.loss.call_args_list] == thresholds
        assert rows[0]["error"] == "BB84 failed"
        assert "Traceback (most recent call last)" in rows[0]["traceback"]
        assert "RuntimeError: BB84 failed" in rows[0]["traceback"]
        assert "error" not in rows[1]
        assert "traceback" not in rows[1]
        assert json.loads((tmp_path / "results" / "all_scenarios.json").read_text()) == rows
