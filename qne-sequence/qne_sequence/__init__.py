"""qne_sequence — distributed runtime for the SeQUeNCe quantum-network simulator.

Phase A: run real SeQUeNCe QKDNode/BB84 instances in separate processes that
exchange BB84 control messages as *real traffic on the wire* (loopback TCP),
with a wall-clock-driven timeline and a guarded `another` seam that proves every
cross-process state access has been converted to a message.

See ../DESIGN.md (§4, §5, §7, §8.1, §11 Phase A).
"""

from .guarded_stub import GuardedRemoteStub, RemoteAccessError
from .rt_timeline import RealTimeTimeline
from .wire_codec import WireCodec, WireMessage
from .remote_channel import RemoteClassicalChannel, RemoteQuantumChannel
from .raw_photon import RawQuantumChannel, RawPhotonReceiver
from .listener import Link, Listener
from .photon_path import PhotonEmissionStrategy, BulkStream, PerPhotonEvent, make_strategy
from .distributed_qkd import DistributedBB84, pair_distributed

__all__ = [
    "GuardedRemoteStub",
    "RemoteAccessError",
    "RealTimeTimeline",
    "WireCodec",
    "WireMessage",
    "RemoteClassicalChannel",
    "RemoteQuantumChannel",
    "RawQuantumChannel",
    "RawPhotonReceiver",
    "Link",
    "Listener",
    "PhotonEmissionStrategy",
    "BulkStream",
    "PerPhotonEvent",
    "make_strategy",
    "DistributedBB84",
    "pair_distributed",
]
