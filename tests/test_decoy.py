# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Komal Thareja
#
# Author: Komal Thareja (kthare10@renci.org)

"""Decoy-state BB84 — Lo-Ma-Chen key-rate bounds and the weak-coherent channel."""

from __future__ import annotations

import numpy as np

from qne.decoy import (
    analytic_gain,
    decoy_state_key_rate,
    detection_probability,
    run_decoy_experiment,
    simulate_intensity,
)


def _clean_channel(eta, mu_s=0.6, mu_d=0.1, mu_v=1e-3):
    """Ideal lossy channel: per-photon detection eta, no dark counts, no errors.
    Analytic gain Q_mu = 1 - exp(-eta*mu); true single-photon yield Y1 = eta."""
    intensities = {"signal": mu_s, "decoy": mu_d, "vacuum": mu_v}
    gains = {k: 1.0 - np.exp(-eta * mu) for k, mu in intensities.items()}
    qbers = {"signal": 0.0, "decoy": 0.0, "vacuum": 0.0}
    return gains, qbers, intensities


def test_detection_probability():
    assert detection_probability(0, 0.5, p_dc=1e-6) == 1e-6      # vacuum -> dark count
    assert abs(detection_probability(1, 0.5, p_dc=0.0) - 0.5) < 1e-12
    assert abs(detection_probability(2, 0.5, p_dc=0.0) - 0.75) < 1e-12  # 1-(1-.5)^2
    assert detection_probability(10, 0.9, p_dc=0.0) <= 1.0


def test_Y1_is_valid_lower_bound_on_clean_channel():
    """On a clean channel true Y1 = eta, so 0 < Y1_lower <= eta; e1 = 0; SKR = q*Q1."""
    eta = 0.3
    r = decoy_state_key_rate(*_clean_channel(eta), f_ec=1.16)
    assert 0.0 < r["Y1_lower"] <= eta + 1e-9
    assert r["e1_upper"] == 0.0
    assert abs(r["secure_key_rate"] - 0.5 * r["Q1"]) < 1e-12
    assert r["secure_key_rate"] > 0.0


def test_skr_decreases_with_qber_and_vanishes_at_threshold():
    gains, _, intensities = _clean_channel(0.3)
    skr0 = decoy_state_key_rate(gains, {"signal": 0.0, "decoy": 0.0, "vacuum": 0.0},
                                intensities)["secure_key_rate"]
    skr_mid = decoy_state_key_rate(gains, {"signal": 0.05, "decoy": 0.05, "vacuum": 0.0},
                                   intensities)["secure_key_rate"]
    skr_hi = decoy_state_key_rate(gains, {"signal": 0.5, "decoy": 0.5, "vacuum": 0.0},
                                  intensities)["secure_key_rate"]
    assert skr0 > skr_mid >= 0.0
    assert skr_hi == 0.0                       # E_s >= 0.5 -> no secure key


def test_corrected_formula_below_truncated_estimator():
    """The full Lo-Ma-Chen Y1 (with Q_s + Y0 terms) must be below the truncated
    form that drops them, which would overestimate Y1."""
    eta, mu_s, mu_d, mu_v = 0.3, 0.6, 0.1, 1e-3
    gains, qbers, intensities = _clean_channel(eta, mu_s, mu_d, mu_v)
    r = decoy_state_key_rate(gains, qbers, intensities)
    denom = mu_s * mu_d - mu_d ** 2
    truncated_Y1 = (mu_s * gains["decoy"] * np.exp(mu_d)
                    - mu_d ** 2 * gains["vacuum"] * np.exp(mu_v)) / denom
    assert r["Y1_lower"] < truncated_Y1


def test_degenerate_inputs_return_zero_key():
    intensities = {"signal": 0.6, "decoy": 0.1, "vacuum": 1e-3}
    r = decoy_state_key_rate({"signal": 0.0, "decoy": 0.0, "vacuum": 0.0},
                             {"signal": 0.0, "decoy": 0.0}, intensities)
    assert r["secure_key_rate"] == 0.0 and r["Y1_lower"] == 0.0


# -- weak-coherent channel simulation --------------------------------------------

def test_simulated_gain_matches_analytic():
    eta, mu = 0.2, 0.6
    gain, qber = simulate_intensity(mu, eta, noise=0.0, num_pulses=40000,
                                    p_dc=0.0, rng=np.random.default_rng(1))
    assert abs(gain - analytic_gain(mu, eta, p_dc=0.0)) < 0.01
    assert qber < 0.005                        # no noise, no dark counts -> ~0


def test_simulated_qber_tracks_noise():
    gain, qber = simulate_intensity(0.6, 0.5, noise=0.06, num_pulses=40000,
                                    p_dc=0.0, rng=np.random.default_rng(2))
    assert abs(qber - 0.03) < 0.01             # depolarizing -> noise/2


def test_run_decoy_experiment_produces_positive_key_low_noise():
    r = run_decoy_experiment(eta=0.3, noise=0.02, num_pulses=40000, seed=3)
    assert r.secure_key_rate > 0.0
    assert 0.0 < r.Y1_lower <= 0.3 + 0.05      # ~ eta within MC noise
    assert r.gains["signal"] > r.gains["decoy"] > r.gains["vacuum"]


def test_run_decoy_experiment_high_noise_no_key():
    r = run_decoy_experiment(eta=0.3, noise=0.30, num_pulses=40000, seed=4)
    assert r.secure_key_rate == 0.0           # QBER ~15% > threshold
