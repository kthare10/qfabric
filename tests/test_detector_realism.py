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

"""Detector realism: dead time, timing jitter, basis bias, multi-photon pulses."""

from __future__ import annotations

import math

import numpy as np

from qne.detector import Detector
from qne.photon import PhotonPacket


def _photons(n):
    return [PhotonPacket(basis=0, state=0, sequence_num=i) for i in range(n)]


class TestDeadTime:
    def test_dead_time_blocks_following_slots(self):
        # period 100 ns, dead 250 ns -> each click blinds the next 2 slots:
        # with a perfect detector, exactly every 3rd pulse is detected.
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=1,
                       dead_time=250.0, pulse_period_ns=100.0)
        events = det.detect_batch(_photons(999))
        detected = [e for e in events if e.detected]
        dead = [e for e in events if e.is_dead_time]
        assert len(detected) == 333
        assert len(dead) == 666
        assert det.dead_time_drops == 666
        assert all(e.sequence_num % 3 == 0 for e in detected)

    def test_dead_time_shorter_than_period_is_free(self):
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=1,
                       dead_time=50.0, pulse_period_ns=100.0)
        events = det.detect_batch(_photons(1000))
        assert sum(e.detected for e in events) == 1000

    def test_dead_time_needs_arrival_times(self):
        # without a pulse period (or explicit arrival time) gating is inert
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=1, dead_time=250.0)
        events = det.detect_batch(_photons(100))
        assert sum(e.detected for e in events) == 100

    def test_dark_count_rearms_dead_time(self):
        det = Detector(efficiency=0.0, dark_count_rate=1e9, detection_window=1e-9,
                       seed=3, dead_time=1000.0, pulse_period_ns=100.0)
        events = det.detect_batch(_photons(500))
        darks = [e for e in events if e.is_dark_count]
        assert darks                                 # p_dark = 1 -> clicks happen
        for a, b in zip(darks, darks[1:]):           # ...but never inside the dead window
            assert (b.sequence_num - a.sequence_num) * 100.0 >= 1000.0


class TestTimingJitter:
    def test_jitter_reduces_efficiency_by_erf_factor(self):
        # sigma = window: pass prob = erf(w/(2*sqrt(2)*sigma)) = erf(0.5/sqrt(2)) ~ 0.383
        det = Detector(efficiency=1.0, dark_count_rate=0, detection_window=1e-9,
                       timing_jitter=1.0, seed=5)
        n = 20_000
        events = det.detect_batch(_photons(n))
        rate = sum(e.detected for e in events) / n
        expected = det.jitter_pass_probability()
        assert abs(expected - math.erf(0.5 / math.sqrt(2))) < 1e-12
        sigma = math.sqrt(expected * (1 - expected) / n)
        assert abs(rate - expected) < 4 * sigma

    def test_small_jitter_is_harmless(self):
        det = Detector(efficiency=1.0, dark_count_rate=0, detection_window=1e-9,
                       timing_jitter=0.05, seed=5)
        events = det.detect_batch(_photons(5000))
        assert sum(e.detected for e in events) == 5000
        assert det.jitter_pass_probability() > 0.999999


class TestBasisBias:
    def test_biased_basis_choice_frequency(self):
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=7, basis_bias=0.9)
        n = 20_000
        events = det.detect_batch(_photons(n))
        z_frac = sum(1 for e in events if e.basis == 0) / n
        assert abs(z_frac - 0.9) < 0.01

    def test_unbiased_default(self):
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=7)
        n = 20_000
        events = det.detect_batch(_photons(n))
        z_frac = sum(1 for e in events if e.basis == 0) / n
        assert abs(z_frac - 0.5) < 0.02


class TestMultiPhotonPulse:
    def test_vacuum_pulse_only_dark_counts(self):
        det = Detector(efficiency=1.0, dark_count_rate=0, seed=9)
        events = [det.detect_pulse(p, 0) for p in _photons(1000)]
        assert not any(e.detected for e in events)

    def test_multiphoton_pulse_boosts_detection(self):
        # eta = 0.3: n=1 -> 0.3, n=3 -> 1-(0.7)^3 = 0.657
        rng_n = 30_000
        det = Detector(efficiency=0.3, dark_count_rate=0, seed=11)
        det3 = Detector(efficiency=0.3, dark_count_rate=0, seed=12)
        r1 = np.mean([det.detect_pulse(p, 1).detected for p in _photons(rng_n)])
        r3 = np.mean([det3.detect_pulse(p, 3).detected for p in _photons(rng_n)])
        assert abs(r1 - 0.3) < 0.01
        assert abs(r3 - (1 - 0.7 ** 3)) < 0.01
