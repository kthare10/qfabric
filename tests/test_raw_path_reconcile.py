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

"""Raw-socket path: sample-only disclosure + Cascade + PA over the classical TCP
channel (the photon phase is bypassed by injecting sent/detection logs directly —
AF_PACKET is Linux-only and irrelevant to the sifting/reconciliation logic)."""

from __future__ import annotations

import threading
import time

import numpy as np

from qne.alice import Alice
from qne.bb84 import AliceRecord, BB84Protocol, BobRecord
from qne.bob import Bob
from qne.config import ScenarioConfig
from qne.reconcile import secure_key_bits

_NEXT_PORT = [57281]


def _make_pair(n=4000, qber=0.03, seed=5, auth_key=None, basis_bias=0.5,
               reconcile=True):
    """Alice/Bob with correlated injected logs (error rate ``qber`` on matches)."""
    port = _NEXT_PORT[0]
    _NEXT_PORT[0] += 1
    cfg = ScenarioConfig(name="raw-reconcile-test", seed=seed)
    cfg.protocol.num_photons = n
    cfg.protocol.basis_bias = basis_bias

    alice = Alice(cfg, bob_host="127.0.0.1", bob_port=port, auth_key=auth_key)
    bob = Bob(cfg, classical_host="127.0.0.1", classical_port=port,
              auth_key=auth_key, reconcile=reconcile)

    rng = np.random.default_rng(seed)
    for seq in range(n):
        a_basis = 0 if rng.random() < basis_bias else 1
        a_bit = int(rng.integers(0, 2))
        alice.sent_log.append(AliceRecord(seq, a_basis, a_bit))
        b_basis = 0 if rng.random() < basis_bias else 1
        if b_basis == a_basis:
            b_bit = a_bit ^ (1 if rng.random() < qber else 0)
        else:
            b_bit = int(rng.integers(0, 2))
        bob.detection_log.append(BobRecord(seq, b_basis, b_bit))
    return alice, bob


def _run(alice, bob):
    t = threading.Thread(target=bob._run_sifting)
    t.start()
    time.sleep(0.3)                      # let Bob bind before Alice connects
    alice._run_sifting()
    t.join(timeout=60)
    assert not t.is_alive()


def test_sample_only_disclosure_and_identical_secret():
    alice, bob = _make_pair(n=4000, qber=0.03)
    _run(alice, bob)

    am, bm = alice.collector.metrics, bob.collector.metrics
    # only a 10% sample was disclosed — the rest became key material
    n_sample = BB84Protocol.sample_size(bm.sifted_bits, 0.1)
    key_len = bm.sifted_bits - n_sample
    assert bm.reconciled and am.reconciled
    assert bm.bits_leaked > 0
    # PA output length follows the shared accounting exactly
    expected = secure_key_bits(key_len, bm.qber, bm.bits_leaked, True)
    assert bm.secure_key_bits == expected == am.secure_key_bits
    assert expected > 0
    # both sides extracted the identical secret
    assert alice.final_key is not None
    assert alice.final_key == bob.final_key
    assert am.corrections == bm.corrections > 0


def test_authenticated_run_reconciles():
    alice, bob = _make_pair(n=2000, qber=0.02, seed=9, auth_key="raw-psk")
    _run(alice, bob)
    assert alice.final_key == bob.final_key is not None


def test_above_threshold_aborts_reconciliation():
    alice, bob = _make_pair(n=3000, qber=0.2, seed=11)
    _run(alice, bob)
    assert not bob.collector.metrics.reconciled
    assert bob.final_key is None and alice.final_key is None
    assert bob.collector.metrics.secure_key_bits == 0


def test_biased_bases_raise_sift_ratio_and_still_reconcile():
    bias = 0.9
    alice, bob = _make_pair(n=6000, qber=0.02, seed=13, basis_bias=bias)
    _run(alice, bob)
    bm = bob.collector.metrics
    expected_ratio = bias ** 2 + (1 - bias) ** 2       # 0.82
    assert abs(bm.sifted_bits / 6000 - expected_ratio) < 0.03
    assert bm.sifted_bits / 6000 > 0.5
    assert alice.final_key == bob.final_key is not None


def test_no_reconcile_flag_stops_after_qber():
    alice, bob = _make_pair(n=2000, qber=0.02, seed=17, reconcile=False)
    _run(alice, bob)
    assert not bob.collector.metrics.reconciled
    assert bob.final_key is None
    assert bob.collector.metrics.qber < 0.05       # sifting itself still ran
