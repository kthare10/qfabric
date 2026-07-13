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

"""Authenticated classical channel — MAC verification, replay, and TCP roundtrip."""

from __future__ import annotations

import socket
import threading

import pytest

from qne.auth import HEADER_LEN, AuthError, FrameAuthenticator
from qne.channel import ClassicalChannel, ClassicalClient, ClassicalServer


def _pair():
    return FrameAuthenticator("shared-secret"), FrameAuthenticator("shared-secret")


def test_seal_open_roundtrip_in_order():
    tx, rx = _pair()
    for i in range(5):
        payload = f"msg-{i}".encode()
        assert rx.open(tx.seal(payload)) == payload


def test_tampered_payload_rejected():
    tx, rx = _pair()
    frame = bytearray(tx.seal(b"basis list"))
    frame[-1] ^= 0x01
    with pytest.raises(AuthError, match="bad MAC"):
        rx.open(bytes(frame))


def test_wrong_key_rejected():
    tx = FrameAuthenticator("alice-key")
    rx = FrameAuthenticator("mallory-key")
    with pytest.raises(AuthError, match="bad MAC"):
        rx.open(tx.seal(b"hello"))


def test_replayed_frame_rejected():
    tx, rx = _pair()
    frame = tx.seal(b"qber result")
    assert rx.open(frame) == b"qber result"
    with pytest.raises(AuthError, match="sequence break"):
        rx.open(frame)


def test_reordered_frames_rejected():
    tx, rx = _pair()
    f0, f1 = tx.seal(b"first"), tx.seal(b"second")
    with pytest.raises(AuthError, match="sequence break"):
        rx.open(f1)
    del f0


def test_truncated_frame_rejected():
    tx, rx = _pair()
    with pytest.raises(AuthError, match="too short"):
        rx.open(tx.seal(b"x")[: HEADER_LEN - 3])


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        FrameAuthenticator("")


def _tcp_channels(auth_server, auth_client):
    """A connected ClassicalServer/Client pair over localhost."""
    server = ClassicalServer("127.0.0.1", 0, auth_key=auth_server)
    server.start()
    port = server._server_sock.getsockname()[1]
    got = {}
    t = threading.Thread(target=lambda: got.update(ch=server.accept()))
    t.start()
    client = ClassicalClient.connect("127.0.0.1", port, timeout=5.0,
                                     max_retries=3, retry_delay=0.1,
                                     auth_key=auth_client)
    t.join(timeout=5)
    return server, got["ch"], client


def test_classical_channel_authenticated_roundtrip():
    server, bob_ch, alice_ch = _tcp_channels("k1", "k1")
    try:
        alice_ch.send_message({"type": "alice_bases", "bases": {0: 1, 1: 0}})
        msg = bob_ch.recv_message()
        assert msg["type"] == "alice_bases"
        bob_ch.send_message({"type": "sifting_result", "n": 2})
        assert alice_ch.recv_message()["n"] == 2
    finally:
        alice_ch.close()
        bob_ch.close()
        server.close()


def test_classical_channel_key_mismatch_raises():
    server, bob_ch, alice_ch = _tcp_channels("k1", "k2")
    try:
        alice_ch.send_message({"type": "alice_bases"})
        with pytest.raises(AuthError):
            bob_ch.recv_message()
    finally:
        alice_ch.close()
        bob_ch.close()
        server.close()


def test_injected_plaintext_rejected():
    # an attacker splicing an unauthenticated frame into the stream is caught
    server = ClassicalServer("127.0.0.1", 0, auth_key="k1")
    server.start()
    port = server._server_sock.getsockname()[1]
    got = {}
    t = threading.Thread(target=lambda: got.update(ch=server.accept()))
    t.start()
    raw = socket.create_connection(("127.0.0.1", port), timeout=5)
    t.join(timeout=5)
    try:
        attacker = ClassicalChannel(raw)          # no auth: sends bare JSON
        attacker.send_message({"type": "alice_bases", "bases": {}})
        with pytest.raises(AuthError):
            got["ch"].recv_message()
    finally:
        raw.close()
        server.close()
