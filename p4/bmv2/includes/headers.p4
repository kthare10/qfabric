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

/* headers.p4 — Ethernet + Photon header definitions for QFabric */

#ifndef HEADERS_P4
#define HEADERS_P4

const bit<16> ETHERTYPE_PHOTON = 0x7101;
const bit<16> ETHERTYPE_IPV4   = 0x0800;

header ethernet_t {
    bit<48> dst_addr;
    bit<48> src_addr;
    bit<16> ether_type;
}

/* Photon header: 17 bytes (136 bits) */
header photon_t {
    bit<8>  version;        /* Protocol version (0x01) */
    bit<8>  basis;          /* 0 = Z (rectilinear), 1 = X (diagonal) */
    bit<8>  state;          /* 0 = |0>/|+>, 1 = |1>/|-> */
    bit<8>  wavelength;     /* Channel tag for WDM */
    bit<32> sequence_num;   /* Monotonic photon ID */
    bit<32> timestamp_hi;   /* TX timestamp upper (picoseconds) */
    bit<32> timestamp_lo;   /* TX timestamp lower (picoseconds) */
    bit<8>  padding;        /* Reserved */
}

struct headers_t {
    ethernet_t ethernet;
    photon_t   photon;
}

struct metadata_t {
    bit<32> loss_threshold;  /* Per-wavelength loss threshold */
    bit<32> random_value;    /* Random number for drop decision */
    bit<9>  egress_port;     /* Forwarding port */
    bit<48> egress_src_mac;  /* Source MAC to rewrite on egress */
    bit<48> egress_dst_mac;  /* Dest MAC to rewrite on egress */
    bit<1>  is_photon;       /* 1 if photon packet */
}

#endif /* HEADERS_P4 */
