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

from qne.bb84 import BB84Protocol, BobRecord, SiftingResult
from qne.channel import ClassicalServer
from qne.config import ScenarioConfig
from qne.detector import Detector
from qne.metrics import MetricsCollector
from qne.photon import PhotonPacket


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
    ):
        self.config = config
        self.interface = interface
        self.classical_host = classical_host
        self.classical_port = classical_port
        self.detector = Detector(
            efficiency=config.detector.efficiency,
            dark_count_rate=config.detector.dark_count_rate,
            polarization_error=1.0 - config.channel.polarization_fidelity,
            seed=config.seed + 100,
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
        print(
            f"Bob: {received_count} photons arrived, "
            f"{len(self.detection_log)} detected"
        )

    def _run_sifting(self) -> None:
        """Accept Alice's connection and perform sifting."""
        server = ClassicalServer(self.classical_host, self.classical_port)
        server.start()
        print(f"Bob: Waiting for Alice on {self.classical_host}:{self.classical_port}")

        channel = server.accept()
        print("Bob: Alice connected")

        # All sifting/QBER/key-rate math lives in BB84Protocol — the same code
        # tests/test_bb84.py exercises. Bob only moves messages and builds the
        # aligned bit lists.
        protocol = BB84Protocol(
            sample_fraction=self.config.protocol.sample_fraction,
            seed=self.config.seed + 1,
        )

        try:
            # Receive Alice's basis list
            msg = channel.recv_message()
            assert msg["type"] == "alice_bases"
            alice_bases = {int(k): v for k, v in msg["bases"].items()}

            # Find matching bases. Dedup by sequence number — switch flooding
            # or loops can deliver the same photon frame twice.
            matching_indices = []
            bob_bits = []
            detected_sequences = []
            seen: set[int] = set()

            for bob_rec in self.detection_log:
                if bob_rec.sequence_num in seen:
                    continue
                seen.add(bob_rec.sequence_num)
                detected_sequences.append(bob_rec.sequence_num)
                alice_basis = alice_bases.get(bob_rec.sequence_num)
                if alice_basis is not None and alice_basis == bob_rec.basis:
                    matching_indices.append(bob_rec.sequence_num)
                    bob_bits.append(bob_rec.bit_value)

            # Send sifting result to Alice
            channel.send_message({
                "type": "sifting_result",
                "matching_indices": matching_indices,
                "detected_sequences": detected_sequences,
            })

            # Ask Alice for her bits at the matched positions. (In real QKD only
            # a sample is revealed; here Alice sends all matched bits and the
            # protocol samples locally for QBER estimation.)
            channel.send_message({
                "type": "request_sample",
                "matching_indices": matching_indices,
            })

            sample_msg = channel.recv_message()
            if sample_msg.get("type") == "alice_sample_bits":
                alice_bits = list(sample_msg["bits"])
            else:
                alice_bits = []  # protocol violation -> no verified key material

            sifted = SiftingResult(
                alice_bits=alice_bits,
                bob_bits=bob_bits[: len(alice_bits)],
                matching_indices=matching_indices[: len(alice_bits)],
            )
            qber_est = protocol.estimate_qber(sifted)
            key_rate = protocol.compute_key_rate(
                sifted, qber_est, self.config.protocol.num_photons
            )

            # Send QBER result back to Alice
            channel.send_message({
                "type": "qber_result",
                "qber": qber_est.qber,
                "num_sampled": qber_est.num_sampled,
                "num_errors": qber_est.num_errors,
                "confidence_interval": list(qber_est.confidence_interval),
                "raw_key_rate": key_rate.raw_key_rate,
                "secure_key_rate": key_rate.secure_key_rate,
                "final_key_bits": key_rate.final_key_bits,
            })

            self.collector.set_sifting_results(
                sifted_bits=sifted.sifted_count,
                qber=qber_est.qber,
                confidence=qber_est.confidence_interval,
            )
            self.collector.set_key_rate(
                raw_rate=key_rate.raw_key_rate,
                secure_rate=key_rate.secure_key_rate,
                final_bits=key_rate.final_key_bits,
            )

        finally:
            channel.close()
            server.close()
