"""node_runner — run one SeQUeNCe QKDNode as a process and drive BB84 (§8).

Each invocation builds the *local* slice of the topology (a single QKDNode), swaps
its BB84 protocol for a DistributedBB84 with a GuardedRemoteStub peer, wires a
RemoteClassicalChannel / RemoteQuantumChannel over a real TCP link to the peer host,
and runs a wall-clock timeline until a key completes.

Usage (run two of these — Bob first so it is listening):
    python -m qne_sequence.node_runner --role bob   --name bob   --peer alice \
        --host 127.0.0.1 --port 57123 --key-length 128
    python -m qne_sequence.node_runner --role alice --name alice --peer bob  \
        --host 127.0.0.1 --port 57123 --key-length 128

On completion each process prints one JSON line:
    {"role": 0, "name": "alice", "key": <int>, "key_bits": 128,
     "tx_frames": N, "rx_frames": M, "remote_access_errors": 0}
"""

from __future__ import annotations

import argparse
import json
import sys
from time import time_ns

from sequence.topology.node import QKDNode
from sequence.kernel.event import Event
from sequence.kernel.process import Process

from qne.detector import Detector

from .rt_timeline import RealTimeTimeline
from .listener import Link, Listener
from .remote_channel import RemoteClassicalChannel, RemoteQuantumChannel
from .raw_photon import RawQuantumChannel, RawPhotonReceiver
from .distributed_qkd import DistributedBB84, pair_distributed

_ROLES = {"alice": 0, "bob": 1}

# Wall-clock margin before Alice issues the key request, so Bob's RX thread and
# timeline are up (DESIGN.md §4 start barrier). Expressed in ps.
_START_BARRIER_PS = 300_000_000_000  # 0.3 s at time_scale=1.0


def loss_probability(distance_km: float, attenuation_db_per_km: float) -> float:
    """Fiber loss as a per-photon drop probability: P = 1 - 10^(-alpha*L/10)."""
    return 1.0 - 10 ** (-(attenuation_db_per_km * distance_km) / 10.0)


def run_node(role_name: str, name: str, peer: str, host: str, port: int,
             key_length: int, key_num: int, seed: int, time_scale: float,
             channel_delay: int, distance_km: float, attenuation: float,
             fidelity: float, efficiency: float, dark_count_rate: float,
             sample_fraction: float, num_pulses: int | None,
             photon_mode: str = "bulk", quantum_transport: str = "tcp",
             photon_iface: str | None = None, src_mac: str = "02:00:00:00:00:01",
             dst_mac: str = "02:00:00:00:00:02", wavelength: int = 0,
             photon_drain_ms: float = 200.0, loss: str = "auto") -> dict:
    role = _ROLES[role_name]

    # Photon loss policy (independent of transport):
    #   none   -> lossless channel (ignore distance/attenuation entirely)
    #   model  -> software drop = P(distance, attenuation), applied in the channel
    #   switch -> no software drop; an external BMv2 P4 switch applies it (raw only)
    #   auto   -> model for tcp, switch for raw (conventional default; unchanged)
    loss_where = ("model" if quantum_transport == "tcp" else "switch") if loss == "auto" else loss

    tl = RealTimeTimeline(time_scale=time_scale)
    node = QKDNode(name, tl, stack_size=1, seed=seed)

    # captured result(s)
    result: dict = {}

    def on_key(_role: int, info: dict) -> None:
        result.update(info)
        tl.stop_loop()

    # Bob measures with qfabric's validated detector model; Alice needs none.
    detector = None
    if role == 1:
        detector = Detector(efficiency=efficiency, dark_count_rate=dark_count_rate,
                            polarization_error=1.0 - fidelity, seed=seed + 1)

    # raw mode: photons (P4 path) race the TCP QUBITS_DONE marker -> drain window
    drain_ps = int(photon_drain_ms * 1e9) if quantum_transport == "raw" else 0

    # swap in the distributed BB84 protocol
    dbb = DistributedBB84(node, f"{name}.BB84", f"{name}.lightsource",
                          f"{name}.qsdetector", role=role, seed=seed, on_key=on_key,
                          detector=detector, sample_fraction=sample_fraction,
                          num_pulses=num_pulses, photon_mode=photon_mode,
                          photon_drain_ps=drain_ps)
    node.set_protocol_layer(0, dbb)
    pair_distributed(dbb, role, f"{peer}.BB84", peer)

    # classical control plane: TCP (Bob listens, Alice connects) — real WAN on FABRIC
    link = Link()
    if role == 1:
        link.serve(host, port)
    else:
        link.connect(host, port)
    node.cchannels[peer] = RemoteClassicalChannel(link, delay=channel_delay)

    # Software drop applied by the channel: only when loss_where == 'model'.
    # 'none' -> 0 (lossless); 'switch' -> 0 here (the P4 switch drops downstream).
    sw_loss = loss_probability(distance_km, attenuation) if loss_where == "model" else 0.0
    raw_rx = None
    if quantum_transport == "raw":
        # quantum plane: real 0x7101 frames node-to-node (+ optional P4 switch)
        iface = photon_iface or ("veth1" if role == 0 else "veth3")
        if role == 0:  # Alice transmits photons
            node.qchannels[peer] = RawQuantumChannel(
                iface, src_mac=src_mac, dst_mac=dst_mac, wavelength=wavelength,
                loss_probability=sw_loss, seed=seed + 2)
        else:          # Bob receives photons on a raw RX thread
            node.qchannels[peer] = RawQuantumChannel(iface)  # unused TX placeholder
            raw_rx = RawPhotonReceiver(iface, tl, dbb, peer, delay=channel_delay)
            raw_rx.start()
        # classical frames only on the TCP link
        listener = Listener(tl, node, dbb, delay=channel_delay)
    else:
        # descriptor-on-wire over the shared TCP link (no switch); software loss
        node.qchannels[peer] = RemoteQuantumChannel(link, delay=channel_delay,
                                                    loss_probability=sw_loss, seed=seed + 2)
        listener = Listener(tl, node, dbb, delay=channel_delay)

    link.on_frame = listener.on_frame
    link.start_rx()

    tl.init()

    # shared epoch so both sides agree on wall<->sim mapping
    tl.set_epoch(time_ns())

    if role == 0:  # Alice kicks off after the start barrier
        tl.schedule(Event(tl.now() + _START_BARRIER_PS,
                          Process(dbb, "push", [key_length, key_num])))

    # safety stop so a hung run can't block forever
    tl.stop_time = _START_BARRIER_PS + int(60e12)
    tl.run()
    link.close()
    if raw_rx is not None:
        raw_rx.stop()

    return {
        "role": role,
        "name": name,
        "quantum_transport": quantum_transport,
        "loss_where": loss_where,
        "key": result.get("key"),
        "qber": result.get("qber"),
        "sifted_bits": result.get("sifted_bits"),
        "num_sampled": result.get("num_sampled"),
        "secure_fraction": result.get("secure_fraction"),
        "final_key_bits": result.get("final_key_bits"),
        "photon_mode": result.get("photon_mode", photon_mode),
        "photons_emitted": result.get("photons_emitted"),
        "elapsed_s": result.get("elapsed_s"),
        "photons_per_s": result.get("photons_per_s"),
        "loss_probability": 0.0 if loss_where == "none" else loss_probability(distance_km, attenuation),
        "tx_frames": link.tx_count,
        "rx_frames": link.rx_count,
        "remote_access_errors": len(tl.remote_access_errors),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one distributed SeQUeNCe QKD node.")
    ap.add_argument("--role", required=True, choices=list(_ROLES))
    ap.add_argument("--name", required=True)
    ap.add_argument("--peer", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=57123)
    ap.add_argument("--key-length", type=int, default=128)
    ap.add_argument("--key-num", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--time-scale", type=float, default=1.0)
    ap.add_argument("--channel-delay", type=int, default=0,
                    help="modeled extra delay in ps on top of real wire latency")
    # physics (defaults are ideal: lossless, perfect detector — preserves Phase A)
    ap.add_argument("--distance-km", type=float, default=0.0)
    ap.add_argument("--attenuation", type=float, default=0.0,
                    help="fiber attenuation in dB/km")
    ap.add_argument("--fidelity", type=float, default=1.0,
                    help="polarization fidelity F; detector polarization_error = 1-F")
    ap.add_argument("--efficiency", type=float, default=1.0)
    ap.add_argument("--dark-count-rate", type=float, default=0.0)
    ap.add_argument("--sample-fraction", type=float, default=0.1)
    ap.add_argument("--num-pulses", type=int, default=0,
                    help="override emitted pulse count (0 = derive from key length)")
    ap.add_argument("--photon-mode", choices=["bulk", "per_event"], default="bulk",
                    help="photon throughput strategy (DESIGN §4.3)")
    # quantum transport: tcp descriptor (dev) or raw 0x7101 through P4 (FABRIC)
    ap.add_argument("--quantum-transport", choices=["tcp", "raw"], default="tcp",
                    help="tcp=descriptor-on-wire (no switch); raw=0x7101 L2 frames")
    ap.add_argument("--loss", choices=["auto", "model", "switch", "none"], default="auto",
                    help="photon loss: none=lossless (ignore distance/atten); "
                         "model=software P(dist,atten); switch=external BMv2 P4; "
                         "auto=software for tcp, switch for raw")
    ap.add_argument("--photon-iface", default=None,
                    help="raw-socket interface (default veth1 for alice, veth3 for bob)")
    ap.add_argument("--src-mac", default="02:00:00:00:00:01")
    ap.add_argument("--dst-mac", default="02:00:00:00:00:02")
    ap.add_argument("--wavelength", type=int, default=0, help="P4 loss-table key / WDM tag")
    ap.add_argument("--photon-drain-ms", type=float, default=200.0,
                    help="raw mode: wait for straggler photons after QUBITS_DONE")
    args = ap.parse_args(argv)

    result = run_node(args.role, args.name, args.peer, args.host, args.port,
                      args.key_length, args.key_num, args.seed, args.time_scale,
                      args.channel_delay, args.distance_km, args.attenuation,
                      args.fidelity, args.efficiency, args.dark_count_rate,
                      args.sample_fraction, args.num_pulses or None, args.photon_mode,
                      args.quantum_transport, args.photon_iface, args.src_mac,
                      args.dst_mac, args.wavelength, args.photon_drain_ms, args.loss)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
