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

from __future__ import annotations

import queue
import struct
import threading

import pytest

from qne.auth import AuthError
from qne_sequence.l2_link import LoopbackDatagram, ReliableLink

_HEADER = struct.Struct("!2sBBQHH")
_DATA = 1


def connected_links(
    *,
    server_on_frame=None,
    client_on_frame=None,
    server_key=None,
    client_key=None,
    fragment_size=256,
):
    server_backend, client_backend = LoopbackDatagram.pair()
    server = ReliableLink(
        server_on_frame,
        server_key,
        backend=server_backend,
        fragment_size=fragment_size,
        ack_timeout=0.005,
        max_retries=40,
    )
    client = ReliableLink(
        client_on_frame,
        client_key,
        backend=client_backend,
        fragment_size=fragment_size,
        ack_timeout=0.005,
        max_retries=40,
    )
    errors = []

    def serve():
        try:
            server.serve("unused", 0, timeout=1.0)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    client.connect("unused", 0, retries=100, delay=0.005)
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert not errors
    return server, client, server_backend, client_backend


def close_links(*links):
    for link in links:
        link.close()


def test_small_round_trip_and_counters():
    received = queue.Queue()
    server, client, _, _ = connected_links(server_on_frame=received.put)
    server.start_rx()
    client.send(b"hello")

    assert received.get(timeout=1.0) == b"hello"
    assert client.tx_count == 1
    assert server.rx_count == 1
    close_links(server, client)


def test_large_payload_fragmented_and_reassembled():
    server, client, _, _ = connected_links(fragment_size=700)
    payload = bytes(range(256)) * 1200

    client.send(payload)

    assert server.recv_one() == payload
    close_links(server, client)


def test_messages_delivered_in_order_when_datagrams_reordered():
    server, client, _, client_backend = connected_links(fragment_size=64)
    held = []

    def reorder(data):
        packet_type = _HEADER.unpack_from(data)[2]
        if packet_type != _DATA:
            return [data]
        held.append(data)
        if len(held) == 2:
            first, second = held
            held.clear()
            return [second, first]
        return []

    client_backend.transform = reorder
    payload = b"a" * 100
    client.send(payload)

    assert server.recv_one() == payload
    close_links(server, client)


def test_resend_recovers_dropped_fragment():
    server, client, _, client_backend = connected_links(fragment_size=64)
    dropped = False

    def drop_once(data):
        nonlocal dropped
        _, _, packet_type, _, frag_index, _ = _HEADER.unpack_from(data)
        if packet_type == _DATA and frag_index == 1 and not dropped:
            dropped = True
            return []
        return [data]

    client_backend.transform = drop_once
    payload = b"resend" * 40
    client.send(payload)

    assert dropped
    assert server.recv_one() == payload
    close_links(server, client)


def test_duplicate_fragment_does_not_duplicate_message():
    server, client, _, client_backend = connected_links(fragment_size=64)

    def duplicate(data):
        packet_type = _HEADER.unpack_from(data)[2]
        return [data, data] if packet_type == _DATA else [data]

    client_backend.transform = duplicate
    client.send(b"duplicate me")

    assert server.recv_one() == b"duplicate me"
    assert server.rx_count == 1
    close_links(server, client)


def test_recv_one_before_start_rx():
    server, client, _, _ = connected_links()
    client.send(b"timesync")

    assert server.recv_one() == b"timesync"
    close_links(server, client)


def test_authenticated_round_trip():
    server, client, _, _ = connected_links(server_key="psk", client_key="psk")
    client.send(b"authenticated")

    assert server.recv_one() == b"authenticated"
    assert server.auth_failures == 0
    close_links(server, client)


def test_tampered_fragment_raises_auth_error_and_counts_failure():
    server, client, _, client_backend = connected_links(
        server_key="psk",
        client_key="psk",
        fragment_size=512,
    )
    tampered = False

    def tamper(data):
        nonlocal tampered
        packet_type = _HEADER.unpack_from(data)[2]
        if packet_type == _DATA and not tampered:
            tampered = True
            changed = bytearray(data)
            changed[-1] ^= 0x01
            return [bytes(changed)]
        return [data]

    client_backend.transform = tamper
    client.send(b"do not alter")

    with pytest.raises(AuthError):
        server.recv_one()
    assert tampered
    assert server.auth_failures == 1
    close_links(server, client)
