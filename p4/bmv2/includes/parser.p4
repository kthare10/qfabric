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

/* parser.p4 — Parser and Deparser for QFabric photon packets */

#ifndef PARSER_P4
#define PARSER_P4

#include "headers.p4"

parser PhotonParser(
    packet_in packet,
    out headers_t hdr,
    inout metadata_t meta,
    inout standard_metadata_t standard_metadata
) {
    state start {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.ether_type) {
            ETHERTYPE_PHOTON: parse_photon;
            default: accept;
        }
    }

    state parse_photon {
        packet.extract(hdr.photon);
        meta.is_photon = 1;
        transition accept;
    }
}

control PhotonDeparser(
    packet_out packet,
    in headers_t hdr
) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.photon);
    }
}

control PhotonVerifyChecksum(
    inout headers_t hdr,
    inout metadata_t meta
) {
    apply { /* No checksum for photon packets */ }
}

control PhotonComputeChecksum(
    inout headers_t hdr,
    inout metadata_t meta
) {
    apply { /* No checksum for photon packets */ }
}

#endif /* PARSER_P4 */
