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


# Classical propagation in fiber: c / n (n = 1.468) ~ 204,000 km/s ~ 4.9 us/km.
PS_PER_KM = 4_900_000


def propagation_delay_ps(distance_km: float) -> int:
    """One-way classical propagation delay over the SAME fiber distance (ps).

    The unified distance knob: `--channel-delay auto` derives the classical
    delay from the same L that drives quantum loss, so one distance yields a
    coherent channel model (loss AND latency) instead of two free knobs.
    """
    return int(distance_km * PS_PER_KM)


def run_node(role_name: str, name: str, peer: str, host: str, port: int,
             key_length: int, key_num: int, seed: int, time_scale: float,
             channel_delay: int, distance_km: float, attenuation: float,
             fidelity: float, efficiency: float, dark_count_rate: float,
             sample_fraction: float, num_pulses: int | None,
             photon_mode: str = "bulk", quantum_transport: str = "tcp",
             photon_iface: str | None = None, src_mac: str = "02:00:00:00:00:01",
             dst_mac: str = "02:00:00:00:00:02", wavelength: int = 0,
             photon_drain_ms: float = 200.0, loss: str = "auto",
             photon_rate_hz: float = 10000.0, eve_fraction: float = 0.0,
             do_reconcile: bool = True, cascade_passes: int = 4,
             finite_key: bool = False, eps_sec: float = 1e-9,
             eps_cor: float = 1e-15, auth_key: str | None = None,
             basis_bias: float = 0.5, dead_time: float = 0.0,
             timing_jitter: float = 0.0, pulse_period_ns: float = 0.0,
             decoy: bool = False, mu_signal: float = 0.6, mu_decoy: float = 0.1,
             mu_vacuum: float = 0.001, decoy_probs: str = "0.7,0.2,0.1",
             classical_transport: str = "tcp",
             classical_iface: str | None = None,
             epoch_seed_ns: int = 0) -> dict:
    role = _ROLES[role_name]

    # Photon loss policy (independent of transport):
    #   none   -> lossless channel (ignore distance/attenuation entirely)
    #   model  -> software drop = P(distance, attenuation), applied in the channel
    #   switch -> no software drop; an external BMv2 P4 switch applies it (raw only)
    #   auto   -> model for tcp, switch for raw (conventional default; unchanged)
    loss_where = ("model" if quantum_transport == "tcp" else "switch") if loss == "auto" else loss

    # Decoy-state source: fiber loss is folded into the per-photon binomial
    # thinning at the source (the descriptor carries the SURVIVING photon count),
    # so the channel itself must not drop descriptors — a lost descriptor would be
    # double-counted loss AND wreck the vacuum gain (empty pulses still dark-count).
    decoy_cfg = None
    if decoy:
        if quantum_transport != "tcp":
            raise ValueError("--decoy requires --quantum-transport tcp "
                             "(the 0x7101 frame has no photon-count field yet)")
        probs = [float(x) for x in decoy_probs.split(",")]
        if len(probs) != 3 or abs(sum(probs) - 1.0) > 1e-9:
            raise ValueError(f"--decoy-probs needs 3 values summing to 1, got {decoy_probs}")
        p_loss = loss_probability(distance_km, attenuation) if loss_where == "model" else 0.0
        decoy_cfg = {
            "intensities": {"signal": mu_signal, "decoy": mu_decoy, "vacuum": mu_vacuum},
            "probs": probs,
            "loss_probability": p_loss,
        }
        loss_where = "none"     # channel stays lossless; thinning already applied

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
                            polarization_error=1.0 - fidelity, seed=seed + 1,
                            basis_bias=basis_bias, dead_time=dead_time,
                            timing_jitter=timing_jitter,
                            pulse_period_ns=pulse_period_ns)

    # raw mode: photons (P4 path) race the TCP QUBITS_DONE marker -> drain window
    drain_ps = int(photon_drain_ms * 1e9) if quantum_transport == "raw" else 0

    # Eavesdropper on the quantum channel (Bob receives Eve's resent photons).
    eve = None
    if role == 1 and eve_fraction > 0.0:
        from qne.eve import InterceptResendEve
        eve = InterceptResendEve(eve_fraction, seed=seed + 555)

    # swap in the distributed BB84 protocol
    dbb = DistributedBB84(node, f"{name}.BB84", f"{name}.lightsource",
                          f"{name}.qsdetector", role=role, seed=seed, on_key=on_key,
                          detector=detector, sample_fraction=sample_fraction,
                          num_pulses=num_pulses, photon_mode=photon_mode,
                          photon_drain_ps=drain_ps, eavesdropper=eve,
                          basis_bias=basis_bias, decoy=decoy_cfg)
    node.set_protocol_layer(0, dbb)
    pair_distributed(dbb, role, f"{peer}.BB84", peer)

    # classical channel: a TCP link (dev / control plane) or a raw-L2 0x7102
    # reliable-datagram link through the same P4 switch (the emulated classical
    # channel — pairs with --quantum-transport raw). Bob serves, Alice connects.
    if classical_transport == "l2":
        from .l2_link import ReliableLink
        c_iface = classical_iface or photon_iface or ("veth1" if role == 0 else "veth3")
        # MACs: src = THIS node's real NIC MAC, dst = THIS node's switch-side port
        # MAC. dst MUST be a real switch-port MAC (not a placeholder) or the FABRIC
        # OVS fabric drops the frame instead of delivering it to BMv2's ingress —
        # exactly as Alice's photon TX addresses the switch. The switch rewrites
        # BOTH MACs to the peer's real MAC on egress (classical_channel_params), and
        # the receiver filters on ethertype only, so the peer MAC is never needed
        # here. deploy_fabric passes the correct per-node --src-mac/--dst-mac (for
        # Alice these are the same values her photon TX already uses). The
        # reliable-datagram shim + qne/auth ride on top unchanged.
        link = ReliableLink(auth_key=auth_key, interface=c_iface,
                            src_mac=src_mac, dst_mac=dst_mac)
    else:
        link = Link(auth_key=auth_key)
    if role == 1:
        link.serve(host, port)
    else:
        link.connect(host, port)

    # One shared sim-time origin: Bob is the time master, Alice adopts his epoch
    # corrected by a Cristian offset estimate (timesync.py). This is what lets
    # the lookahead scheduler deliver frames at exactly t_send + channel_delay —
    # the simulator's event time — with no PTP / synchronized wall clocks.
    #
    # Interim central-timeline step (2026-07-15): the epoch may be *seeded by the
    # orchestrator* (`--epoch-ns`, from the deploy run-plan) instead of picked
    # locally, so a whole multi-node run stamps against ONE origin the orchestrator
    # owns — the first move toward hosting the authoritative timeline centrally.
    # 0 = pick locally (back-compat). The client always adopts the master's value.
    from .timesync import request_epoch, serve_epoch
    if role == 1:
        epoch_ns = epoch_seed_ns or time_ns()
        peer_offset_ns, rtt_ns = serve_epoch(link, epoch_ns)
        timesync_info = {"role": "master", "offset_ns": peer_offset_ns,
                         "rtt_ns": rtt_ns, "epoch_seeded": bool(epoch_seed_ns)}
    else:
        epoch_ns, peer_offset_ns, rtt_ns = request_epoch(link)
        timesync_info = {"role": "client", "offset_ns": peer_offset_ns,
                         "rtt_ns": rtt_ns}

    node.cchannels[peer] = RemoteClassicalChannel(link, delay=channel_delay,
                                                  timeline=tl)

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
                loss_probability=sw_loss, seed=seed + 2,
                rate_hz=photon_rate_hz)
        else:          # Bob receives photons on a raw RX thread
            node.qchannels[peer] = RawQuantumChannel(iface)  # unused TX placeholder
            raw_rx = RawPhotonReceiver(iface, tl, dbb, peer, delay=channel_delay)
            raw_rx.start()
        # classical frames only on the TCP link
        listener = Listener(tl, node, dbb, delay=channel_delay)
    else:
        # descriptor-on-wire over the shared TCP link (no switch); software loss
        node.qchannels[peer] = RemoteQuantumChannel(link, delay=channel_delay,
                                                    loss_probability=sw_loss, seed=seed + 2,
                                                    timeline=tl)
        listener = Listener(tl, node, dbb, delay=channel_delay)

    link.on_frame = listener.on_frame
    link.start_rx()

    tl.init()

    # the epoch negotiated in the handshake above — both timelines now agree on
    # the wall<->sim mapping to within ~RTT/2
    tl.set_epoch(epoch_ns)

    if role == 0:  # Alice kicks off after the start barrier
        tl.schedule(Event(tl.now() + _START_BARRIER_PS,
                          Process(dbb, "push", [key_length, key_num])))

    # safety stop so a hung run can't block forever
    tl.stop_time = _START_BARRIER_PS + int(60e12)
    tl.run()
    if raw_rx is not None:
        raw_rx.stop()

    # Post-processing over the (now idle) TCP link: Cascade reconciliation then
    # privacy amplification. Both sides hold key_bits in the same key_order; we swap
    # the link into synchronous RPC mode now that the timeline has stopped.
    from .remote_qm import RpcChannel
    from .reconcile_link import bits_to_int, drive_cascade, serve_parities

    reconciled = False
    corrections = bits_leaked = 0
    rpc = None
    sift_key = list(getattr(dbb, "key_bits", None) or [])   # aligned key on both sides
    final_key = sift_key                                    # unamplified fallback
    qber = dbb.metrics.get("qber", 0.0)
    # Above the ~11% threshold there's no secure key, so abort rather than waste
    # effort reconciling. Both sides see the same QBER, so they agree (no deadlock).
    secure_ok = dbb.metrics.get("secure_fraction", 0.0) > 0
    if do_reconcile and sift_key and secure_ok:
        # rebinds link.on_frame to a buffered queue; Cascade traffic is classical
        # channel traffic, so it gets the same lookahead pacing as the protocol
        rpc = RpcChannel(link, delay_ps=channel_delay,
                         peer_offset_ns=peer_offset_ns)
        finite = ({"n_sample": int(result.get("num_sampled") or 0),
                   "eps_sec": eps_sec, "eps_cor": eps_cor} if finite_key else None)
        if role == 1:            # Bob drives Cascade + announces the PA hash
            final_key, corrections, bits_leaked = drive_cascade(
                rpc, sift_key, qber, seed + 303, passes=cascade_passes, finite=finite)
        else:                    # Alice answers parities, then applies the same PA hash
            final_key, corrections, bits_leaked = serve_parities(rpc, sift_key)
        reconciled = True

    link.close()

    # Finite-key accounting (metrics): both sides hold identical inputs — the same
    # sample size, QBER, and (announced) Cascade leak — so they report the same bound.
    finite_info = None
    if finite_key and reconciled:
        from qne.finite_key import finite_key_length
        fk = finite_key_length(len(sift_key), int(result.get("num_sampled") or 0),
                               qber, bits_leaked, eps_sec=eps_sec, eps_cor=eps_cor)
        finite_info = {"secret_bits": fk.secret_bits,
                       "asymptotic_bits": fk.asymptotic_bits,
                       "qber_upper": fk.qber_upper, "mu": fk.mu,
                       "eps_sec": eps_sec, "eps_cor": eps_cor}

    # Report the extracted secret key — reconciled+amplified → identical bit-for-bit.
    reconciled_key = bits_to_int(final_key) if final_key else result.get("key")
    secure_key_len = len(final_key) if reconciled else 0
    return {
        "role": role,
        "name": name,
        "quantum_transport": quantum_transport,
        "classical_transport": classical_transport,
        "loss_where": loss_where,
        "key": reconciled_key,                       # post-Cascade key (Bob corrected)
        "qber": result.get("qber"),
        "qber_x": result.get("qber_x"),
        "basis_bias": basis_bias,
        "sift_ratio": result.get("sift_ratio"),
        "detected_pulses": result.get("detected_pulses"),
        "dead_time_drops": detector.dead_time_drops if detector else None,
        "sifted_bits": result.get("sifted_bits"),
        "key_bits": result.get("key_bits"),          # sifted minus disclosed sample
        "num_sampled": result.get("num_sampled"),
        "secure_fraction": result.get("secure_fraction"),
        "final_key_bits": result.get("final_key_bits"),
        "reconciled": reconciled,
        "corrections": corrections,
        "bits_leaked": bits_leaked,
        "secure_key_bits": secure_key_len,
        "finite_key": finite_info,
        "photon_mode": result.get("photon_mode", photon_mode),
        "photons_emitted": result.get("photons_emitted"),
        "elapsed_s": result.get("elapsed_s"),
        "photons_per_s": result.get("photons_per_s"),
        "loss_probability": (decoy_cfg["loss_probability"] if decoy_cfg else
                             0.0 if loss_where == "none" else
                             loss_probability(distance_km, attenuation)),
        "decoy": result.get("decoy"),
        "eve_fraction": eve_fraction,
        "eve_photons_intercepted": result.get("eve_photons_intercepted"),
        "tx_frames": link.tx_count,
        "rx_frames": link.rx_count,
        "authenticated": auth_key is not None,
        "auth_failures": link.auth_failures,
        "remote_access_errors": len(tl.remote_access_errors),
        # emulation-fidelity certificate: with channel_delay > 0, every frame
        # should fire at exactly t_send + delay (late_events == 0 means the
        # run executed the simulator's event schedule). Counts cover both the
        # timeline phase (listener) and the Cascade RPC phase.
        "channel_delay_ps": channel_delay,
        "timesync": timesync_info,
        "lookahead": {
            "on_time_events": listener.on_time_events
                              + (rpc.on_time_events if rpc else 0),
            "late_events": listener.late_events
                           + (rpc.late_events if rpc else 0),
            "max_lateness_ps": max(listener.max_lateness_ps,
                                   (rpc.max_lateness_ns * 1000) if rpc else 0),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one distributed SeQUeNCe QKD node.")
    ap.add_argument("--role", required=True, choices=[*_ROLES, "repeater"])
    ap.add_argument("--protocol", choices=["bb84", "e91", "bbm92", "repeater"],
                    default="bb84",
                    help="bb84=prepare-and-measure; e91/bbm92=entanglement-based "
                         "(shared quantum-state service; alice hosts the register); "
                         "repeater=3-process entanglement-swapping chain "
                         "(alice=source/register, repeater=swap+herald, bob=far end)")
    ap.add_argument("--num-pairs", type=int, default=20000,
                    help="entanglement protocols: Bell pairs to generate")
    ap.add_argument("--chain-mode", choices=["bbm92", "e91"], default="bbm92",
                    help="repeater protocol: bbm92=Z/X key; e91=adds the CHSH "
                         "Bell test across the swapped chain")
    ap.add_argument("--port-ar", type=int, default=0,
                    help="repeater protocol: alice<->repeater link port "
                         "(0 = --port + 1; the repeater listens)")
    ap.add_argument("--port-rb", type=int, default=0,
                    help="repeater protocol: repeater->bob herald link port "
                         "(0 = --port + 2; bob listens)")
    ap.add_argument("--no-correction", dest="correction", action="store_false",
                    help="repeater protocol: skip the heralded Pauli correction "
                         "(control run — QBER collapses to 0.5)")
    ap.add_argument("--bob-host", default=None,
                    help="repeater protocol: address where bob's listeners are "
                         "reached (default: --host; set on FABRIC where each "
                         "link terminates on a different node)")
    ap.add_argument("--repeater-host", default=None,
                    help="repeater protocol: address where the repeater "
                         "station's listener is reached (default: --host)")
    ap.add_argument("--num-stations", type=int, default=1,
                    help="repeater protocol: number of repeater stations K "
                         "(chain has K+2 nodes, K+1 links; station i listens on "
                         "port+2i-1, bob listens for it on port+2i)")
    ap.add_argument("--station-index", type=int, default=1,
                    help="repeater protocol, --role repeater: which station this "
                         "process is (1-based)")
    ap.add_argument("--repeater-hosts", default=None,
                    help="repeater protocol, --role alice: comma-separated list "
                         "of the K station addresses (default: --repeater-host "
                         "or --host for all)")
    ap.add_argument("--reconcile", action=argparse.BooleanOptionalAction, default=True,
                    help="run Cascade error reconciliation so both keys match "
                         "bit-for-bit (--no-reconcile to skip)")
    ap.add_argument("--cascade-passes", type=int, default=4,
                    help="number of Cascade passes")
    ap.add_argument("--finite-key", action="store_true",
                    help="size privacy amplification with the finite-key bound "
                         "(Serfling-corrected QBER + eps terms) instead of the "
                         "asymptotic fraction; adds finite_key metrics")
    ap.add_argument("--eps-sec", type=float, default=1e-9,
                    help="finite-key security failure budget")
    ap.add_argument("--eps-cor", type=float, default=1e-15,
                    help="finite-key correctness failure budget")
    ap.add_argument("--auth-key", default=None,
                    help="pre-shared key: HMAC-authenticate every classical frame "
                         "(tag + anti-replay seq); both sides must pass the same key")
    ap.add_argument("--basis-bias", type=float, default=0.5,
                    help="P(Z basis) for both sides; >0.5 = efficient BB84 "
                         "(sift ratio p^2+(1-p)^2 > 50%%; key from Z, X estimates "
                         "the phase error)")
    ap.add_argument("--dead-time", type=float, default=0.0,
                    help="detector dead time in ns after each click "
                         "(needs --pulse-period-ns to place arrivals)")
    ap.add_argument("--timing-jitter", type=float, default=0.0,
                    help="detector timing jitter sigma in ns (clicks outside the "
                         "1 ns gate are lost)")
    ap.add_argument("--pulse-period-ns", type=float, default=0.0,
                    help="emulated pulse slot spacing in ns (arrival time of "
                         "photon k = k*period); required for dead-time gating")
    ap.add_argument("--decoy", action="store_true",
                    help="decoy-state source on the live transport: Poisson(mu) "
                         "photons per pulse at 3 intensities, measured gains/QBERs "
                         "feed the Lo-Ma-Chen/GLLP analysis (key from signal pulses)")
    ap.add_argument("--mu-signal", type=float, default=0.6)
    ap.add_argument("--mu-decoy", type=float, default=0.1)
    ap.add_argument("--mu-vacuum", type=float, default=0.001)
    ap.add_argument("--decoy-probs", default="0.7,0.2,0.1",
                    help="P(signal),P(decoy),P(vacuum) per pulse, comma-separated")
    ap.add_argument("--name", required=True)
    ap.add_argument("--peer", default=None,
                    help="peer node name (required for the two-party protocols)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=57123)
    ap.add_argument("--key-length", type=int, default=128)
    ap.add_argument("--key-num", type=int, default=1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--time-scale", type=float, default=1.0)
    ap.add_argument("--epoch-ns", type=int, default=0,
                    help="orchestrator-seeded shared sim-time epoch (wall-clock ns) "
                         "for the time master; 0 = master picks it locally. The "
                         "interim central-timeline step: one origin for a whole "
                         "multi-node run (metric alignment), owned by the run-plan.")
    ap.add_argument("--channel-delay", default="0",
                    help="modeled one-way channel delay in ps, or 'auto' to "
                         "derive it from --distance-km (~4.9e6 ps per km — the "
                         "unified distance knob: one L drives loss AND delay). "
                         "When > 0, every classical message is delivered at "
                         "exactly t_send + delay in shared clock terms "
                         "(lookahead mode — matches the simulator's schedule "
                         "as long as real latency stays below it; late frames "
                         "are counted in the result). 0 = legacy: deliver at "
                         "real arrival time.")
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
    # classical transport: tcp (dev / control plane) or raw 0x7102 through P4
    ap.add_argument("--classical-transport", choices=["tcp", "l2"], default="tcp",
                    help="classical control channel: tcp=length-prefixed JSON over "
                         "TCP (dev / control plane); l2=raw 0x7102 Ethernet frames "
                         "through the same P4 switch with a reliable-datagram shim "
                         "(seq/ack/resend/dedup + fragmentation). Pairs with "
                         "--quantum-transport raw. AF_PACKET (Linux/slice) only.")
    ap.add_argument("--classical-iface", default=None,
                    help="raw-socket interface for --classical-transport l2 "
                         "(default: --photon-iface, else veth1/veth3 by role)")
    ap.add_argument("--loss", choices=["auto", "model", "switch", "none"], default="auto",
                    help="photon loss: none=lossless (ignore distance/atten); "
                         "model=software P(dist,atten); switch=external BMv2 P4; "
                         "auto=software for tcp, switch for raw")
    ap.add_argument("--photon-iface", default=None,
                    help="raw-socket interface (default veth1 for alice, veth3 for bob)")
    ap.add_argument("--src-mac", default="02:00:00:00:00:01")
    ap.add_argument("--dst-mac", default="02:00:00:00:00:02")
    ap.add_argument("--wavelength", type=int, default=0, help="P4 loss-table key / WDM tag")
    ap.add_argument("--photon-rate-hz", type=float, default=10000.0,
                    help="raw bulk TX pacing in frames/s (0 = unpaced burst; an "
                         "unpaced 20k burst overruns BMv2/socket buffers and the "
                         "drops masquerade as fiber loss)")
    ap.add_argument("--photon-drain-ms", type=float, default=200.0,
                    help="raw mode: wait for straggler photons after QUBITS_DONE")
    ap.add_argument("--eve-fraction", type=float, default=0.0,
                    help="intercept-resend eavesdropper: fraction of photons Eve taps "
                         "[0,1]. Adds QBER ~ 0.25*f on the sifted key (BB84 path).")
    args = ap.parse_args(argv)

    if args.role == "repeater" and args.protocol != "repeater":
        ap.error("--role repeater requires --protocol repeater")
    if args.protocol != "repeater" and not args.peer:
        ap.error("--peer is required for the two-party protocols")

    channel_delay = (propagation_delay_ps(args.distance_km)
                     if args.channel_delay == "auto" else int(args.channel_delay))

    if args.protocol == "repeater":
        from .distributed_repeater import ROLES as _ROLES3
        from .distributed_repeater import run_repeater_node
        loss_p = (0.0 if args.loss == "none"
                  else loss_probability(args.distance_km, args.attenuation))
        result = run_repeater_node(
            _ROLES3[args.role], args.name, args.host,
            port_ab=args.port,
            port_ar=args.port_ar or args.port + 1,
            port_rb=args.port_rb or args.port + 2,
            num_stations=args.num_stations, station_index=args.station_index,
            num_pairs=args.num_pairs, fidelity=args.fidelity,
            loss_probability=loss_p, mode=args.chain_mode,
            sample_fraction=args.sample_fraction, seed=args.seed,
            do_reconcile=args.reconcile, cascade_passes=args.cascade_passes,
            finite_key=args.finite_key, eps_sec=args.eps_sec,
            eps_cor=args.eps_cor, auth_key=args.auth_key,
            apply_correction=args.correction,
            bob_host=args.bob_host, repeater_host=args.repeater_host,
            repeater_hosts=(args.repeater_hosts.split(",")
                            if args.repeater_hosts else None),
            channel_delay=channel_delay)
        print(json.dumps(result))
        return 0

    if args.protocol in ("e91", "bbm92"):
        from .distributed_e91 import run_e91_node
        loss_p = (0.0 if args.loss == "none"
                  else loss_probability(args.distance_km, args.attenuation))
        result = run_e91_node(
            _ROLES[args.role], args.name, args.peer, args.host, args.port,
            num_pairs=args.num_pairs, fidelity=args.fidelity,
            loss_probability=loss_p, mode=args.protocol,
            sample_fraction=args.sample_fraction, seed=args.seed,
            do_reconcile=args.reconcile, cascade_passes=args.cascade_passes,
            finite_key=args.finite_key, eps_sec=args.eps_sec, eps_cor=args.eps_cor,
            auth_key=args.auth_key, channel_delay=channel_delay)
        print(json.dumps(result))
        return 0

    result = run_node(args.role, args.name, args.peer, args.host, args.port,
                      args.key_length, args.key_num, args.seed, args.time_scale,
                      channel_delay, args.distance_km, args.attenuation,
                      args.fidelity, args.efficiency, args.dark_count_rate,
                      args.sample_fraction, args.num_pulses or None, args.photon_mode,
                      args.quantum_transport, args.photon_iface, args.src_mac,
                      args.dst_mac, args.wavelength, args.photon_drain_ms, args.loss,
                      args.photon_rate_hz, args.eve_fraction,
                      args.reconcile, args.cascade_passes,
                      args.finite_key, args.eps_sec, args.eps_cor, args.auth_key,
                      args.basis_bias, args.dead_time, args.timing_jitter,
                      args.pulse_period_ns, args.decoy, args.mu_signal,
                      args.mu_decoy, args.mu_vacuum, args.decoy_probs,
                      classical_transport=args.classical_transport,
                      classical_iface=args.classical_iface,
                      epoch_seed_ns=args.epoch_ns)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
