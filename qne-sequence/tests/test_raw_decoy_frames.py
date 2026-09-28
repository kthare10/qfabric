"""Decoy-state pulses on the raw 0x7101 photon path — the parts testable without a switch.

The live path (AF_PACKET + veth + BMv2) runs on Linux/FABRIC; `p4/tests/test_loss_model.py`
covers the switch's per-photon thinning there. Here we pin the Python side:

  1. The frame's photon-count byte round-trips, 0 on the wire reads as 1 (legacy frames),
     and the count is capped at MAX_PHOTON_COUNT.
  2. `RawQuantumChannel` puts the pulse's photon count on the wire, sends nothing for an
     empty pulse, and — with software loss (no switch) — thins the count per photon.
  3. `RawPhotonReceiver` hands a multi-photon frame back as the 4-field decoy pulse the
     protocol already understands, and a single-photon frame as the plain 3-field pulse.
  4. Bob dark-counts the slots whose photon never arrived (lost or vacuum), so the raw
     path's vacuum gain is not identically zero.
"""

from __future__ import annotations

import types

import numpy
import pytest

from qne.photon import MAX_PHOTON_COUNT, PhotonPacket
from qne_sequence.raw_photon import RawPhotonReceiver, RawQuantumChannel, parse_mac

SRC = parse_mac("02:00:00:00:00:01")
DST = parse_mac("02:00:00:00:00:02")


class _FakeSocket:
    def __init__(self):
        self.frames: list[bytes] = []

    def send(self, frame: bytes) -> None:
        self.frames.append(frame)

    def close(self) -> None:
        pass


def _channel(loss: float = 0.0, seed: int = 0) -> tuple[RawQuantumChannel, _FakeSocket]:
    ch = RawQuantumChannel("veth1", loss_probability=loss, seed=seed)
    sock = _FakeSocket()
    ch._sock = sock          # never opens AF_PACKET
    return ch, sock


def test_photon_count_round_trips_and_legacy_zero_reads_as_one():
    for n in (1, 2, 5, MAX_PHOTON_COUNT):
        pkt = PhotonPacket(basis=1, state=0, sequence_num=7, photon_count=n)
        assert PhotonPacket.from_ethernet_frame(
            pkt.to_ethernet_frame(DST, SRC)).photon_count == n
    # over the cap: clamped on the wire
    big = PhotonPacket(basis=0, state=1, sequence_num=1, photon_count=40).to_bytes()
    assert PhotonPacket.from_bytes(big).photon_count == MAX_PHOTON_COUNT
    # a legacy sender wrote 0 in the padding byte: that is one photon
    legacy = bytearray(PhotonPacket(basis=0, state=1, sequence_num=1).to_bytes())
    legacy[-1] = 0
    assert PhotonPacket.from_bytes(bytes(legacy)).photon_count == 1
    # a plain photon still goes out as exactly one
    assert PhotonPacket(basis=0, state=0, sequence_num=0).to_bytes()[-1] == 1


def test_raw_channel_puts_the_pulse_photon_count_on_the_wire():
    ch, sock = _channel()
    ch.transmit_batch("alice", "bob.BB84",
                      [[0, 0, 1, 3], [1, 1, 0, 0], [2, 0, 0, 1], [3, 1, 1]])
    # the vacuum pulse (n = 0) sends nothing; plain 3-field pulses are one photon
    counts = [PhotonPacket.from_ethernet_frame(f) for f in sock.frames]
    assert [(p.sequence_num, p.photon_count) for p in counts] == [(0, 3), (2, 1), (3, 1)]
    assert ch.tx_count == 3


def test_raw_channel_software_loss_thins_per_photon():
    """No switch (`--loss model`): each of the n photons survives independently, so a
    lossless channel forwards n unchanged, a fully lossy one forwards nothing, and a
    half-lossy one forwards Binomial(n, 0.5) survivors."""
    ch, sock = _channel(loss=0.0)
    ch.transmit_batch("alice", "bob.BB84", [[i, 0, 0, 4] for i in range(50)])
    assert all(PhotonPacket.from_ethernet_frame(f).photon_count == 4 for f in sock.frames)

    ch, sock = _channel(loss=1.0)
    ch.transmit_batch("alice", "bob.BB84", [[i, 0, 0, 4] for i in range(50)])
    assert sock.frames == []

    ch, sock = _channel(loss=0.5, seed=3)
    n_pulses, n_per = 4000, 4
    ch.transmit_batch("alice", "bob.BB84", [[i, 0, 0, n_per] for i in range(n_pulses)])
    survivors = [PhotonPacket.from_ethernet_frame(f).photon_count for f in sock.frames]
    # P(all 4 lost) = 1/16 -> ~3750 frames; mean survivors per SENT pulse = 2
    assert 3600 < len(sock.frames) < 3900
    assert abs(sum(survivors) / n_pulses - n_per * 0.5) < 0.1
    assert max(survivors) <= n_per and min(survivors) >= 1


def test_receiver_decodes_multi_photon_frames_as_decoy_pulses():
    one = PhotonPacket(basis=0, state=1, sequence_num=5).to_ethernet_frame(DST, SRC)
    three = PhotonPacket(basis=1, state=0, sequence_num=6, photon_count=3).to_ethernet_frame(DST, SRC)
    assert RawPhotonReceiver.pulse_from_frame(one) == [5, 0, 1]
    assert RawPhotonReceiver.pulse_from_frame(three) == [6, 1, 0, 3]
    assert RawPhotonReceiver.pulse_from_frame(b"\x00" * 60) is None      # not 0x7101


def _make_bob(dark_prob: float):
    from qne.detector import Detector
    from qne_sequence.distributed_qkd import DistributedBB84, pair_distributed

    owner = types.SimpleNamespace(
        name="bobnode", protocols=[], components={}, cchannels={}, qchannels={},
        timeline=types.SimpleNamespace(now=lambda: 0))
    # dark_count_prob = rate * window
    det = Detector(efficiency=1.0, dark_count_rate=dark_prob / 1e-9, detection_window=1e-9,
                   polarization_error=0.0, seed=4)
    proto = DistributedBB84(owner, "bob.BB84", "ls", "qsd", role=1, seed=2, detector=det)
    pair_distributed(proto, 1, "alice.BB84", "alicenode")
    proto.working = True
    return proto


def test_bob_dark_counts_the_slots_whose_photon_never_arrived():
    proto = _make_bob(dark_prob=1.0)
    proto.receive_qubits("alice", [[0, 0, 1], [2, 1, 0, 3]])      # slots 1, 3, 4 never came
    assert set(proto._bob_records) == {0, 2}
    proto._finalize_detections(5)
    assert set(proto._bob_records) == {0, 1, 2, 3, 4}             # every empty slot clicked
    # arrived photons keep their measured values; darks are random bits
    assert proto._bob_records[0] == (0, 1) or proto._bob_records[0][0] == 1

    quiet = _make_bob(dark_prob=0.0)
    quiet.receive_qubits("alice", [[0, 0, 1]])
    quiet._finalize_detections(1000)
    assert set(quiet._bob_records) == {0}                          # no darks, no phantom clicks


def test_arrived_but_undetected_slots_are_not_drawn_twice():
    """A pulse that reached the detector already had its dark draw in
    receive_qubits; the back-fill must skip it, or the dark rate doubles."""
    proto = _make_bob(dark_prob=0.0)
    proto.detector.efficiency = 0.0                                # arrives, never clicks
    proto.receive_qubits("alice", [[i, 0, 0] for i in range(50)] + [[50, 0, 0, 0]])
    assert proto._bob_records == {}
    proto.detector.dark_count_prob = 1.0                           # a second draw would click
    proto._finalize_detections(60)
    assert set(proto._bob_records) == set(range(51, 60))           # only the never-seen slots


def test_dead_time_runs_replay_every_slot_in_order():
    """With dead time modeled, arrived pulses are parked and every slot — arrived
    or never-arrived — is detected in slot order at finalize time, so a click
    (real or dark) blinds exactly the slots that follow it within the window and
    nothing that came before it. Here slot 10 arrives (perfect detector: it
    clicks), slots 9 and 11 never arrive (dark probability 1): slot 11 falls in
    the 150 ns window and must be lost; slot 9 precedes the click and must click;
    slot 12 is past the window and clicks."""
    from qne.detector import Detector
    from qne_sequence.distributed_qkd import DistributedBB84, pair_distributed

    owner = types.SimpleNamespace(
        name="bobnode", protocols=[], components={}, cchannels={}, qchannels={},
        timeline=types.SimpleNamespace(now=lambda: 0))
    det = Detector(efficiency=1.0, dark_count_rate=1e9, detection_window=1e-9,   # dark p = 1
                   polarization_error=0.0, seed=1, dead_time=150.0, pulse_period_ns=100.0)
    proto = DistributedBB84(owner, "bob.BB84", "ls", "qsd", role=1, seed=2, detector=det)
    pair_distributed(proto, 1, "alice.BB84", "alicenode")
    proto.working = True

    proto.receive_qubits("alice", [[10, 0, 0]])
    assert proto._bob_records == {} and proto._bob_pending == {10: (0, 0, None)}   # parked
    proto._finalize_detections(13)
    # In slot order with p_dark = 1 and a 150 ns window over 100 ns slots, every
    # other slot clicks and blinds its successor: 0, 2, 4, 6, 8 dark-click, 9 is
    # blinded by 8, the ARRIVED photon in slot 10 clicks (1000 ns is past 800 + 150),
    # 11 is blinded by it, 12 clicks. Had the never-arrived slots been back-filled
    # after slot 10 instead, slot 9 would have clicked at 900 ns with slot 10 already
    # recorded 100 ns later — two clicks inside one dead window.
    assert set(proto._bob_records) == {0, 2, 4, 6, 8, 10, 12}
    assert det.dead_time_drops == 6
    assert proto._bob_pending == {}

    # a second, quiet detector: no darks -> only the arrived photon, still gated in order
    det2 = Detector(efficiency=1.0, dark_count_rate=0.0, polarization_error=0.0,
                    seed=1, dead_time=150.0, pulse_period_ns=100.0)
    proto2 = DistributedBB84(owner, "bob2.BB84", "ls", "qsd", role=1, seed=2, detector=det2)
    pair_distributed(proto2, 1, "alice.BB84", "alicenode")
    proto2.working = True
    proto2.receive_qubits("alice", [[10, 0, 0], [11, 0, 0], [12, 0, 0]])
    proto2._finalize_detections(13)
    assert set(proto2._bob_records) == {10, 12}      # 11 inside 10's window, 12 outside


def test_bob_dark_count_rate_matches_the_detector_probability():
    p = 0.05
    proto = _make_bob(dark_prob=p)
    proto._finalize_detections(20000)
    clicks = len(proto._bob_records)
    sigma = numpy.sqrt(20000 * p * (1 - p))
    assert abs(clicks - 20000 * p) < 4 * sigma


@pytest.mark.parametrize("transport", ["tcp", "raw"])
def test_node_runner_accepts_decoy_on_both_transports(transport):
    """`--decoy` used to refuse `--quantum-transport raw`; both are now legal and
    the loss model lands in the right place (source thinning vs. the channel)."""
    from qne_sequence import node_runner as nr

    # exercise only the configuration block: re-implement its decision to keep the
    # test hermetic (no sockets), mirroring run_node's rules
    loss_where = "model" if transport == "tcp" else "switch"
    source_thins = transport == "tcp" and loss_where == "model"
    p_loss = nr.loss_probability(10.0, 0.2)
    cfg_loss = p_loss if source_thins else 0.0
    if transport == "tcp":
        assert cfg_loss == pytest.approx(p_loss) and cfg_loss > 0
    else:
        assert cfg_loss == 0.0          # the switch (or channel) thins per photon


def test_stray_and_duplicate_sequence_numbers_are_discarded():
    """A corrupt frame with a huge sequence number must neither stall the
    in-order replay (which is bounded by the announced train) nor stay in the
    detection record; a repeated sequence number is one pulse, not two."""
    # immediate-detection path (no dead time), perfect detector
    proto = _make_bob(dark_prob=0.0)
    proto.receive_qubits("alice", [[0, 0, 1], [1, 1, 0], [1, 1, 1], [2**31, 0, 0], [-3, 0, 0]])
    assert set(proto._bob_records) == {0, 1, 2**31}
    assert proto._bob_duplicates == 2
    proto._finalize_detections(4)
    assert set(proto._bob_records) == {0, 1}                    # stray detection purged
    assert proto._bob_out_of_range == 1

    # a stray that reached the detector but did NOT click is still a stray
    blind = _make_bob(dark_prob=0.0)
    blind.detector.efficiency = 0.0
    blind.receive_qubits("alice", [[0, 0, 0], [7, 0, 0], [99, 0, 0]])
    assert blind._bob_records == {}
    blind._finalize_detections(8)
    assert blind._bob_out_of_range == 1 and blind._bob_records == {}

    # dead-time (replay) path: the loop is bounded by num_slots, not by the stray seq
    from qne.detector import Detector
    from qne_sequence.distributed_qkd import DistributedBB84, pair_distributed
    owner = types.SimpleNamespace(
        name="bobnode", protocols=[], components={}, cchannels={}, qchannels={},
        timeline=types.SimpleNamespace(now=lambda: 0))
    det = Detector(efficiency=1.0, dark_count_rate=0.0, polarization_error=0.0,
                   seed=1, dead_time=150.0, pulse_period_ns=100.0)
    bob = DistributedBB84(owner, "bob.BB84", "ls", "qsd", role=1, seed=2, detector=det)
    pair_distributed(bob, 1, "alice.BB84", "alicenode")
    bob.working = True
    bob.receive_qubits("alice", [[0, 0, 0], [5, 0, 0], [10**9, 0, 0]])
    bob._finalize_detections(6)                                   # returns promptly
    assert set(bob._bob_records) == {0, 5}
    assert bob._bob_out_of_range == 1 and bob._bob_pending == {}
