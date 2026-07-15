"""Lookahead delivery — emulation executes the simulator's event schedule.

The fidelity contract (listener.Listener + timesync.py):

1. With a shared epoch and ``channel_delay > 0``, every frame is delivered at
   exactly ``t_send + delay`` in sim time — the event time a pure simulator
   would use — as long as real wire latency stays below the modeled delay
   (conservative-DES lookahead). ``late_events == 0`` is the per-run proof.
2. The protocol outcome is therefore *delay-invariant*: the same seeds produce
   the same key whether the modeled delay is 0 or 20 ms, because the modeled
   delay shifts the schedule without reordering it — exactly like a simulator.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from time import time_ns

from sequence.kernel.event import Event

from qne_sequence.listener import Link, Listener
from qne_sequence.timesync import request_epoch, serve_epoch
from qne_sequence.wire_codec import WireCodec

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../qne-sequence
KEY_LENGTH = 128


# ---------------------------------------------------------------------------
# unit: Listener scheduling semantics
# ---------------------------------------------------------------------------

class _FakeTimeline:
    def __init__(self, now_ps: int):
        self._now = now_ps
        self.injected: list[Event] = []

    def now(self) -> int:
        return self._now

    def inject(self, event: Event) -> None:
        self.injected.append(event)


class _Sink:
    def receive_message(self, src, msg):
        pass

    def receive_qubits(self, src, pulses):
        pass


def _classical_frame(t_send: int | None) -> bytes:
    return WireCodec.encode(kind="classical", src="alice", receiver="bob.BB84",
                            msg_type="TEST", payload={}, t_send=t_send)


def test_listener_on_time_frame_fires_at_t_send_plus_delay():
    tl = _FakeTimeline(now_ps=1_000_000)
    lst = Listener(tl, _Sink(), _Sink(), delay=500_000)
    lst.on_frame(_classical_frame(t_send=900_000))   # deadline 1_400_000 > now

    assert [e.time for e in tl.injected] == [1_400_000]
    assert lst.on_time_events == 1
    assert lst.late_events == 0


def test_listener_late_frame_fires_now_and_is_counted():
    tl = _FakeTimeline(now_ps=2_000_000)
    lst = Listener(tl, _Sink(), _Sink(), delay=500_000)
    lst.on_frame(_classical_frame(t_send=1_000_000))  # deadline 1_500_000 < now

    assert [e.time for e in tl.injected] == [2_000_000]
    assert lst.late_events == 1
    assert lst.max_lateness_ps == 500_000
    assert lst.on_time_events == 0


def test_listener_legacy_paths_unchanged():
    # no t_send -> arrival + delay; delay == 0 -> arrival, no lookahead metrics
    tl = _FakeTimeline(now_ps=1_000_000)
    lst = Listener(tl, _Sink(), _Sink(), delay=500_000)
    lst.on_frame(_classical_frame(t_send=None))
    assert tl.injected[-1].time == 1_500_000

    lst0 = Listener(tl, _Sink(), _Sink(), delay=0)
    lst0.on_frame(_classical_frame(t_send=900_000))
    assert tl.injected[-1].time == 1_000_000
    assert lst0.on_time_events == 0 and lst0.late_events == 0


# ---------------------------------------------------------------------------
# unit: epoch handshake
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_epoch_handshake_aligns_clocks_on_loopback():
    port = _free_port()
    server = Link()
    client = Link()
    epoch = time_ns()
    server_err: list[Exception] = []
    server_sync: list[tuple[int, int]] = []

    def _serve():
        try:
            server.serve("127.0.0.1", port)
            server_sync.append(serve_epoch(server, epoch))
        except Exception as exc:  # surfaced by the main thread's asserts
            server_err.append(exc)

    t = threading.Thread(target=_serve)
    t.start()
    try:
        client.connect("127.0.0.1", port)
        local_epoch, offset, rtt = request_epoch(client)
    finally:
        t.join(timeout=10)
        client.close()
        server.close()

    assert not server_err, server_err
    assert rtt > 0
    # same physical clock on loopback: recovered epoch within 5 ms of the real
    # one (error bound is ~RTT/2, typically tens of microseconds)
    assert abs(local_epoch - epoch) < 5_000_000, (local_epoch, epoch, offset, rtt)
    # both ends learned the (mirrored) peer offset
    s_off, s_rtt = server_sync[0]
    assert abs(s_off + offset) < 1000 and s_rtt == rtt


def test_rpc_channel_paced_delivery():
    from qne_sequence.remote_qm import RpcChannel
    from qne_sequence.timesync import sync_link

    port = _free_port()
    server = Link()
    client = Link()
    delay_ps = 50_000_000_000                       # 50 ms
    server_side: dict = {}

    def _serve():
        server.serve("127.0.0.1", port)
        off, _rtt = sync_link(server, serving=True)
        server_side["rpc"] = RpcChannel(server, delay_ps=delay_ps,
                                        peer_offset_ns=off)
        server.start_rx()

    t = threading.Thread(target=_serve)
    t.start()
    try:
        client.connect("127.0.0.1", port)
        off, _rtt = sync_link(client, serving=False)
        rpc_c = RpcChannel(client, delay_ps=delay_ps, peer_offset_ns=off)
        client.start_rx()
        t.join(timeout=10)

        t0 = time_ns()
        server_side["rpc"].send("PING", {"n": 1})
        body = rpc_c.recv("PING")
        elapsed_ms = (time_ns() - t0) / 1e6
        assert body == {"n": 1}
        # delivered no earlier than the modeled 50 ms (real loopback ~0)
        assert 45 <= elapsed_ms <= 500, elapsed_ms
        assert rpc_c.on_time_events == 1 and rpc_c.late_events == 0

        # delay 0 -> legacy immediate delivery, no pacing metadata
        rpc_s0 = RpcChannel(server)                 # rebinds on_frame
        rpc_c0 = RpcChannel(client)
        t0 = time_ns()
        rpc_s0.send("PING", {"n": 2})
        assert rpc_c0.recv("PING") == {"n": 2}
        assert (time_ns() - t0) / 1e6 < 45
        assert rpc_c0.on_time_events == 0 and rpc_c0.late_events == 0
    finally:
        client.close()
        server.close()


# ---------------------------------------------------------------------------
# integration: two-process BB84 with modeled delay >> real loopback latency
# ---------------------------------------------------------------------------

_DELAY_20MS_PS = 20_000_000_000


def _spawn(role: str, port: int, seed: int, delay_ps: int,
           extra: tuple = ()) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = (PKG_DIR + os.pathsep + os.path.dirname(PKG_DIR)
                         + os.pathsep + env.get("PYTHONPATH", ""))
    name, peer = (("alice", "bob") if role == "alice" else ("bob", "alice"))
    return subprocess.Popen(
        [sys.executable, "-m", "qne_sequence.node_runner",
         "--role", role, "--name", name, "--peer", peer,
         "--host", "127.0.0.1", "--port", str(port),
         "--key-length", str(KEY_LENGTH), "--seed", str(seed),
         "--channel-delay", str(delay_ps), *extra],
        cwd=PKG_DIR, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _run_pair(port: int, delay_ps: int, extra: tuple = ()) -> tuple[dict, dict]:
    bob = _spawn("bob", port, seed=2, delay_ps=delay_ps, extra=extra)
    alice = _spawn("alice", port, seed=1, delay_ps=delay_ps, extra=extra)
    try:
        results = []
        for proc in (alice, bob):
            out, err = proc.communicate(timeout=120)
            line = next((ln for ln in out.strip().splitlines()
                         if ln.startswith("{")), "")
            assert line, f"no JSON result.\nstdout:\n{out}\nstderr:\n{err}"
            results.append(json.loads(line))
        return results[0], results[1]
    finally:
        for p in (alice, bob):
            if p.poll() is None:
                p.kill()


def test_two_node_lookahead_zero_late_and_delay_invariant_key():
    # 20 ms modeled delay dwarfs loopback latency -> every frame beats its
    # deadline: the run provably executed the simulator's event schedule.
    ra, rb = _run_pair(_free_port(), _DELAY_20MS_PS)
    for r in (ra, rb):
        assert r["lookahead"]["late_events"] == 0, r["lookahead"]
        assert r["lookahead"]["on_time_events"] > 0, r["lookahead"]
    assert ra["key"] is not None and ra["key"] == rb["key"]
    assert ra["timesync"]["role"] == "client" and ra["timesync"]["rtt_ns"] > 0
    assert rb["timesync"]["role"] == "master"

    # delay-invariance: same seeds, no modeled delay -> identical key. The
    # modeled delay changed WHEN events fired, not WHAT the protocol computed —
    # i.e. the emulation behaves like the simulation.
    ra0, rb0 = _run_pair(_free_port(), 0)
    assert ra0["key"] == rb0["key"] == ra["key"], (ra0["key"], rb0["key"], ra["key"])


def test_e91_lookahead_zero_late_and_delay_invariant_key():
    # entanglement path: every classical RPC message (basis plan, measurement
    # batches, Cascade parities) is paced to t_send + 20 ms — zero deadline
    # misses, and the extracted key is identical to the zero-delay run.
    extra = ("--protocol", "bbm92", "--num-pairs", "1500", "--fidelity", "0.97")
    ra, rb = _run_pair(_free_port(), _DELAY_20MS_PS, extra=extra)
    for r in (ra, rb):
        assert r["lookahead"]["late_events"] == 0, r["lookahead"]
        assert r["lookahead"]["on_time_events"] > 0, r["lookahead"]
    assert ra["key"] == rb["key"] is not None

    ra0, rb0 = _run_pair(_free_port(), 0, extra=extra)
    assert ra0["key"] == rb0["key"] == ra["key"]


def _run_chain(port: int, delay_ps: int) -> dict[str, dict]:
    env = dict(os.environ)
    env["PYTHONPATH"] = (PKG_DIR + os.pathsep + os.path.dirname(PKG_DIR)
                         + os.pathsep + env.get("PYTHONPATH", ""))
    seeds = {"alice": 1, "bob": 2, "repeater": 3}
    procs = {}
    for role in ("bob", "repeater", "alice"):
        procs[role] = subprocess.Popen(
            [sys.executable, "-m", "qne_sequence.node_runner",
             "--role", role, "--name", role, "--protocol", "repeater",
             "--host", "127.0.0.1", "--port", str(port),
             "--num-pairs", "1200", "--fidelity", "0.97",
             "--sample-fraction", "0.2", "--seed", str(seeds[role]),
             "--channel-delay", str(delay_ps)],
            cwd=PKG_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    results = {}
    try:
        for who, p in procs.items():
            out, err = p.communicate(timeout=120)
            line = next((ln for ln in out.strip().splitlines()
                         if ln.startswith("{")), "")
            assert line, f"no JSON from {who}.\nstdout:\n{out}\nstderr:\n{err}"
            results[who] = json.loads(line)
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
    return results


def test_channel_delay_auto_derives_from_distance():
    # unit: the unified distance knob — one L drives loss AND delay
    from qne_sequence.node_runner import PS_PER_KM, propagation_delay_ps
    assert propagation_delay_ps(10) == 10 * PS_PER_KM == 49_000_000
    assert propagation_delay_ps(0) == 0

    # integration: '--channel-delay auto' at 500 km -> 2.45e9 ps (2.45 ms)
    # modeled delay. The deadline must exceed the software stack's real frame
    # latency for a clean certificate — a 2 km deadline (9.8 us) is honestly
    # unmeetable in Python on loopback and would (correctly) count as late.
    extra = ("--distance-km", "500", "--loss", "none")
    ra, rb = _run_pair(_free_port(), "auto", extra=extra)
    for r in (ra, rb):
        assert r["channel_delay_ps"] == 500 * 4_900_000
        assert r["lookahead"]["late_events"] == 0, r["lookahead"]
    assert ra["key"] == rb["key"] is not None


def test_repeater_lookahead_zero_late_and_delay_invariant_key():
    # the swapped chain: BSM RPCs, HERALDS, and the QKD tail are all paced —
    # the herald latency is now a modeled quantity, not an accident of the wire.
    r = _run_chain(_free_port(), _DELAY_20MS_PS)
    for who in ("alice", "bob", "repeater"):
        assert r[who]["lookahead"]["late_events"] == 0, (who, r[who]["lookahead"])
        assert r[who]["lookahead"]["on_time_events"] > 0, (who, r[who]["lookahead"])
        assert r[who]["channel_delay_ps"] == _DELAY_20MS_PS
    assert r["alice"]["key"] == r["bob"]["key"] is not None

    r0 = _run_chain(_free_port(), 0)
    assert r0["alice"]["key"] == r0["bob"]["key"] == r["alice"]["key"]
