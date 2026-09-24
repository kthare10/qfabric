"""Distributed quantum computing over a real link (ROADMAP Phase 6).

Two processes run a gate between QPUs that share no qubit, spending network
entanglement and putting the correction bits on the wire:

  * **alice** (role 0) is the *control* node and hosts the QuantumStateService —
    the register authority, same as E91 and the repeater chain. She creates the
    Bell pairs, allocates her own data qubits, runs her half of each gate locally,
    and serves bob's quantum ops over the link.
  * **bob** (role 1) is the *target* node. He holds no local register; his data
    qubits are allocated in the shared one and his half of each gate is an RPC
    (``APPLY_REQ`` / ``RECV_REQ``) — the same "op over the wire" seam the repeater
    station uses for its BSMs.

What is genuinely distributed is the op stream and, crucially, the **corrections**:
m1 reaches bob inside ``PLAN`` and m2 returns to alice inside ``ACK``, and each side
applies the value **as it arrived**, not the one it could have computed locally. So
withholding a bit on the wire really does corrupt the result — which is what makes
``--dqc-drop`` a meaningful control rather than a simulated one.

Payload. Each run measures both Pauli channels, exactly like the in-process
``dqc.run_dqc_session``:

  * **bit channel** — control |0>, target |0>, both read out in Z; a faulty gate
    flips the target.
  * **phase channel** — control |+>, target |0>, so a correct non-local CNOT leaves
    the two nodes holding |Phi+>; both read out in X, where a correct pair agrees.
    This is the demonstrable part: two QPUs that shared no qubit now hold an
    entangled state *they computed*, using entanglement the network supplied.

Both channels err at (1-w)/2 for Werner weight w — the E91 QBER curve — so the
platform prices a distributed gate and a distributed key with one number.

Message flow per channel (telegate; teleport drops the ACK leg and needs one bit
fewer round trip)::

    A -> B  PLAN         {b_ids, m1}          the A->B correction channel
    B -> A  APPLY_REQ    {b_ids, m1 as seen}  bob's half of the gate
    A -> B  APPLY_RESP   {m2, target_ids}
    B -> A  ACK          {m2}                 the B->A correction channel
    B -> A  MEASURE_REQ  {(target, angle)}    bob reads his qubits out
    A -> B  MEASURE_RESP {outcomes}
    A -> B  SUMMARY      {errors}
"""

from __future__ import annotations

import math

from . import dqc
from .listener import Link, lookahead_report
from .quantum_state_service import QuantumStateService
from .remote_qm import LogicalClock, RpcChannel
from .timesync import sync_link

# (name, data state alice prepares, readout angle) for the two Pauli channels
_CHANNELS = (("bit", "zero", 0.0), ("phase", "plus", math.pi / 2))


def _seen(value: int, name: str, withheld: str | None) -> int:
    """The correction bit as the applying node sees it (0 if it never arrived)."""
    return 0 if withheld == name else value


def run_dqc_node(role: int, name: str, peer: str, host: str, port: int, *,
                 num_gates: int = 2000, primitive: str = "telegate",
                 fidelity: float = 1.0, loss_probability: float = 0.0,
                 seed: int = 0, auth_key: str | None = None,
                 channel_delay: int = 0, classical_transport: str = "tcp",
                 classical_iface: str | None = None,
                 src_mac: str | None = None, dst_mac: str | None = None,
                 logical_clock: bool = False,
                 dropped: str | None = None,
                 late: str | None = None) -> dict:
    """Run one side of a distributed teleport / non-local-CNOT session.

    ``dropped`` withholds a correction bit on the wire permanently; ``late``
    withholds it for the gate and folds it into the recorded outcome afterwards
    (a tracked Pauli frame — the result comes back clean, so latency costs memory
    time rather than fidelity). See ``dqc.py``.
    """
    if primitive not in ("teleport", "telegate"):
        raise ValueError(f"unknown primitive {primitive!r}")
    if not isinstance(num_gates, int) or isinstance(num_gates, bool) or num_gates < 1:
        raise ValueError(f"num_gates must be a positive integer, got {num_gates!r}")
    if not 0.0 <= fidelity <= 1.0:
        raise ValueError(f"fidelity (Werner w) must lie in [0, 1], got {fidelity!r}")
    if not 0.0 <= loss_probability <= 1.0:
        raise ValueError(f"loss_probability must lie in [0, 1], got {loss_probability!r}")
    for label, value in (("dropped", dropped), ("late", late)):
        if value not in (None, "m1", "m2"):
            raise ValueError(f"{label} must be None, 'm1' or 'm2', got {value!r}")
    if dropped is not None and late is not None:
        raise ValueError("a correction bit is either dropped or late, not both")

    if classical_transport == "l2":
        from .l2_link import ReliableLink
        c_iface = classical_iface or ("veth1" if role == 0 else "veth3")
        link = ReliableLink(auth_key=auth_key, interface=c_iface,
                            src_mac=src_mac, dst_mac=dst_mac)
    else:
        link = Link(auth_key=auth_key)
    if role == 1:                       # bob listens; alice connects (as in E91)
        link.serve(host, port)
    else:
        link.connect(host, port)
    peer_offset_ns, rtt_ns = sync_link(link, serving=(role == 1))
    clock = LogicalClock() if logical_clock else None
    rpc = RpcChannel(link, delay_ps=channel_delay, peer_offset_ns=peer_offset_ns,
                     clock=clock)
    link.start_rx()

    try:
        if role == 0:
            result = _run_control(rpc, num_gates, primitive, fidelity,
                                  loss_probability, seed, dropped, late)
        else:
            result = _run_target(rpc, primitive, dropped, late)
    finally:
        link.close()

    result.update({"role": role, "name": name, "primitive": primitive,
                   "timeline": ("logical" if logical_clock else "wall-clock"),
                   "sim_elapsed_ps": rpc.sim_elapsed_ps,
                   "quantum_transport": "entangled-state-service",
                   "classical_transport": classical_transport,
                   "tx_frames": link.tx_count, "rx_frames": link.rx_count,
                   "authenticated": auth_key is not None,
                   "auth_failures": link.auth_failures,
                   "short_frames": link.short_frames,
                   "channel_delay_ps": channel_delay,
                   "dropped": dropped, "late": late,
                   "timesync": {"offset_ns": peer_offset_ns, "rtt_ns": rtt_ns},
                   "lookahead": lookahead_report(rpc.on_time_events, rpc.late_events,
                                                 rpc.max_lateness_ns * 1000,
                                                 channel_delay),
                   "werner_w": fidelity,
                   "bell_fidelity": (3.0 * fidelity + 1.0) / 4.0})
    return result


def _run_control(rpc, num_gates, primitive, fidelity, loss_probability, seed,
                 dropped, late) -> dict:
    """Alice: register authority, control node, and the side that scores the run."""
    svc = QuantumStateService(seed=seed)
    withheld = dropped or late
    errors: dict[str, float] = {}
    completed = 0

    for channel, state, readout in _CHANNELS:
        pairs = svc.create_pairs(num_gates, fidelity=fidelity,
                                 loss_probability=loss_probability)
        a_ids, b_ids, surviving = pairs["a_ids"], pairs["b_ids"], pairs["surviving"]
        live = [i for i in range(num_gates) if surviving[i]]
        for i in range(num_gates):                     # a lost half kills the gate
            if not surviving[i]:
                svc.discard_pair(a_ids[i], b_ids[i])
        live_b = [b_ids[i] for i in live]

        my_ids = svc.alloc_batch(len(live), state)     # controls, or data to teleport
        if primitive == "telegate":
            m1 = [dqc.telegate_cnot_send(svc.register, my_ids[k], a_ids[i])
                  for k, i in enumerate(live)]
            rpc.send("PLAN", {"channel": channel, "state": state,
                              "readout": readout, "b_ids": live_b, "m1": m1})
            req = rpc.recv("APPLY_REQ")
            targets = svc.alloc_batch(len(req["b_ids"]), "zero")
            m2 = svc.telegate_apply_batch(
                list(zip(req["b_ids"], targets, req["m1"], strict=True)))
            rpc.send("APPLY_RESP", {"m2": m2, "target_ids": targets})
            # the m2 alice applies is the one that came back over the link
            wire_m2 = rpc.recv("ACK")["m2"]
            for k, control in enumerate(my_ids):
                dqc.telegate_cnot_finish(svc.register,
                                         control, _seen(wire_m2[k], "m2", withheld))
            a_bits = [svc.measure(q, readout) for q in my_ids]
            # the Pauli frame still owed: bob's deferred m1 (he reports what HE
            # received) and alice's own deferred m2 (the value ACK carried)
            frame_m1 = req["frame_m1"]
            frame_m2 = [v if late == "m2" else 0 for v in wire_m2]
        else:
            heralds = [dqc.teleport_send(svc.register, my_ids[k], a_ids[i])
                       for k, i in enumerate(live)]
            m1 = [h[0] for h in heralds]
            m2 = [h[1] for h in heralds]
            rpc.send("PLAN", {"channel": channel, "state": state,
                              "readout": readout, "b_ids": live_b,
                              "m1": m1, "m2": m2})
            req = rpc.recv("RECV_REQ")
            svc.teleport_recv_batch(
                list(zip(req["b_ids"], req["m1"], req["m2"], strict=True)))
            rpc.send("RECV_RESP", {})
            targets = req["b_ids"]                     # the teleported state lives here
            a_bits = None
            frame_m1, frame_m2 = req["frame_m1"], req["frame_m2"]

        # bob reads his qubits out through the register authority
        meas = rpc.recv("MEASURE_REQ")
        b_bits = [svc.measure(qid, ang) for qid, ang in meas["reqs"]]
        rpc.send("MEASURE_RESP", {"outcomes": b_bits})

        # Fold the outstanding Pauli frame into the recorded outcomes. These are
        # the bits **as they crossed the link** (bob reports the m1 he received;
        # frame_m2 is what ACK carried) -- never alice's local copies, or a
        # corrupted wire value would be silently repaired from clean local state
        # and the run would claim a recovery that did not happen.
        #
        # Which Pauli a bit carries is primitive-specific and NOT symmetric:
        # telegate -- m1 is the X on bob's target, m2 the Z on alice's control;
        # teleport -- the correction is X^m2.Z^m1, so m2 is the X and m1 the Z,
        # both on bob's qubit.
        bad = 0
        for k in range(len(live)):
            if primitive == "telegate":
                b_out = dqc.correct_outcome(b_bits[k], readout, x=frame_m1[k])
                a_out = dqc.correct_outcome(a_bits[k], readout, z=frame_m2[k])
                # bit channel: |0>|0> -> the target must read 0.
                # phase channel: |+>|0> -> |Phi+>, whose X outcomes agree.
                bad += (b_out != 0) if channel == "bit" else (a_out != b_out)
            else:
                # teleport: bob holds alice's state, so his readout must be 0
                b_out = dqc.correct_outcome(b_bits[k], readout,
                                            x=frame_m2[k], z=frame_m1[k])
                bad += b_out != 0
        errors[channel] = bad / len(live) if live else 0.0
        completed += len(live)

    gates = completed
    summary = {
        "primitive": primitive,
        "gates_attempted": 2 * num_gates,
        "gates_completed": gates,
        "pairs_consumed": gates,
        # telegate: 1 bit each way; teleport: 2 bits A->B
        "classical_bits": 2 * gates,
        "bit_error": errors["bit"],
        "phase_error": errors["phase"],
        "error_pred": None if dropped else (1.0 - fidelity) / 2.0,
    }
    rpc.send("SUMMARY", summary)
    return summary


def _run_target(rpc, primitive, dropped, late) -> dict:
    """Bob: no local register — his qubits and his half of each gate are RPCs.

    ``late`` withholds the bit here exactly as ``dropped`` does — a late bit missed
    the gate, which is the whole point; the difference is that alice then folds it
    into the recorded outcome instead of living with the Pauli error.
    """
    withheld = dropped or late
    for _ in _CHANNELS:
        plan = rpc.recv("PLAN")
        readout = plan["readout"]
        # what bob applies, and what he still owes: both derived from the values
        # that actually reached him, so a corrupted PLAN corrupts the result
        m1 = [_seen(v, "m1", withheld) for v in plan["m1"]]
        frame_m1 = [v if late == "m1" else 0 for v in plan["m1"]]
        if primitive == "telegate":
            resp = rpc.call("APPLY_REQ", {"b_ids": plan["b_ids"], "m1": m1,
                                          "frame_m1": frame_m1},
                            expected="APPLY_RESP")
            targets = resp["target_ids"]
            rpc.send("ACK", {"m2": resp["m2"]})        # bob owes m2 back to alice
        else:
            m2 = [_seen(v, "m2", withheld) for v in plan["m2"]]
            frame_m2 = [v if late == "m2" else 0 for v in plan["m2"]]
            rpc.call("RECV_REQ", {"b_ids": plan["b_ids"], "m1": m1, "m2": m2,
                                  "frame_m1": frame_m1, "frame_m2": frame_m2},
                     expected="RECV_RESP")
            targets = plan["b_ids"]
        rpc.call("MEASURE_REQ", {"reqs": [[int(q), readout] for q in targets]},
                 expected="MEASURE_RESP")
    return dict(rpc.recv("SUMMARY"))
