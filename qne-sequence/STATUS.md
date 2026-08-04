# qne-sequence — implementation state

*Snapshot: 2026-08-04. Source of truth for the design is [`DESIGN.md`](DESIGN.md); this is a
one-page read of where the code actually is. Note that [`README.md`](README.md) lags the code —
it still says "Phases A–C1" while E91, repeater chains, decoy, Eve, auth and finite-key are all
built and tested.*

---

## What it is

A **distributed runtime for SeQUeNCe**: real SeQUeNCe node objects running as separate
processes on separate hosts (FABRIC sites), with SeQUeNCe's quantum and classical channels
carrying **actual traffic on the wire** instead of in-memory event scheduling.

The point: instead of hand-reimplementing each quantum protocol (which `qne/` does), run
SeQUeNCe's peer-reviewed protocol stack — BB84, Cascade, entanglement generation /
purification / swapping, network & resource management — over real links, so **real WAN
latency, jitter and congestion feed back into the quantum protocol naturally.**

**Size:** ~7,000 LoC across 22 modules in `qne_sequence/`, plus 20 test files.
SeQUeNCe itself stays an **unmodified dependency** (pinned `sequence==1.0.0`).

---

## Why SeQUeNCe can't just be distributed — and the three seams

SeQUeNCe is a single-process discrete-event simulator. Three properties block distribution;
each maps to one integration seam:

| Blocker | Seam | Module |
|---|---|---|
| One global `Timeline` in **virtual picoseconds** — `run()` jumps `self.time` instantly, no relation to wall-clock | `RealTimeTimeline` — wall-clock-driven event loop with thread-safe event injection from network listeners | `rt_timeline.py` |
| Channels move objects **by reference** into that one heap (`transmit()` schedules a local `Event` holding a live Python object) | Subclassed channels that **serialize and send on a socket** when the peer is on another host | `remote_channel.py`, `l2_link.py`, `raw_photon.py` |
| **No serialization anywhere** — `Message`/`Photon` passed by reference; `quantum_state` is a local object or an int key into an in-process manager | Wire codec per type, plus a `RemoteQuantumManager` proxy for entanglement | `wire_codec.py`, `remote_qm.py`, `quantum_state_service.py` |

**The receiver side is unchanged.** Once a frame arrives, the listener injects a local event
calling `node.receive_message(...)` / `node.receive_qubit(...)` exactly as SeQUeNCe expects.
Only *transmit* and the *timeline* are intercepted — BB84, Cascade, detectors, memories,
resource/network managers run untouched.

### The correctness risk that drove Phase A

Stock `sequence.qkd.BB84` reaches into **`self.another`** — the peer protocol object's memory —
in ~10 places, including **reading the peer's secret key** (`BB84.py:398`). Cascade does it in
~6 more. In a distributed run that object doesn't exist locally, so every access is a silent
correctness bug.

Fix: `DistributedBB84` converts each access into an explicit message, and
**`GuardedRemoteStub`** (`guarded_stub.py`) is a runtime gate that raises `RemoteAccessError`
on any access that wasn't converted. A test asserts that stock BB84 *does* trip the guard —
proving the violation set is real, not hypothetical — and every phase exit criterion includes
**zero `RemoteAccessError`s**.

---

## Phase status

| Phase | Deliverable | State |
|---|---|---|
| **A** | Transport + timeline skeleton + `another` seam: `RealTimeTimeline`, remote channels, `WireCodec`, `Listener`/`Link`, `NodeRunner`, `GuardedRemoteStub`, `DistributedBB84` | ✅ **Done** — two runners on loopback produce an **identical key** (128/256/512-bit), zero remote-access errors; guard test confirms stock BB84 violates |
| **B** | BB84 with realistic physics + QBER by **sample disclosure** (replacing `self.key ^ self.another.key`): fiber loss `P = 1 − 10^(−αL/10)` + qfabric `Detector` (efficiency, dark counts, polarization error) at Bob | ✅ **Done** — sampled QBER matches an in-process reference *and* analytical `(1−F)/2` within noise (3-seed mean, tol 0.01); ideal physics → QBER 0 |
| **C1** | Photon throughput strategies: `BulkStream` vs `PerPhotonEvent` (`--photon-mode`), `bench_throughput.py` | ✅ **Done** — both modes correct; benchmark recorded. Loopback TCP falls short of MHz rates, which is what motivated C2 |
| **C2** | Raw-socket / P4 fast path: `RawQuantumChannel` + `RawPhotonReceiver` emitting/parsing **EtherType 0x7101** frames via `qne.photon.PhotonPacket` (same P4 program/table); `--quantum-transport raw`; cross-path race handled by a `--photon-drain-ms` window | 🟡 **Code complete, unit-tested** — frame round-trip + import safety pass on macOS. Live veth+BMv2 run is the remaining test (no AF_PACKET/BMv2 on macOS) |
| **D** | FABRIC 2-site: topology splitter + `deploy_fabric.py` integration, epoch barrier, metrics in QFabric schema | ✅ **Done in practice** — `results/sequence_scenarios_fabric.json` |
| **E** | Cross-validation: distributed-SeQUeNCe vs in-process SeQUeNCe vs live QFabric vs NetSquid | 🟡 **Partial** — `results/reference_netsquid_distance.json` exists; NetSquid re-run **blocked** (below) |
| **F** | Entanglement (Phase-4 enabler): `QuantumStateService` + `RemoteQuantumManager`, E91, then swapping on a chain | ✅ **Built** — `quantum_state_service.py`, `remote_qm.py`, `qstate_core.py`/`qstate_sequence.py`, `distributed_e91.py`, `e91.py`, `repeater.py`, `distributed_repeater.py`, `sequence_swap_poc.py`. n-node repeater chains validated on real FABRIC WAN |

### Beyond the original phase plan

Also implemented, with tests, though not in the `DESIGN.md` table:

- **Decoy states** over the distributed link — `test_two_node_decoy.py`
- **Eve / eavesdropper** — `test_two_node_eve.py`
- **Finite-key analysis** on the link — `test_finite_key_link.py`, `test_two_node_auth_finite.py`
- **Authenticated classical channel** (HMAC-SHA256 + anti-replay) — `test_link_auth.py`
- **Biased basis choice** — `test_two_node_biased.py`
- **Cascade reconciliation** — `reconcile_link.py`, `test_two_node_bb84_cascade.py`
- **L2 / raw-Ethernet classical backend** — `l2_link.py`, `test_l2_link.py`
- **Lookahead fidelity program** — `test_lookahead.py`
- **Time sync** — `timesync.py`

---

## Module map

| Module | Role |
|---|---|
| `rt_timeline.py` | `RealTimeTimeline` — wall-clock event loop, thread-safe injection |
| `wire_codec.py` | JSON envelope for SeQUeNCe traffic (no pickle) |
| `listener.py` | `Link` (framed TCP) + `Listener` (decode → inject event) |
| `l2_link.py` | Raw-Ethernet classical backend (EtherType 0x7102) |
| `remote_channel.py` | `RemoteClassicalChannel` / `RemoteQuantumChannel` |
| `raw_photon.py` | 0x7101 photon frames for the P4 data-plane path |
| `guarded_stub.py` | `GuardedRemoteStub` — the `another`-access runtime gate |
| `distributed_qkd.py` | `DistributedBB84` — all peer-state pokes turned into messages |
| `reconcile_link.py` | Cascade error reconciliation over the link |
| `node_runner.py` | CLI: build this host's node, wire remote channels, run |
| `quantum_state_service.py`, `remote_qm.py` | Shared quantum state for entanglement protocols |
| `qstate_core.py`, `qstate_sequence.py` | State backends (own core, SeQUeNCe-backed) |
| `e91.py`, `distributed_e91.py` | E91 / BBM92 on the state service |
| `repeater.py`, `distributed_repeater.py`, `sequence_swap_poc.py` | Entanglement swapping, n-node chains |
| `photon_path.py`, `timesync.py` | Photon path model, epoch/clock alignment |

Harnesses: `bench_throughput.py`, `sweep.py`, `plots.py`; outputs in `results/` (+ `results/figures/`).

---

## Open items

1. **NetSquid cross-validation blocked.** `.venv-nsq` is gone from the slice — needs
   `setup_sim_envs` with netsquid.org credentials before the B.1 → B.2 comparison can re-run.
2. **C2 live run.** The raw/P4 photon path needs a veth + BMv2 run on the slice reproducing
   QFabric's QBER/loss curve; it's only unit-tested today.
3. **Large uncommitted body** on top of `4b9f4c7`: notebook reorganization, raw-L2 emulation
   notebooks (07/08/09), E91 L2 backend, `ReliableLink` Ethernet-padding fix (`_VERSION=2`),
   P4 loss-freeze fix. Nothing committed yet.
4. **`README.md` is stale** — claims Phases A–C1 only.

---

## Running it

```bash
cd qne-sequence
export PYTHONPATH=.:..        # qne_sequence + the qfabric root package (qne)

# Bob listens first, then Alice connects
COMMON="--distance-km 10 --attenuation 0.2 --fidelity 0.95 --efficiency 0.8 \
  --dark-count-rate 10 --num-pulses 30000 --sample-fraction 0.2 --key-length 128"
python -m qne_sequence.node_runner --role bob   --name bob   --peer alice --port 57123 --seed 2 $COMMON &
python -m qne_sequence.node_runner --role alice --name alice --peer bob   --port 57123 --seed 1 $COMMON

python -m pytest tests/ -v
```

Each runner prints one JSON result line (`qber`, `sifted_bits`, `num_sampled`,
`secure_fraction`, `final_key_bits`, `loss_probability`, `remote_access_errors`, …).
