"""Phase C2 (raw 0x7101 photon transport) — the parts testable without a raw socket.

The live raw/P4 path runs on FABRIC/Linux (AF_PACKET + veth + BMv2). Here we verify:
  1. The SeQUeNCe pulse (seq, basis, bit) -> qfabric PhotonPacket 0x7101 frame -> parse
     round-trip is lossless and matches the existing wire format (so the same P4
     program/table apply).
  2. On a host without AF_PACKET (e.g. macOS), opening the raw transport raises a clear,
     actionable error instead of something cryptic.
  3. MAC parsing accepts both string and bytes forms.
"""

from __future__ import annotations

import socket

import pytest

from qne.photon import PhotonPacket, ETHERTYPE_PHOTON
from qne_sequence.raw_photon import RawQuantumChannel, RawPhotonReceiver, parse_mac

_HAS_AF_PACKET = hasattr(socket, "AF_PACKET")


def test_pulse_to_0x7101_frame_roundtrip():
    """A BB84 pulse survives the exact wire format qfabric's P4 switch parses."""
    src = parse_mac("02:00:00:00:00:01")
    dst = parse_mac("02:00:00:00:00:02")
    for seq, basis, bit in [(0, 0, 0), (1, 0, 1), (42, 1, 0), (65535, 1, 1)]:
        pkt = PhotonPacket(basis=basis, state=bit, sequence_num=seq, wavelength=0)
        frame = pkt.to_ethernet_frame(dst_mac=dst, src_mac=src)
        # EtherType is the photon type the P4 parser keys on
        assert frame[12:14] == ETHERTYPE_PHOTON.to_bytes(2, "big")
        assert len(frame) >= 60                       # Ethernet minimum
        back = PhotonPacket.from_ethernet_frame(frame)
        assert (back.sequence_num, back.basis, back.state) == (seq, basis, bit)


def test_parse_mac_accepts_str_and_bytes():
    assert parse_mac("02:00:00:00:00:02") == b"\x02\x00\x00\x00\x00\x02"
    assert parse_mac(b"\x02\x00\x00\x00\x00\x02") == b"\x02\x00\x00\x00\x00\x02"
    with pytest.raises(ValueError):
        parse_mac(b"\x02\x00")                          # wrong length


def test_raw_software_loss_drops_without_a_socket():
    """`loss=model` lets raw 0x7101 run with NO P4 switch: a fully-lossy channel
    drops every photon in software (no socket opened, no AF_PACKET needed)."""
    ch = RawQuantumChannel("veth1", loss_probability=1.0, seed=0)
    ch.transmit_batch("alice", "bob.BB84", [[i, 0, 1] for i in range(100)])
    ch.transmit_one("alice", "bob.BB84", 100, 1, 0)
    assert ch.tx_count == 0           # all dropped -> nothing sent, no raw socket touched
    # a partial-loss channel drops ~half of a large batch (software loss model)
    ch2 = RawQuantumChannel("veth1", loss_probability=0.5, seed=1)
    survived = sum(not ch2._dropped() for _ in range(2000))
    assert 850 < survived < 1150      # ~50% survive


@pytest.mark.skipif(_HAS_AF_PACKET, reason="AF_PACKET present (Linux); runtime path used")
def test_raw_transport_errors_clearly_without_af_packet():
    """On macOS/dev the raw path must fail with an actionable message, not a crash."""
    ch = RawQuantumChannel("veth1")
    with pytest.raises(RuntimeError, match="AF_PACKET"):
        ch.transmit_one("alice", "bob.BB84", 0, 0, 0)

    rx = RawPhotonReceiver("veth3", timeline=None, protocol=None, peer_name="alice")
    with pytest.raises(RuntimeError, match="AF_PACKET"):
        rx.start()


def _make_bob(detector_seed: int = 1):
    """Minimal Bob protocol — no sockets, no timeline thread."""
    import types

    from qne.detector import Detector
    from qne_sequence.distributed_qkd import DistributedBB84, pair_distributed

    owner = types.SimpleNamespace(
        name="bobnode", protocols=[], components={}, cchannels={}, qchannels={},
        timeline=types.SimpleNamespace(now=lambda: 0),
    )
    det = Detector(efficiency=1.0, dark_count_rate=0.0,
                   polarization_error=0.0, seed=detector_seed)
    proto = DistributedBB84(owner, "bob.BB84", "ls", "qsd", role=1, seed=2,
                            detector=det)
    pair_distributed(proto, 1, "alice.BB84", "alicenode")
    return proto


def test_photons_arriving_before_begin_are_buffered_not_wiped():
    """Raw-mode head race: photons through the P4 path can beat the TCP
    BEGIN_PHOTON_PULSE. They must survive the BEGIN reset, not be silently
    lost (which shows up on FABRIC as phantom extra fiber loss)."""
    import types

    proto = _make_bob()

    # head of the train arrives before BEGIN is processed
    proto.receive_qubits("alice", [[0, 0, 1], [1, 1, 0]])
    assert proto._bob_records == {}            # nothing recorded yet...
    assert len(proto._pre_begin_pulses) == 2   # ...but nothing lost either

    begin = types.SimpleNamespace(msg_type="BEGIN_PHOTON_PULSE", payload={
        "frequency": 8e7, "light_time": 1e-3, "start_time": 0,
        "wavelength": 0, "end_run_time": 10**18, "key_length": 128})
    proto.received_message("alicenode", begin)

    # with a perfect detector both early photons are recorded after replay
    assert set(proto._bob_records) == {0, 1}
    assert proto._pre_begin_pulses == []

    # the rest of the train accumulates normally on top
    proto.receive_qubits("alice", [[2, 0, 0]])
    assert set(proto._bob_records) == {0, 1, 2}
