/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Komal Thareja
 *
 * Author: Komal Thareja (kthare10@renci.org)
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/* quantum_channel.p4 — BMv2 V1Model quantum channel emulator
 *
 * Implements fiber attenuation as probabilistic packet drop:
 *   P(loss) = 1 - 10^(-alpha * L / 10)
 *
 * The loss probability is pre-computed as a 32-bit threshold and
 * installed in a match-action table keyed by wavelength. At runtime,
 * each photon packet generates a random 32-bit number; if the random
 * value is less than the threshold, the photon is dropped.
 *
 * Non-photon traffic (e.g., classical BB84 sifting) is forwarded
 * via a standard L2 forwarding table.
 */

#include <core.p4>
#include <v1model.p4>
#include "includes/headers.p4"
#include "includes/parser.p4"

/* ================================================================
 * Ingress Processing
 * ================================================================ */

control PhotonIngress(
    inout headers_t hdr,
    inout metadata_t meta,
    inout standard_metadata_t standard_metadata
) {
    /* Counter for photon statistics */
    counter(256, CounterType.packets) photon_tx_counter;
    counter(256, CounterType.packets) photon_drop_counter;

    /* ---- Quantum channel loss table ----
     * Match on wavelength → set loss threshold and egress port.
     * Threshold = floor(P(loss) * 2^32)
     */
    action set_channel_params(bit<32> threshold, bit<9> port, bit<48> src_mac, bit<48> dst_mac) {
        meta.loss_threshold = threshold;
        meta.egress_port = port;
        meta.egress_src_mac = src_mac;
        meta.egress_dst_mac = dst_mac;
    }

    action drop_photon() {
        mark_to_drop(standard_metadata);
    }

    table quantum_channel_params {
        key = {
            hdr.photon.wavelength: exact;
        }
        actions = {
            set_channel_params;
            drop_photon;
        }
        default_action = drop_photon();
        size = 256;
    }

    /* ---- Port-based forwarding for non-photon traffic ----
     * Forwards based on ingress port (bidirectional pipe) and rewrites
     * src MAC to the switch port's own MAC. This avoids FABRIC's OVS
     * dropping frames with unknown destination MACs (MAC learning issue).
     */
    action port_forward(bit<9> egress_port, bit<48> src_mac, bit<48> dst_mac) {
        standard_metadata.egress_spec = egress_port;
        hdr.ethernet.src_addr = src_mac;
        hdr.ethernet.dst_addr = dst_mac;
    }

    action port_drop() {
        mark_to_drop(standard_metadata);
    }

    table port_forwarding {
        key = {
            standard_metadata.ingress_port: exact;
        }
        actions = {
            port_forward;
            port_drop;
        }
        default_action = port_drop();
        size = 16;
    }

    /* ---- Emulated classical channel (EtherType 0x7102) ----
     * The classical control channel rides raw L2 through the same switch as the
     * photons — "the switch is the fiber, carrying both wavelengths." Loss is NOT
     * modeled here (the classical channel is engineered lossless); the switch only
     * classifies, forwards (bidirectional pipe by ingress port), rewrites the MACs
     * to the egress port's own (FABRIC/OVS MAC-learning workaround, as with the
     * photon/port_forwarding paths), and counts. Propagation delay is applied by
     * netem on the switch's egress ports, since BMv2 cannot hold a packet.
     */
    counter(256, CounterType.packets) classical_fwd_counter;

    action classical_forward(bit<9> egress_port, bit<48> src_mac, bit<48> dst_mac) {
        standard_metadata.egress_spec = egress_port;
        hdr.ethernet.src_addr = src_mac;
        hdr.ethernet.dst_addr = dst_mac;
    }

    action classical_drop() {
        mark_to_drop(standard_metadata);
    }

    table classical_channel_params {
        key = {
            standard_metadata.ingress_port: exact;
        }
        actions = {
            classical_forward;
            classical_drop;
        }
        default_action = classical_drop();
        size = 16;
    }

    apply {
        if (hdr.photon.isValid()) {
            /* Photon packet: apply quantum channel loss model.
             * Gate the loss/forward logic on a table HIT: on a miss the
             * default_action drop_photon() has already marked the packet to
             * drop, and the loss branch below must NOT run — otherwise a
             * zero-initialized meta.loss_threshold sends the else-branch,
             * overwriting egress_spec to port 0 and forwarding an
             * unknown-wavelength photon instead of dropping it. */
            if (quantum_channel_params.apply().hit) {
                /* Generate random number for drop decision */
                random(meta.random_value, (bit<32>)0, (bit<32>)0xFFFFFFFF);

                /* Count all photon packets */
                photon_tx_counter.count((bit<32>)hdr.photon.wavelength);

                if (meta.random_value < meta.loss_threshold) {
                    /* Photon lost in fiber */
                    photon_drop_counter.count((bit<32>)hdr.photon.wavelength);
                    mark_to_drop(standard_metadata);
                } else {
                    /* Photon survives — forward to detector (Bob) */
                    standard_metadata.egress_spec = meta.egress_port;
                    hdr.ethernet.src_addr = meta.egress_src_mac;
                    hdr.ethernet.dst_addr = meta.egress_dst_mac;
                }
            }
            /* else: table miss — default_action drop_photon() already
             * marked-to-drop; leave the packet dropped. */
        } else if (meta.is_classical == 1) {
            /* Emulated classical channel (0x7102): forward + count, no loss */
            if (classical_channel_params.apply().hit) {
                classical_fwd_counter.count((bit<32>)standard_metadata.ingress_port);
            }
        } else {
            /* Other non-photon traffic (ARP, control): forward by ingress port */
            port_forwarding.apply();
        }
    }
}

/* ================================================================
 * Egress Processing (pass-through)
 * ================================================================ */

control PhotonEgress(
    inout headers_t hdr,
    inout metadata_t meta,
    inout standard_metadata_t standard_metadata
) {
    apply { /* No egress processing needed */ }
}

/* ================================================================
 * Pipeline instantiation
 * ================================================================ */

V1Switch(
    PhotonParser(),
    PhotonVerifyChecksum(),
    PhotonIngress(),
    PhotonEgress(),
    PhotonComputeChecksum(),
    PhotonDeparser()
) main;
