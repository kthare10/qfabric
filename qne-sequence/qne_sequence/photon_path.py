"""PhotonEmissionStrategy — pluggable photon throughput modes (DESIGN.md §4.3).

The per-photon discrete-event model may not survive real-time at MHz rates in Python,
so we make the photon path a strategy with two interchangeable implementations and
*measure* rather than assume (see `tests/test_phase_c_throughput.py` and
`bench_throughput.py`):

  * `PerPhotonEvent` — each photon is one timeline Event and one wire frame, fully
    through RealTimeTimeline. Maximum fidelity (per-photon timing/dynamics), lower
    throughput ceiling.
  * `BulkStream` — the whole pulse train is shipped in one frame, bypassing the
    per-event loop. Targets the ~1 MHz rate; loses per-photon timing granularity.

Both end with a `QUBITS_DONE` classical marker so the receiver knows the train is
complete (it cannot infer this from a count — fiber loss drops photons in flight).

This module is transport-agnostic: it drives `protocol.owner.qchannels[peer]`, whose
implementation today is `RemoteQuantumChannel` (descriptor-on-wire over TCP). Phase C2
swaps that channel for the raw-socket / 0x7101 / BMv2 fast path with no change here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sequence.kernel.event import Event
from sequence.kernel.process import Process


class PhotonEmissionStrategy(ABC):
    name: str

    @abstractmethod
    def emit(self, protocol, basis_list, bit_list) -> None:
        """Alice side: transmit the pulse train, then signal QUBITS_DONE."""


class BulkStream(PhotonEmissionStrategy):
    name = "bulk"

    def emit(self, protocol, basis_list, bit_list) -> None:
        n = len(basis_list)
        pulses = [[i, int(basis_list[i]), int(bit_list[i])] for i in range(n)]
        protocol.owner.qchannels[protocol.peer_node].transmit_batch(
            protocol.owner.name, protocol.peer_proto, pulses)
        protocol.send_quantum_done()


class PerPhotonEvent(PhotonEmissionStrategy):
    name = "per_event"

    def emit(self, protocol, basis_list, bit_list) -> None:
        n = len(basis_list)
        tl = protocol.owner.timeline
        now = tl.now()
        # one Event per photon (all due now -> drained back-to-back; this is the
        # per-photon event/send cost we want to measure). DONE fires strictly after.
        for i in range(n):
            tl.schedule(Event(now, Process(
                protocol, "emit_one_photon", [i, int(basis_list[i]), int(bit_list[i])])))
        tl.schedule(Event(now + 1, Process(protocol, "send_quantum_done", [])))


_STRATEGIES = {s.name: s for s in (BulkStream, PerPhotonEvent)}


def make_strategy(name: str) -> PhotonEmissionStrategy:
    try:
        return _STRATEGIES[name]()
    except KeyError:
        raise ValueError(f"unknown photon mode {name!r}; choose from {list(_STRATEGIES)}")
