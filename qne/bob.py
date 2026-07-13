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
from qne.reconcile import ChannelRpc, bits_to_int, drive_cascade


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
    ):
        self.config = config
        self.interface = interface
        self.classical_host = classical_host
        self.classical_port = classical_port
        self.auth_key = auth_key
        self.reconcile = reconcile
        self.final_key: int | None = None    # extracted secret (post Cascade + PA)
        self.detector = Detector(
            efficiency=config.detector.efficiency,
            dark_count_rate=config.detector.dark_count_rate,
            polarization_error=1.0 - config.channel.polarization_fidelity,
            seed=config.seed + 100,
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
        """Accept Alice's connection; sift, estimate QBER, reconcile, amplify.

        Only a random SAMPLE of the sifted positions is disclosed for QBER
        estimation (the rest stays secret — key material), then Bob drives
        Cascade against Alice as a parity oracle and both extract the identical
        Toeplitz-amplified secret. All math is shared: BB84Protocol for the
        sifting/QBER accounting, qne.reconcile for Cascade + PA.
        """
        server = ClassicalServer(self.classical_host, self.classical_port,
                                 auth_key=self.auth_key)
        server.start()
        print(f"Bob: Waiting for Alice on {self.classical_host}:{self.classical_port}")

        channel = server.accept()
        print("Bob: Alice connected")

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
            bias = self.config.protocol.basis_bias
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
                final, corrections, bits_leaked = drive_cascade(
                    ChannelRpc(channel), key_bits, qber_est.qber,
                    self.config.seed + 303)
                reconciled = True
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
