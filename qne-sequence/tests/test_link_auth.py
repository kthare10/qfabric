"""Authenticated Link — frames verified end-to-end; tampering tears the link down."""

from __future__ import annotations

import socket
import struct
import threading
import time

from qne_sequence.listener import Link


def _linked_pair(server_key, client_key, port):
    server = Link(auth_key=server_key)
    client = Link(auth_key=client_key)
    t = threading.Thread(target=server.serve, args=("127.0.0.1", port))
    t.start()
    client.connect("127.0.0.1", port)
    t.join(timeout=5)
    return server, client


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_authenticated_link_delivers_frames_in_order():
    got = []
    server, client = _linked_pair("psk", "psk", 57431)
    server.on_frame = got.append
    server.start_rx()
    try:
        for i in range(10):
            client.send(f"frame-{i}".encode())
        assert _wait_for(lambda: len(got) == 10)
        assert got == [f"frame-{i}".encode() for i in range(10)]
        assert server.auth_failures == 0
    finally:
        client.close()
        server.close()


def test_key_mismatch_closes_link_and_delivers_nothing():
    got = []
    server, client = _linked_pair("right-key", "wrong-key", 57433)
    server.on_frame = got.append
    server.start_rx()
    try:
        client.send(b"attack traffic")
        assert _wait_for(lambda: server.auth_failures == 1)
        assert got == []
    finally:
        client.close()
        server.close()


def test_spliced_raw_frame_is_rejected():
    # inject a well-formed but unauthenticated frame directly on the socket
    got = []
    server = Link(auth_key="psk")
    t = threading.Thread(target=server.serve, args=("127.0.0.1", 57435))
    t.start()
    raw = socket.create_connection(("127.0.0.1", 57435), timeout=5)
    t.join(timeout=5)
    server.on_frame = got.append
    server.start_rx()
    try:
        payload = b'{"kind":"classical"}'
        raw.sendall(struct.pack("!I", len(payload)) + payload)
        assert _wait_for(lambda: server.auth_failures == 1)
        assert got == []
    finally:
        raw.close()
        server.close()
