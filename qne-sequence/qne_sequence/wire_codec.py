"""WireCodec — serialize SeQUeNCe protocol traffic to/from bytes (DESIGN.md §7).

Rather than pickling SeQUeNCe Message objects (brittle across envs, unsafe over a
network), we use an explicit JSON envelope. Phase A carries both the classical
control messages and the (stubbed) quantum descriptor batch over one framed TCP
link, distinguished by ``kind``:

    {
      "kind":     "classical" | "quantum",
      "src":      "<source node name>",
      "receiver": "<destination protocol name>",   # == msg.receiver
      "msg_type": "BEGIN_PHOTON_PULSE" | ... | "QUBITS",
      "payload":  { ... },                          # type-specific fields
      "t_send":   <sender sim-time in ps>           # optional; lookahead delivery
    }

``t_send`` is the sender's simulation clock at transmit. With a shared epoch
(timesync.py) the receiver can deliver at exactly ``t_send + modeled_delay`` —
the same event time a pure simulator would use — instead of paying real wire
latency *plus* the modeled delay (see listener.Listener).

On decode of a classical frame we rebuild a WireMessage, which Node.receive_message
routes to the matching protocol exactly as in-process (routing is by msg.receiver).
"""

from __future__ import annotations

import json

from sequence.message import Message


class WireMessage(Message):
    """Concrete SeQUeNCe Message carrying a string msg_type and a dict payload.

    Message defines ``__slots__ = ['msg_type', 'receiver', 'protocol_type', 'payload']``
    so we reuse those four fields exactly — msg_type is a string, payload a dict.
    """

    def __init__(self, msg_type: str, receiver: str, payload: dict | None = None):
        super().__init__(msg_type, receiver)
        self.protocol_type = "DistributedBB84"
        self.payload = payload or {}


class WireCodec:
    """Encode/decode the JSON envelope above into length-agnostic bytes.

    Framing (length prefix) is the transport's job (see listener.Link); this codec
    only handles the payload bytes.
    """

    @staticmethod
    def encode(kind: str, src: str, receiver: str, msg_type: str, payload: dict,
               t_send: int | None = None) -> bytes:
        envelope = {
            "kind": kind,
            "src": src,
            "receiver": receiver,
            "msg_type": msg_type,
            "payload": payload,
        }
        if t_send is not None:
            envelope["t_send"] = int(t_send)
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def decode(data: bytes) -> dict:
        return json.loads(data.decode("utf-8"))

    @staticmethod
    def to_message(frame: dict) -> WireMessage:
        """Rebuild a routable SeQUeNCe message from a decoded classical frame."""
        return WireMessage(frame["msg_type"], frame["receiver"], frame["payload"])
