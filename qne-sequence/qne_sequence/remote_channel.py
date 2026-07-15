"""Remote channels — the transmit-side seam (DESIGN.md §2 seam 2, §5).

These are shaped like SeQUeNCe's ClassicalChannel / QuantumChannel (they expose a
``.delay`` attribute and a ``transmit`` entry point) and are installed into
``node.cchannels[peer]`` / ``node.qchannels[peer]``. But instead of scheduling a
local Event on the receiver's in-memory heap, they serialize the message/photon and
push the bytes onto a real socket. The peer's Listener turns the bytes back into a
local event (see listener.py).

Phase A note: the quantum channel is a *stub* — it ships photon descriptors
losslessly over the same TCP link as a "QUBITS" frame (descriptor-on-wire, §6.1),
with no P4 loss model yet. Phase C replaces transmit_batch with the raw-socket /
0x7101 / BMv2 fast path.
"""

from __future__ import annotations

import numpy

from .wire_codec import WireCodec


class RemoteClassicalChannel:
    """In-memory stand-in for SeQUeNCe ClassicalChannel that sends over the wire.

    Matches the signature Node.send_message expects:
        cchannels[dst].transmit(msg, source, priority, sender_delay)
    """

    def __init__(self, link, delay: int = 0, timeline=None):
        self.link = link
        self.delay = delay  # modeled fiber delay (ps); read by BB84.start_protocol
        # with a timeline, frames carry t_send so the receiver can deliver at
        # exactly t_send + delay (lookahead mode; see listener.Listener)
        self.timeline = timeline

    def transmit(self, msg, source, priority, sender_delay: int = 0) -> None:
        frame = WireCodec.encode(
            kind="classical",
            src=source.name,
            receiver=msg.receiver,
            msg_type=msg.msg_type,
            payload=getattr(msg, "payload", {}) or {},
            t_send=self.timeline.now() if self.timeline is not None else None,
        )
        self.link.send(frame)


class RemoteQuantumChannel:
    """Descriptor-on-wire quantum channel (DESIGN.md §6.1).

    Real photons cannot traverse a socket; for prepare-and-measure (BB84) we send
    the classical descriptor ``[seq, basis, bit]`` per pulse and let the receiver
    measure. Phase B adds **fiber loss** as a per-photon probabilistic drop —
    ``P(loss) = 1 - 10^(-alpha*L/10)`` — the same model the P4 switch applies, so a
    Phase C swap to the raw-socket / 0x7101 / BMv2 fast path is a drop-in.

    Args:
        link: the Link to send on.
        delay: modeled propagation delay (ps), read by BB84.
        loss_probability: per-photon drop probability (0.0 = lossless).
        seed: RNG seed for the loss draws.
    """

    def __init__(self, link, delay: int = 0, loss_probability: float = 0.0,
                 seed: int = 0, timeline=None):
        self.link = link
        self.delay = delay
        self.loss_probability = loss_probability
        self._rng = numpy.random.default_rng(seed)
        self.timeline = timeline  # for t_send stamping (lookahead delivery)

    def transmit_batch(self, src_name: str, receiver_proto: str, pulses: list) -> None:
        # pulses: list of [seq, basis, bit]; drop each independently (fiber loss)
        if self.loss_probability > 0.0:
            survivors = [p for p in pulses
                         if self._rng.random() >= self.loss_probability]
        else:
            survivors = pulses
        self._send_pulses(src_name, receiver_proto, survivors)

    def transmit_one(self, src_name: str, receiver_proto: str,
                     seq: int, basis: int, bit: int) -> None:
        """Transmit a single photon (PerPhotonEvent mode). Dropped photons send
        nothing — fiber loss removes them from the wire entirely."""
        if self.loss_probability > 0.0 and self._rng.random() < self.loss_probability:
            return
        self._send_pulses(src_name, receiver_proto, [[seq, basis, bit]])

    def _send_pulses(self, src_name: str, receiver_proto: str, pulses: list) -> None:
        frame = WireCodec.encode(
            kind="quantum",
            src=src_name,
            receiver=receiver_proto,
            msg_type="QUBITS",
            payload={"pulses": pulses},
            t_send=self.timeline.now() if self.timeline is not None else None,
        )
        self.link.send(frame)
