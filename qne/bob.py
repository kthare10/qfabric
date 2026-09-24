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

"""Bob — BB84 detector and receiver.

Listens for photon Ethernet frames on a raw socket, applies detector
model (efficiency, dark counts), and performs classical sifting with Alice.
"""

from __future__ import annotations

import socket

from qne.bb84 import BB84Protocol, BobRecord
from qne.channel import ClassicalServer
from qne.config import ScenarioConfig
from qne.detector import Detector
from qne.metrics import MetricsCollector
from qne.photon import PhotonPacket
from qne.channel import ProtocolError
from qne.finite_key import finite_key_length
from qne.reconcile import (ChannelRpc, KeyVerificationError, bits_to_int,
                           drive_cascade)


class Bob:
    """BB84 receiver (detector).

    Attributes:
        config: Experiment scenario configuration.
        interface: Network interface for raw socket (e.g., "veth3").
        classical_host: Host for classical channel server.
        classical_port: Port for classical channel server.
    """

    def __init__(
        self,
        config: ScenarioConfig,
        interface: str = "veth3",
        classical_host: str = "0.0.0.0",
        classical_port: int = 5100,
        auth_key: bytes | str | None = None,
        reconcile: bool = True,
        finite_key: bool = False,
        eps_sec: float = 1e-9,
        eps_cor: float = 1e-15,
        accept_timeout: float | None = None,
        data_timeout: float | None = 600.0,
    ):
        self.config = config
        self.interface = interface
        self.classical_host = classical_host
        self.classical_port = classical_port
        self.auth_key = auth_key
        self.reconcile = reconcile
        # finite_key: size privacy amplification with the finite-key bound
        # (qne/finite_key.py) instead of the asymptotic accounting.
        self.finite_key = finite_key
        self.eps_sec = eps_sec
        self.eps_cor = eps_cor
        self.accept_timeout = accept_timeout
        self.data_timeout = data_timeout
        self.finite_key_result = None
        self.final_key: int | None = None    # extracted secret (post Cascade + PA)
        self.detector = Detector(
            efficiency=config.detector.efficiency,
            dark_count_rate=config.detector.dark_count_rate,
            detection_window=config.detector.detection_window,
            polarization_error=1.0 - config.channel.polarization_fidelity,
            seed=config.derived_seed(100),
            basis_bias=config.protocol.basis_bias,
            dead_time=config.detector.dead_time,
            timing_jitter=config.detector.timing_jitter,
            # slot spacing from the source rate — anchors dead-time gating
            pulse_period_ns=(1e9 / config.protocol.send_rate_hz
                             if config.protocol.send_rate_hz > 0 else 0.0),
        )
        self.detection_log: list[BobRecord] = []
        self.collector = MetricsCollector(config.name)

    def run(self) -> None:
        """Execute the full BB84 receiver protocol.

        1. Listen for photon packets on raw socket.
        2. Apply detector model to each received photon.
        3. Accept Alice's classical channel connection.
        4. Perform sifting and compute QBER.
        """
        self.collector.start()
        self.collector.set_config(self.config.to_dict())

        # Phase 1: Receive photons
        self._receive_photons()

        # Phase 2: Classical sifting
        self._run_sifting()

        # Bob doesn't transmit, but it knows the intended photon count from the
        # scenario — record it so the result reports photons_sent / loss_rate
        # correctly (otherwise photons_sent would be 0 and loss_rate negative).
        self.collector.record_sent(self.config.protocol.num_photons)
        metrics = self.collector.finalize()
        print("\n=== Bob Results ===")
        print(f"  Photons received (raw): {len(self.detection_log)}")
        print(f"  Sifted bits:            {metrics.sifted_bits}")
        print(f"  QBER:                   {metrics.qber:.4f}")
        print(f"  Secure key rate:        {metrics.secure_key_rate:.4f}")
        print(f"  Final key bits:         {metrics.final_key_bits}")
        if metrics.reconciled:
            print(f"  Reconciled:             yes ({metrics.corrections} corrections, "
                  f"{metrics.bits_leaked} bits leaked)")
            print(f"  Secure key bits:        {metrics.secure_key_bits}")
        print(f"  Elapsed:                {metrics.elapsed_seconds:.2f}s")

        return metrics

    def _receive_photons(self) -> None:
        """Listen for photon frames and apply detector model."""
        sock = socket.socket(
            socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x7101)
        )
        sock.bind((self.interface, 0))
        sock.settimeout(30.0)  # Timeout after 30s of silence (allow SSH startup)

        print(f"Bob: Listening for photons on {self.interface}")

        received_count = 0
        self._arrived_seqs: set[int] = set()
        while True:
            try:
                frame, _addr = sock.recvfrom(65535)
            except socket.timeout:
                print(f"Bob: Timeout — received {received_count} photons")
                break

            try:
                photon = PhotonPacket.from_ethernet_frame(frame)
            except ValueError:
                continue  # Not a photon frame

            # Apply detector model
            self._arrived_seqs.add(photon.sequence_num)
            event = self.detector.detect(photon)

            if event.detected:
                self.detection_log.append(BobRecord(
                    sequence_num=event.sequence_num,
                    basis=event.basis,
                    bit_value=event.bit_value,
                ))
                self.collector.record_received()
                if event.is_dark_count:
                    self.collector.record_dark_count()

            received_count += 1

        sock.close()

        # A gated detector dark-counts in EVERY slot, not only where a frame
        # arrived. Slots whose photon was lost in the channel (never seen here)
        # still get a chance to click; without this the model under-reports dark
        # counts exactly in the high-loss regime where they dominate QBER, and
        # disagrees with the NetSquid / distributed-BB84 paths, which do model it.
        seen = {rec.sequence_num for rec in self.detection_log}
        seen.update(self._arrived_seqs)
        dark_events = 0
        for seq in range(self.config.protocol.num_photons):
            if seq in seen:
                continue
            if self.detector.rng.random() < self.detector.dark_count_prob:
                self.detection_log.append(BobRecord(
                    sequence_num=seq,
                    basis=int(self.detector.rng.random() >= self.detector.basis_bias),
                    bit_value=int(self.detector.rng.integers(0, 2)),
                ))
                self.collector.record_received()
                self.collector.record_dark_count()
                dark_events += 1
        print(
            f"Bob: {received_count} photons arrived, "
            f"{len(self.detection_log) - dark_events} detected, "
            f"{dark_events} dark counts in empty slots"
        )

    def _run_sifting(self) -> None:
        """Accept Alice's connection; sift, estimate QBER, reconcile, amplify.

        Only a random SAMPLE of the sifted positions is disclosed for QBER
        estimation (the rest stays secret — key material), then Bob drives
        Cascade against Alice as a parity oracle and both extract the identical
        Toeplitz-amplified secret. All math is shared: BB84Protocol for the
        sifting/QBER accounting, qne.reconcile for Cascade + PA.
        """
        server = ClassicalServer(self.classical_host, self.classical_port,
                                 auth_key=self.auth_key,
                                 accept_timeout=self.accept_timeout,
                                 data_timeout=self.data_timeout)
        server.start()
        print(f"Bob: Waiting for Alice on {self.classical_host}:{self.classical_port}")

        channel = server.accept()
        print("Bob: Alice connected")

        protocol = BB84Protocol(
            sample_fraction=self.config.protocol.sample_fraction,
            seed=self.config.derived_seed(1),
        )

        try:
            # Receive Alice's basis list
            msg = channel.recv_message()
            if msg.get("type") != "alice_bases":
                raise ProtocolError(f"expected alice_bases, got {msg.get('type')!r}")
            alice_bases = {int(k): v for k, v in msg["bases"].items()}

            # Find matching bases. Dedup by sequence number — switch flooding
            # or loops can deliver the same photon frame twice. Sorted, so both
            # sides derive the identical key order.
            bob_by_seq: dict[int, BobRecord] = {}
            detected_sequences = []
            for bob_rec in self.detection_log:
                if bob_rec.sequence_num in bob_by_seq:
                    continue
                bob_by_seq[bob_rec.sequence_num] = bob_rec
                detected_sequences.append(bob_rec.sequence_num)
            matching = sorted(
                seq for seq, rec in bob_by_seq.items()
                if alice_bases.get(seq) == rec.basis
            )

            # Send sifting result to Alice
            channel.send_message({
                "type": "sifting_result",
                "matching_indices": matching,
                "detected_sequences": detected_sequences,
            })

            # Disclose only a random sample: Bob picks the positions, Alice
            # returns her bits there; everything else stays secret.
            bias = self.config.protocol.basis_bias
            if bias != 0.5:
                # Efficient BB84: the key comes from Z-Z matches ONLY. Every X-X
                # match is disclosed (phase-error estimate e_x) plus a random
                # sample of the Z-Z matches (bit-error estimate e_z). Keeping
                # X-X bits in the key would leave them outside the 1-h(e_z)-h(e_x)
                # bound (same policy as qne_sequence.distributed_qkd).
                matching_z = [s for s in matching if bob_by_seq[s].basis == 0]
                matching_x = [s for s in matching if bob_by_seq[s].basis == 1]
                n_sample = BB84Protocol.sample_size(
                    len(matching_z), self.config.protocol.sample_fraction)
                z_sample = sorted(protocol.rng.choice(
                    matching_z, size=n_sample, replace=False).tolist()) if n_sample else []
                sample = sorted(matching_x + z_sample)
            else:
                n_sample = BB84Protocol.sample_size(
                    len(matching), self.config.protocol.sample_fraction)
                sample = sorted(protocol.rng.choice(
                    matching, size=n_sample, replace=False).tolist()) if n_sample else []
            channel.send_message({
                "type": "request_sample",
                "sample_indices": sample,
            })

            sample_msg = channel.recv_message()
            if sample_msg.get("type") == "alice_sample_bits":
                alice_sample_bits = list(sample_msg["bits"])
            else:
                alice_sample_bits = []  # protocol violation -> no verified key material
                sample = []

            bob_sample_bits = [bob_by_seq[s].bit_value for s in sample]
            qber_est = BB84Protocol.qber_from_disclosed(
                alice_sample_bits, bob_sample_bits)

            # Key = sifted minus disclosed; the disclosed sample is public.
            sample_set = set(sample)
            key_order = [s for s in matching if s not in sample_set]
            key_bits = [bob_by_seq[s].bit_value for s in key_order]

            # Efficient BB84 (biased bases): split the sample by basis — Z is the
            # bit error, X the phase error — and rate with 1 - h(e_z) - h(e_x).
            qber_pa = None          # PA erases the PHASE error; unbiased: same as Q
            if bias != 0.5 and sample:
                zi = [i for i, s in enumerate(sample) if alice_bases[s] == 0]
                xi = [i for i, s in enumerate(sample) if alice_bases[s] == 1]
                qz = BB84Protocol.qber_from_disclosed(
                    [alice_sample_bits[i] for i in zi], [bob_sample_bits[i] for i in zi])
                qx = BB84Protocol.qber_from_disclosed(
                    [alice_sample_bits[i] for i in xi], [bob_sample_bits[i] for i in xi])
                if qz.num_sampled and qx.num_sampled:
                    secure_fraction = BB84Protocol.efficient_secure_fraction(
                        qz.qber, qx.qber)
                    qber_pa = qx.qber
                else:
                    secure_fraction = 0.0
            else:
                secure_fraction = BB84Protocol.secure_key_fraction(qber_est.qber)

            num_photons = self.config.protocol.num_photons
            final_key_bits = int(len(key_order) * secure_fraction)
            raw_key_rate = len(matching) / num_photons if num_photons else 0.0
            secure_key_rate = final_key_bits / num_photons if num_photons else 0.0

            # Above the ~11% threshold there is no secure key — skip Cascade.
            do_reconcile = bool(self.reconcile and key_order and secure_fraction > 0)
            channel.send_message({
                "type": "qber_result",
                "qber": qber_est.qber,
                "num_sampled": qber_est.num_sampled,
                "num_errors": qber_est.num_errors,
                "confidence_interval": list(qber_est.confidence_interval),
                "raw_key_rate": raw_key_rate,
                "secure_key_rate": secure_key_rate,
                "final_key_bits": final_key_bits,
                "reconcile": do_reconcile,
            })

            # Cascade + privacy amplification over the same channel: Bob drives,
            # Alice serves parities; both extract the identical secret.
            reconciled = False
            corrections = bits_leaked = 0
            final = key_bits
            if do_reconcile:
                finite = None
                if self.finite_key:
                    finite = {"n_sample": qber_est.num_sampled,
                              "eps_sec": self.eps_sec, "eps_cor": self.eps_cor}
                    self.finite_key_result = finite_key_length(
                        len(key_bits), qber_est.num_sampled, qber_est.qber,
                        leak_ec=0.0, eps_sec=self.eps_sec, eps_cor=self.eps_cor)
                try:
                    final, corrections, bits_leaked = drive_cascade(
                        ChannelRpc(channel), key_bits, qber_est.qber,
                        self.config.derived_seed(303), finite=finite, qber_pa=qber_pa)
                    reconciled = True
                except KeyVerificationError as e:
                    corrections, bits_leaked = e.corrections, e.bits_leaked
                    final, reconciled = [], False
                    print(f"Bob: key verification FAILED after Cascade ({e})")
            self.final_key = bits_to_int(final) if reconciled else None

            self.collector.set_sifting_results(
                sifted_bits=len(matching),
                qber=qber_est.qber,
                confidence=qber_est.confidence_interval,
            )
            self.collector.set_key_rate(
                raw_rate=raw_key_rate,
                secure_rate=secure_key_rate,
                final_bits=final_key_bits,
            )
            self.collector.set_reconciliation(
                reconciled, corrections, bits_leaked,
                len(final) if reconciled else 0,
            )

        finally:
            channel.close()
            server.close()
