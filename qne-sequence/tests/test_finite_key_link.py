"""Finite-key sizing through the reconcile-link driver (in-memory RPC pair)."""

from __future__ import annotations

import queue
import threading

import numpy as np

from qne.bb84 import BB84Protocol
from qne.finite_key import finite_key_length
from qne_sequence.reconcile_link import drive_cascade, serve_parities


class _Pipe:
    """Minimal in-memory RpcChannel double (send/call/recv_any)."""

    def __init__(self):
        self.inbox: "queue.Queue[tuple]" = queue.Queue()
        self.peer: "_Pipe | None" = None

    def send(self, kind, body):
        self.peer.inbox.put((kind, body))

    def recv_any(self, timeout=30.0):
        return self.inbox.get(timeout=timeout)

    def call(self, kind, body, expected, timeout=30.0):
        self.send(kind, body)
        k, b = self.recv_any(timeout)
        assert k == expected
        return b


def _pipes():
    a, b = _Pipe(), _Pipe()
    a.peer, b.peer = b, a
    return a, b


def test_drive_cascade_uses_finite_length():
    rng = np.random.default_rng(5)
    n, q = 6_000, 0.03
    alice = rng.integers(0, 2, n).tolist()
    bob = [b ^ (1 if rng.random() < q else 0) for b in alice]

    a_rpc, b_rpc = _pipes()
    out = {}
    t = threading.Thread(target=lambda: out.update(a=serve_parities(a_rpc, alice)))
    t.start()
    finite = {"n_sample": 600, "eps_sec": 1e-9, "eps_cor": 1e-15}
    b_final, _corrections, leaked = drive_cascade(b_rpc, bob, q, seed=9, finite=finite)
    t.join(timeout=30)
    a_final = out["a"][0]

    assert a_final == b_final                     # identical extracted secret
    expected = finite_key_length(n, 600, q, leaked,
                                 eps_sec=1e-9, eps_cor=1e-15).secret_bits
    assert len(b_final) == expected               # PA sized by the finite bound
    # finite length is strictly below the asymptotic accounting for the same run
    assert expected < n * (1 - 2 * BB84Protocol.binary_entropy(q)) + 1


def test_finite_none_preserves_asymptotic_behaviour():
    rng = np.random.default_rng(6)
    n, q = 4_000, 0.02
    alice = rng.integers(0, 2, n).tolist()
    bob = [b ^ (1 if rng.random() < q else 0) for b in alice]

    a_rpc, b_rpc = _pipes()
    out = {}
    t = threading.Thread(target=lambda: out.update(a=serve_parities(a_rpc, alice)))
    t.start()
    b_final, _corrections, leaked = drive_cascade(b_rpc, bob, q, seed=11)
    t.join(timeout=30)

    h = BB84Protocol.binary_entropy(q)
    assert len(b_final) == max(0, int(n * (1.0 - h)) - leaked)
    assert out["a"][0] == b_final
