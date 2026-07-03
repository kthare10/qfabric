# QFabric × SeQUeNCe — Distributed Emulator Design

> **Status:** v0.1 design draft (2026-06-27)
> **Goal:** Run real **SeQUeNCe** node instances as separate processes on separate
> hosts (FABRIC sites), and have SeQUeNCe's quantum/classical channels carry
> **actual traffic on the wire** instead of in-memory event scheduling.
> **First target:** BB84 over 2 nodes (real SeQUeNCe `QKDNode`s), reusing QFabric's
> existing photon wire format + P4 loss model.
> **Companion docs:** [`../SPEC.md`](../SPEC.md), [`../ROADMAP.md`](../ROADMAP.md), [`../README.md`](../README.md)

---

## 1. Motivation

Today QFabric is a **hand-coded** BB84 emulator (`qne/alice.py`, `qne/bob.py`): it
gets real-WAN realism on FABRIC but reimplements the quantum-protocol logic itself,
and uses SeQUeNCe only as an off-node *cross-validation* reference.

The opportunity: instead of re-implementing each protocol by hand, **run SeQUeNCe
itself as the node engine, distributed across hosts.** That gives us SeQUeNCe's full,
peer-reviewed protocol stack — BB84, Cascade, entanglement generation/purification/
swapping, network & resource management — running over real links. QFabric becomes a
*distributed runtime for SeQUeNCe*, keeping its research lever intact: **real latency,
jitter, and congestion on the classical (and photon) path feed back into the quantum
protocol naturally.** This directly unblocks ROADMAP Phase 4 (repeater chains,
entanglement distribution) and the E91/swapping protocol backlog.

### Design decisions (locked for this draft)

| Decision | Choice | Consequence |
|---|---|---|
| **Time model** | Real-time emulation (wall-clock) | Channel delay = real WAN latency + modeled fiber delay; ps-accurate quantum *timing* is sacrificed; realistic net effects are gained. |
| **Quantum state across nodes** | **Pluggable** per channel/protocol | Prepare-and-measure (BB84): serialize photon *descriptor* on the wire. Entanglement protocols: shared **Quantum State Service**. |
| **First build target** | BB84 over 2 nodes | Real SeQUeNCe `QKDNode` on each host; directly comparable to existing QFabric results. |
| **Deliverable** | This design doc first | Implementation phased in §11. |

---

## 2. Why SeQUeNCe can't be distributed as-is

SeQUeNCe is a **single-process, discrete-event** simulator. Three properties block
distribution; each maps to a precise integration seam.

1. **One global `Timeline` in virtual picoseconds.**
   `Timeline.run()` pops events from a min-heap and *jumps* `self.time = event.time`
   instantly — there is no relation to wall-clock. `now()` returns virtual ps.
   - *Seam:* replace `Timeline` with a **`RealTimeTimeline`** whose `now()` is derived
     from wall-clock and whose `run()` sleeps until each event's real firing time.

2. **Channels move objects by reference into that one heap.**
   `ClassicalChannel.transmit()` (optical_channel.py:301) does:
   ```python
   process = Process(self.receiver, "receive_message", [source.name, message])
   event = Event(now + sender_delay + self.delay, process, priority)
   self.timeline.schedule(event)          # same in-memory heap, message by reference
   ```
   `QuantumChannel.transmit()` (optical_channel.py:132) is identical with
   `"receive_qubit"` and a `Photon`. The receiver is a live Python object in the same
   process.
   - *Seam:* subclass these into **`RemoteClassicalChannel` / `RemoteQuantumChannel`**
     that, when the peer lives on another host, **serialize and send on a socket**
     instead of scheduling locally.

3. **No serialization anywhere.** `Message` / `Photon` are passed by reference;
   `Photon.quantum_state` is either a local `FreeQuantumState` or an integer key into
   the timeline's in-process `quantum_manager`. Nothing crosses a process boundary.
   - *Seam:* a **wire codec** per message/photon type, plus a **`RemoteQuantumManager`**
     proxy for the entanglement case.

The crucial good news: the **receiver side is unchanged.** Once a frame is delivered,
the listener injects a local event that calls `node.receive_message(src, msg)` /
`node.receive_qubit(src, qubit)` exactly as SeQUeNCe expects. We only intercept the
*transmit* side and the *timeline*. Everything above the channel (BB84, Cascade,
resource/network managers, detectors, memories) runs **unmodified**.

---

## 3. Architecture overview

```
                          ┌─────────────────── FABRIC slice ───────────────────┐
   Host A (site 1)        │                                                     │   Host B (site 2)
 ┌───────────────────────┐│                                                     │┌───────────────────────┐
 │  node_runner (proc)   ││                                                     ││  node_runner (proc)   │
 │ ┌───────────────────┐ ││                                                     ││ ┌───────────────────┐ │
 │ │ SeQUeNCe QKDNode  │ ││   classical: TCP/JSON (length-prefixed)             ││ │ SeQUeNCe QKDNode  │ │
 │ │  BB84 / Cascade   │◄┼┼─────────────────────────────────────────────────►┼┼►│  BB84 / Cascade   │ │
 │ │  LightSource/QSD  │ ││         (rides real WAN ⇒ real latency/jitter)      ││ │  LightSource/QSD  │ │
 │ └───────┬───────────┘ ││                                                     ││ └─────────┬─────────┘ │
 │  RealTimeTimeline     ││   quantum: photon frames (EtherType 0x7101)         ││   RealTimeTimeline    │
 │  RemoteChannels       │┼──────────►  [ P4 BMv2 switch ]  ──────────►─────────┼│   RemoteChannels      │
 │  WireCodec / Listener ││            fiber-loss drop model                    ││   WireCodec / Listener│
 │ └─────────┬───────────┘│                                                     │└──────────┬────────────┘
 └───────────┼────────────┘                                                     └───────────┼────────────┘
             │                                                                               │
             └───────────────► (entanglement protocols only) ◄────────────────────────────┘
                               Quantum State Service  (shared QuantumManager, RPC)
```

Components (all new code lives under `qne-sequence/`, SeQUeNCe stays an unmodified dep):

| Component | Responsibility |
|---|---|
| `RealTimeTimeline` | Wall-clock-driven event loop; thread-safe event injection from the network. |
| `RemoteClassicalChannel` / `RemoteQuantumChannel` | Override `transmit()`; serialize + send to the peer host instead of local scheduling. |
| `WireCodec` | Encode/decode SeQUeNCe `Message` and `Photon` ↔ bytes (TCP/JSON for control, 0x7101 frames for photons). |
| `Listener` (per transport) | Receive bytes, decode, inject `receive_message`/`receive_qubit` event into the local timeline. |
| `NodeRunner` (CLI) | Build the *local slice* of the topology (this host's node(s)), wire remote channels, run. |
| `QuantumStateService` + `RemoteQuantumManager` | Shared quantum state for entanglement-based protocols (pluggable; off for BB84). |
| `Orchestrator` / topology splitter | Take a topology JSON + placement map, generate per-host configs, launch runners, wire endpoints. Integrates with `scripts/deploy_fabric.py`. |

---

## 4. Time model (real-time emulation)

### 4.1 Mapping virtual ps ↔ wall-clock

All runners share an **epoch** `T0` (a wall-clock instant) and a **`time_scale`**
(seconds of wall-clock per second of sim). Then:

```
now_ps()            = round( (monotonic_wallclock() - T0_mono) / time_scale * 1e12 )
wall_deadline(ev)   = T0_mono + ev.time_ps * 1e-12 * time_scale
```

- `time_scale = 1.0` → real-time (1 sim-second = 1 wall-second).
- `time_scale > 1.0` → **slow motion** (stretch sim so events spread out). This is the
  knob that makes µs-scale fiber delays and high photon rates tractable on Python +
  OS sockets — see §4.3.

`RealTimeTimeline.now()` returns `now_ps()`; SeQUeNCe code calling `timeline.now()`
keeps working. Channel delays still compute in ps (`distance / light_speed`), but a
*real-time component* (the actual socket RTT) is added by transport — so a message's
effective delay is `max(modeled_fiber_delay, real_network_delay)`, captured honestly
because the bytes really traverse the WAN.

### 4.2 Event loop with network wake-ups

```
run():
  while not stopped:
      ev = heap.peek()                      # earliest local event (None if empty)
      deadline = wall_deadline(ev) if ev else +inf
      # sleep until deadline OR until a newly-arrived network event wakes us
      wait_on(condition, timeout = deadline - monotonic_wallclock())
      now = now_ps()
      for ev in heap.pop_all_due(now):      # all events whose time <= now
          self.time = max(self.time, ev.time)
          ev.process.run()                  # → node.receive_message / receive_qubit / local dynamics
```

- The **Listener threads** push decoded inbound events into the heap under a lock and
  `notify()` the condition, so a freshly-arrived message can preempt a long sleep.
- Local hardware dynamics (detector dead-time, memory decoherence, light-source pulse
  scheduling) remain ordinary heap events — the discrete-event kernel is preserved
  *within* a host; only *cross-host* edges become sockets.

### 4.3 Photon throughput — implement both modes, benchmark, decide

BB84 in QFabric sends **100k photons at ~1 MHz**. The per-photon discrete-event model
may not survive real-time at that rate in Python (and µs fiber delays sit below socket/
OS jitter) — but rather than assume, we make the photon path a **pluggable strategy**
with two interchangeable implementations behind one interface, benchmark both in
**Phase C**, and record the crossover. `time_scale` (§4.1) is an orthogonal knob that
applies to either mode.

```python
class PhotonEmissionStrategy(ABC):
    def emit(self, pulses): ...        # Alice side: (basis, bit, wavelength, seq, ts)*
    def on_receive(self, frame): ...   # Bob side: feed SeQUeNCe QSDetector.get()
```

**Mode 1 — `PerPhotonEvent` (fidelity-faithful).** Each photon is one `Event` /
one 0x7101 frame, fully through `RealTimeTimeline`. Preserves SeQUeNCe's exact
per-photon semantics (timing, detector dynamics, per-qubit loss). Expected ceiling:
~10³–10⁴ photons/s in Python; pair with `time_scale ≫ 1` for small, correctness-grade
runs and any case where per-photon timing matters (dead-time, jitter, repeater
heralding).

**Mode 2 — `BulkStream` (throughput-faithful).** SeQUeNCe drives *protocol logic and
classical messaging in real time*, but the bulk photon stream is shimmed past the
per-event loop: `LightSource.emit` hands a **batch** of pulse descriptors to QFabric's
raw-socket + P4 pipe, and the receiving `QSDetector.get` is fed by the photon RX
listener. The P4 switch applies loss exactly as today; aggregate detections re-enter
SeQUeNCe as the inputs BB84 expects. Targets the existing ~1 MHz rate. This is the §1
"real-WAN lever" preserved, with SeQUeNCe owning protocol semantics.

**Benchmark & decision (Phase C1 artifact — measured over loopback TCP, no P4).**
Both modes are implemented (`--photon-mode`) and benchmarked by `bench_throughput.py`;
both produce correct keys/QBER with zero cross-process accesses. Measured photons/s
(macOS dev box, loopback):

| pulses | `per_event` | `bulk` |
|--:|--:|--:|
| 2,000 | ~125k/s | ~409k/s |
| 20,000 | ~133k/s | ~378k/s |
| 100,000 | ~133k/s | ~571k/s |

`per_event` plateaus near **133k photons/s** (one Event + one TCP frame per photon);
`bulk` amortizes that overhead and **scales to ~571k/s** (gap widens with count).
**Decision:** default `bulk` for throughput-grade runs; keep `per_event` for
per-photon-fidelity studies (timing/dead-time/heralding) where its ~133k/s ceiling is
acceptable; `time_scale` stretches either. Neither pure-Python mode reaches the ~1 MHz
BB84 target over loopback — confirming the Phase C2 plan: MHz rates require the
raw-socket / 0x7101 / BMv2 fast path (the strategy + channel interface is already
isolated for that swap). The selected mode is per-run config and the table ships in
results, so the trade-off is explicit, not hidden.

> **Honest limitation to document in results:** real-time mode trades SeQUeNCe's
> ps-exact timing for network realism. Quantum *ordering/causality* is preserved;
> absolute quantum timing is at OS/network resolution — and in `BulkStream`, per-photon
> timing is replaced by batch timing. State this in every dataset.

### 4.4 Clock synchronization

Cross-host event ordering needs a common clock. On FABRIC:
- Baseline: **NTP** (ms-class) — adequate when `time_scale` stretches events well
  above clock error.
- Better: **PTP / PPS** where available (the research plan already calls out PTP for
  Idea 6) — sub-µs, enabling tighter `time_scale`.
- The epoch `T0` is distributed by the Orchestrator at launch (absolute wall-clock +
  a "start at T0+Δ" countdown so all runners begin together).

---

## 5. Transport layer

Two transports, mirroring SeQUeNCe's two channel types and **reusing QFabric wire
formats verbatim** so the existing P4 switch and tooling apply unchanged.

### 5.1 Classical channel — TCP / length-prefixed JSON

Reuse `qne/channel.py`'s framing: `[4-byte big-endian length][JSON payload]`. One
persistent TCP connection per directed channel (or a multiplexed pair). On FABRIC this
rides the real WAN — the central research lever. `netem` impairment hooks
(`apply_classical_netem`) already exist and apply directly.

`RemoteClassicalChannel.transmit(message, source, priority, sender_delay)`:
```
frame = WireCodec.encode_message(message, src=source.name, priority=priority)
conn.send(frame)                       # real bytes on the WAN
# (no local Event scheduled; the peer's listener will schedule on arrival)
```

Peer **Listener**:
```
msg, src, priority = WireCodec.decode_message(frame)
ev = Event(now_ps() + modeled_delay, Process(local_node, "receive_message", [src, msg]), priority)
timeline.inject(ev)                    # thread-safe push + notify
```

### 5.2 Quantum channel — 0x7101 photon frames over the P4 switch

Reuse the QFabric photon frame exactly (`qne/photon.py`, `p4/bmv2/.../headers.p4`):

| Field | Bytes | SeQUeNCe source (BB84 / PM case) |
|---|---|---|
| version | 1 | `0x01` |
| basis | 1 | Alice's chosen basis for this pulse |
| state | 1 | Encoded bit/state |
| wavelength | 1 | P4 loss-table key (per-link / WDM) |
| sequence_num | 4 | photon index (monotonic) |
| timestamp_hi/lo | 8 | TX `now_ps()` (64-bit) |
| padding | 1 | reserved |

Transport = raw `AF_PACKET` on layer 2, EtherType `0x7101`, through the BMv2 switch
which drops with `P(loss)=1−10^(−αL/10)` (control plane already computes the
threshold). Survivors arrive at the receiving runner's photon listener and feed
SeQUeNCe's detector path (§4.3, option 2).

> This frame only carries a **classical descriptor** of the photon — correct and
> sufficient for prepare-and-measure (BB84). Entanglement needs §6.

---

## 6. Quantum-state model (pluggable)

A real qubit cannot traverse a socket. We pick, **per channel/protocol**, how the
"quantum" content is represented:

### 6.1 Descriptor-on-wire (prepare-and-measure: BB84, B92, decoy-state)

The photon's *classical* description (basis, state/bit, wavelength, seq, ts) goes in
the 0x7101 frame (§5.2). The receiver measures on arrival via SeQUeNCe's
`QSDetector`. **No shared state**; fully distributed; this is the BB84 path. Maps to
the existing QFabric model 1:1, so cross-validation is direct.

### 6.2 Shared Quantum State Service (entanglement: E91, swapping, repeaters)

Entangled states span nodes and can't be serialized as independent descriptors.
Provide a **central `QuantumStateService`** that owns one shared `QuantumManager`
(ket/density/bell-diagonal/stabilizer — SeQUeNCe's pluggable formalisms). Each runner
uses a **`RemoteQuantumManager`** proxy whose methods
(`new / set / get / run_circuit / add_loss / remove`) are RPCs to the service. A
photon/qubit crossing the wire then carries only its **integer state key + metadata**;
the amplitudes stay authoritative in the service, so entanglement math is globally
correct.

- Selection is per-`Timeline`: `RealTimeTimeline(quantum_manager=RemoteQuantumManager(addr))`
  for entanglement runs; the local in-process manager (or "none") for BB84.
- Trade-off: the service is a logical centralization (latency + a coordination point).
  Acceptable because it models *bookkeeping we physically cannot transmit*, not the
  classical control traffic — which still rides the real WAN. Document it as such.
- Future: shard/partition the manager by entanglement group to reduce centralization;
  out of scope for v0.1.

This pluggability is exactly the "Both" decision: **BB84 uses §6.1; Phase-4
entanglement protocols use §6.2** — same runner, different `quantum_manager`.

---

## 7. Wire codec — serializing SeQUeNCe messages

SeQUeNCe `Message` subclasses carry arbitrary per-protocol payloads (BB84:
`frequency / light_time / start_time / wavelength / bases / indices`; Cascade:
`key / checksums / pass_id / …`). We need a **registry-based codec**, not blind
pickling (pickle is brittle across envs and a security risk over a network).

```
WireCodec.register(BB84MsgType.BEGIN_PHOTON_PULSE,
                   encode=lambda m: {...}, decode=lambda d: BB84Message(...))
```

Envelope (JSON):
```json
{ "src": "alice", "receiver": "bob.BB84", "proto": "BB84",
  "msg_type": "BASIS_LIST", "priority": 12, "ts_ps": 123456,
  "payload": { "bases": [0,1,1,0, ...] } }
```

On decode, the listener reconstructs the concrete `Message` object and calls
`node.receive_message(src, msg)`, which routes by `msg.receiver` / `msg.protocol_type`
exactly as in-process (node.py:149). v0.1 codec covers **BB84MsgType** (4 types) and
**CascadeMsgType** (KEY, PARAMS, CHECKSUMS, SEND/RECEIVE_FOR_BINARY, GENERATE_KEY,
KEY_IS_VALID). New protocols register their types incrementally.

---

## 8. Node runner

`python -m qne_sequence.node_runner --config nodeA.yaml`

Responsibilities:
1. Build a `RealTimeTimeline(stop_time, time_scale, epoch, quantum_manager=…)`.
2. Instantiate **only this host's** SeQUeNCe node(s) (e.g. one `QKDNode`) + local
   components (LightSource, QSDetector) — standard SeQUeNCe construction.
3. For each channel whose peer is **local**, use the normal in-process
   `ClassicalChannel`/`QuantumChannel`. For each **remote** peer, install
   `RemoteClassicalChannel`/`RemoteQuantumChannel` pointed at the peer's host:port (or
   the photon interface / P4 path).
4. Pair protocol instances across hosts by name (the distributed analogue of
   `pair_bb84_protocols`) — see §8.1 for the hard part.
5. Start listeners, perform the epoch/start barrier, `timeline.init()`, `timeline.run()`.
6. Emit metrics (key bits, QBER, raw/secure key rate) in the existing QFabric metrics
   schema for drop-in comparison.

### 8.1 The `another` seam — SeQUeNCe's QKD reaches into the peer's memory

**This is the single highest correctness risk and must be resolved in Phase A.** An
audit of every `self.another` access (grep of `qkd/BB84.py` + `qkd/cascade.py`) shows
SeQUeNCe's QKD is written assuming **both protocol objects live in one address space**:
it does not merely use `another` to address messages — it directly **reads and mutates
the peer protocol's state**:

| Site | Access | Why it breaks across processes |
|---|---|---|
| `BB84.py:170,174,179` | Alice's `push()` writes **Bob's** `key_lengths`, `end_run_times`, `working` | peer-state write |
| `BB84.py:198–207,272,277` | resets/pops the **other side's** `basis_lists`, `bit_lists`, `key_bits` | peer-state write |
| `BB84.py:389–390` | `self.another.set_key()` then `self.another._pop(...)` (authors' own `# TODO: why access another node?`) | calls methods on the remote object |
| **`BB84.py:398`** | **`key_diff = self.key ^ self.another.key`** — reads the **peer's secret key** to compute QBER | reads remote secret; impossible & insecure on the wire |
| `cascade.py:357,588,608,615–617` | `++another.disclosed_bits_counter`, `another.end_cascade()`, `another.latency=…`, read `another.valid_keys` | peer-state read/write/call |

Legitimate (keep as-is): `another.owner.name` and `another.name` — pure addressing.

A naive "thin proxy" would **silently return wrong data** (e.g. `another.key` → `None`
→ bogus QBER) or crash. The recommended fix is two parts:

**(a) `GuardedRemoteStub` — prove the violation set is complete (Phase A gate).**
Assign this to `self.another`. Its `__getattr__` whitelists only `.owner.name` / `.name`
and raises `RemoteAccessError(attr, callsite)` on anything else. Run the in-process
BB84+Cascade test with the stub installed: every illegal access throws with a file:line.
This converts the static grep above into a *runtime proof* that we caught them all, and
guards against drift if the pinned SeQUeNCe version ever changes.

**(b) `DistributedBB84` / `DistributedCascade` — convert each poke to a message.**
Thin subclasses in `qne-sequence/` (SeQUeNCe stays **unforked**) overriding only the
flagged methods. The work list is bounded (~10 BB84 sites, ~6 Cascade):

| SeQUeNCe in-process cheat | Distributed replacement |
|---|---|
| `push()` writing `another.key_lengths/working` | a `SESSION_INIT` message; Bob sets *his own* buffers in `received_message`. |
| resetting `another.*` buffers | each side resets its own on the matching message. |
| `another.set_key()` / `another._pop()` | each side calls its **own** `set_key()/_pop()`, triggered by a message. |
| **`self.key ^ self.another.key` for QBER (398)** | **sample disclosure over the classical channel — which QFabric already implements** (`request_sample` → `alice_sample_bits`). Reuse it. |
| Cascade `disclosed_bits_counter`, `end_cascade()`, `valid_keys` | the corresponding Cascade message types (already in `CascadeMsgType`). |

**Reject the alternative — a transparent RPC proxy** that makes `another` a live remote
object: it would let unmodified BB84 "run," but line 398 would then **transmit the
secret key over the control channel** — exactly what QKD must never do. It models a
fiction, is chatty, and defeats the purpose. Never for a security protocol.

**Payoff:** line 398 is a simulation shortcut SeQUeNCe gets away with in one process.
Forcing it onto the wire as sample disclosure makes the *emulator more faithful than the
simulator* — precisely QFabric's research thesis.

---

## 9. Orchestration & topology

Extend SeQUeNCe's topology JSON with a **placement map**:

```json
{
  "nodes":     [ {"name": "alice", "type": "QKDNode", "host": "site1"},
                 {"name": "bob",   "type": "QKDNode", "host": "site2"} ],
  "qchannels": [ {"source": "alice", "destination": "bob", "distance": 1000,
                  "attenuation": 0.0002, "transport": "p4_0x7101"} ],
  "cchannels": [ {"source": "alice", "destination": "bob", "transport": "tcp",
                  "endpoint": "site2:5100"} ],
  "hosts":     { "site1": {"mgmt_ip": "...", "photon_if": "veth1"},
                 "site2": {"mgmt_ip": "...", "photon_if": "veth3", "tcp_port": 5100} },
  "time_scale": 1.0, "stop_time": 2000000000000, "formalism": "ket_vector"
}
```

The **splitter** partitions by `host`, generates a per-host runner config (local nodes
+ remote channel endpoints), and the launcher (extending `scripts/deploy_fabric.py`)
provisions the FABRIC slice, places the P4 switch on the quantum path, starts the
service (if entanglement), distributes `T0`, and launches runners. Single-host /
multi-process mode (loopback + veth + local BMv2) is the dev/CI target before FABRIC.

---

## 10. BB84-over-2-nodes — concrete end-to-end flow

Real SeQUeNCe `QKDNode`s on host A (Alice, role 0) and host B (Bob, role 1):

```
A: protocol_stack[0].push(length, key_num)        # app requests a key
A: start_protocol()
     └─ send_message(bob, BB84 BEGIN_PHOTON_PULSE)        ──TCP/JSON──►  B   (real WAN delay)
A: begin_photon_pulse()
     └─ LightSource.emit(states)  ── shim ──► photon TX (0x7101 frames) ─► [P4 loss] ─► B photon RX
B: (BEGIN arrives) set random measure bases; QSDetector fed by photon RX listener
B: end_photon_pulse()
     └─ send_message(alice, BB84 RECEIVED_QUBITS)         ──TCP/JSON──►  A
A: send_message(bob, BB84 BASIS_LIST {bases})            ──TCP/JSON──►  B
B: compare bases → matching_indices; collect key bits
     └─ send_message(alice, BB84 MATCHING_INDICES)        ──TCP/JSON──►  A
A: collect key bits; set_key(); _pop() → Cascade (optional)  …loop until key_num keys
```

Every arrow is **real traffic**: the four BB84 classical messages over TCP across the
WAN, the photon stream over the 0x7101 P4 path. SeQUeNCe owns all protocol logic;
QFabric owns the wire. Cascade error-correction (stack_size ≥ 2) rides the same
classical transport with its own registered message types.

---

## 11. Phased implementation plan

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **A. Transport + timeline skeleton + `another` seam** ✅ **DONE** | `RealTimeTimeline`, `RemoteClassicalChannel`/`RemoteQuantumChannel`, `WireCodec`, `Listener`/`Link`, `NodeRunner`, `GuardedRemoteStub`, `DistributedBB84`. Quantum channel = lossless descriptor stub. | ✅ Two runners on loopback produce an **identical key** (128/256/512-bit) with **zero `RemoteAccessError`s**; guard test confirms stock BB84 *does* violate. See `tests/test_two_node_bb84.py`. |
| **B. BB84 with realistic physics + QBER disclosure** ✅ **DONE** | Lossy descriptor-on-wire channel (`P(loss)=1-10^(-αL/10)`) + qfabric `Detector` (efficiency, dark counts, polarization error) at Bob; **sample-disclosure QBER** replacing `self.key ^ self.another.key`, reusing `qne.bb84.BB84Protocol`. | ✅ Key produced; **sampled QBER matches an in-process reference and analytical `(1-F)/2` within noise** (3-seed mean, tol 0.01); ideal physics → QBER 0 + identical keys; zero `RemoteAccessError`s. See `tests/test_phase_b_qber.py`. |
| **C1. Photon throughput strategies** ✅ **DONE** | `PhotonEmissionStrategy` with `BulkStream` + `PerPhotonEvent` (`--photon-mode`), `bench_throughput.py`. | ✅ Both modes produce correct keys/QBER; benchmark decided (below). See `tests/test_phase_c_throughput.py`. |
| **C2. Raw-socket / P4 fast path** 🟡 **code complete; runtime needs FABRIC** | `raw_photon.py`: `RawQuantumChannel` (TX) + `RawPhotonReceiver` (RX) emit/parse `0x7101` frames via `qne.photon.PhotonPacket` (same P4 program/table); `--quantum-transport raw`; cross-path race handled by a `--photon-drain-ms` window (P4 photon path vs TCP `QUBITS_DONE`). | Frame round-trip + import-safety unit-tested on macOS (`tests/test_phase_c2_raw_photon.py`). ⬜ Live veth+BMv2 run reproducing QFabric QBER/loss is the FABRIC test. *(AF_PACKET/veth/BMv2 unavailable on macOS.)* |
| **D. FABRIC 2-site** | Splitter + `deploy_fabric.py` integration; NTP/PTP epoch barrier; metrics in QFabric schema. | BB84 across two FABRIC sites; real WAN latency/jitter visible in time-to-key vs `netem` (reuse `06_network_effects`). |
| **E. Cross-validation** | Compare distributed-SeQUeNCe vs in-process SeQUeNCe vs live QFabric vs NetSquid. | Statistically-consistent QBER/key-rate (reuse Phase-3 agreement test). |
| **F. Entanglement (Phase-4 enabler)** | `QuantumStateService` + `RemoteQuantumManager`; E91 then entanglement swapping on a 3-node chain. | Heralded entanglement across hosts with correct fidelity under the shared manager. |

Phases A–C are local/CI; D onward needs a slice. Each phase is independently useful
and testable.

---

## 12. Failure modes, risks & open questions

| Risk | Mitigation |
|---|---|
| **Photon rate vs event loop** (§4.3) | Fast data-plane path for photons; `time_scale` for event-faithful small runs. Benchmark sustainable rate (ROADMAP Phase 1 backlog). |
| **Clock skew reorders cross-host events** | Stretch `time_scale` above clock error; PTP/PPS where available; add a small receiver-side `lookahead` slack so a slightly-late message still schedules in the future, not the past (`assert time <= event.time` in `Timeline.run()` would otherwise fire). |
| **Real net delay > modeled fiber delay** | Effective delay = `max(modeled, real)`; report both. This *is* the experiment for the WAN-effects study. |
| **`another` reads/writes remote protocol state** (§8.1) | Confirmed real, not hypothetical: ~10 BB84 + ~6 Cascade sites incl. reading the peer's secret key (BB84.py:398). Fix = `DistributedBB84/Cascade` message-converting subclasses + `GuardedRemoteStub` runtime gate. Highest-priority work in Phase A. |
| **Message codec drift vs SeQUeNCe version** | Registry codec pinned to `sequence==1.0.0` (already pinned in qfabric `pyproject.toml`); contract test per msg type. |
| **Determinism lost** | Real-time + real net = non-reproducible bit-for-bit (already true across QFabric's P4/Python RNGs). Keep seeded *logical* RNGs per node; report distributions, not single runs. |
| **Quantum State Service = centralization** | Only for entanglement runs; classical path stays on the WAN. Document as bookkeeping, not transport. Shard later. |
| **TCP head-of-line / reconnect** | Persistent connections with backoff (reuse `qne/channel.py` retry logic); sequence numbers already in the photon frame for the data path. |

**Open questions for next session:**
1. One SeQUeNCe node per process, or allow multiple co-located nodes per runner
   (matters for repeater chains in Phase F)?
2. RPC substrate for the Quantum State Service — plain TCP/JSON (consistent with the
   classical channel) vs gRPC (typed, faster)? Lean TCP/JSON for v0.1 consistency.
3. Do we need Cascade in the first FABRIC milestone, or is asymptotic Shor–Preskill
   key rate (already in QFabric) enough for Phase D?

---

## 13. Proposed file layout

```
qne-sequence/
  DESIGN.md                  ← this doc
  __init__.py
  rt_timeline.py             RealTimeTimeline (wall-clock event loop, thread-safe inject)
  remote_channel.py          RemoteClassicalChannel / RemoteQuantumChannel
  wire_codec.py              Message/Photon ↔ bytes; per-type registry
  listener.py                TCP + photon (0x7101) receive → event injection
  photon_path.py             PhotonEmissionStrategy: PerPhotonEvent | BulkStream (§4.3)
  distributed_qkd.py         DistributedBB84 / DistributedCascade (message-converted §8.1)
  guarded_stub.py            GuardedRemoteStub — runtime `another`-access gate (§8.1)
  node_runner.py             CLI: build local slice, wire channels, run
  qstate_service.py          QuantumStateService + RemoteQuantumManager (Phase F)
  topology.py                placement-aware topology + splitter
  configs/                   example 2-node BB84 configs (loopback, veth, FABRIC)
  tests/                     codec contract tests, 2-proc BB84 smoke test
```

Reuses unchanged: `qne/photon.py`, `qne/channel.py` (framing), `qne/detector.py`,
`p4/bmv2/*`, `scripts/deploy_fabric.py`. SeQUeNCe stays a pinned dependency
(`sequence==1.0.0`, Python 3.12 env per `scripts/setup_sequence_env.sh`) — **no fork**.
