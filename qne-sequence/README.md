# qne-sequence — distributed runtime for SeQUeNCe

*State as of 2026-09-21. Design record: [`DESIGN.md`](DESIGN.md) (June 2026 draft; the
"Phase" table there is historical — this file is the current source of truth).*

## What it is

A **distributed runtime for SeQUeNCe**: real SeQUeNCe node objects running as separate
processes on separate hosts, with SeQUeNCe's quantum and classical channels carrying
**actual frames on the wire** instead of in-memory event scheduling. SeQUeNCe stays an
unmodified, pinned dependency (`sequence==1.0.0`, Python 3.12).

What is real and what is modeled: the classical/herald traffic is real (raw `0x7102`
Ethernet through the BMv2 P4 switch, or TCP for dev); photons are `0x7101` frames dropped
statistically by the switch; entangled pairs are Werner states in a numpy register on one
node reached over RPC (`quantum_transport: entangled-state-service`). See `../docs/ASSUMPTIONS.md`.

## Why SeQUeNCe can't just be distributed — the three seams

| Blocker | Seam | Module |
|---|---|---|
| One global `Timeline` in virtual picoseconds — `run()` jumps `self.time`, no wall-clock relation | `RealTimeTimeline`: wall-clock-driven loop, thread-safe injection from listeners | `rt_timeline.py` |
| Channels move objects **by reference** into that heap | Subclassed channels that serialize and send on a socket / raw L2 | `remote_channel.py`, `l2_link.py`, `raw_photon.py` |
| No serialization; `quantum_state` is a local object / in-process manager key | Wire codec per type + `RemoteQuantumManager` proxy → `QuantumStateService` | `wire_codec.py`, `remote_qm.py`, `quantum_state_service.py` |

The receiver side is unchanged: a listener injects `node.receive_message(...)` /
`receive_qubit(...)` exactly as SeQUeNCe expects. Only *transmit* and the *timeline* are
intercepted.

**The correctness risk that shaped the design.** Stock `sequence.qkd.BB84` reaches into
`self.another` — the peer object's memory — in ~10 places, including reading the peer's
secret key; Cascade in ~6 more. `DistributedBB84` converts each access into a message and
`GuardedRemoteStub` raises `RemoteAccessError` on any that was not converted. A test asserts
stock BB84 *does* trip the guard; every run reports `remote_access_errors` (must be 0).

## What is implemented

| Area | Modules | Notes |
|---|---|---|
| BB84 over two processes | `distributed_qkd.py`, `node_runner.py` | sample-disclosure QBER, `qne.detector` physics at Bob, `bulk` / `per_event` photon modes, `--quantum-transport tcp|raw`, `--loss none|model|switch` |
| Raw-L2 classical channel | `l2_link.py` (`ReliableLink`) | EtherType `0x7102`, header `!2sBBQHHI` (magic, ver=2, type, seq, frag idx, frag count, payload len); per-fragment ack, timeout-resend, dedup, in-order delivery; truncated frames dropped and counted (`short_frames`) |
| Lookahead delivery | `listener.py`, `timesync.py` | frames fire at `t_send + delay` in shared sim time; `lookahead.certified` is True only with a nonzero modeled delay, ≥1 frame checked and none late (`None` when not applicable) |
| **Global timeline** | `time_authority.py`, `conservative_timeline.py`, `LogicalClock` in `remote_qm.py` | `--time-authority`: every message delivered at exactly `t_send + delay` in simulation time, so `late_events == 0` by construction at any wall-clock speed. **BB84's protocol phase** free-runs on an event queue and needs LBTS grants from the coordinator (`min(next_event + lookahead)` + in-flight message counting; result `timeline: authority`). **BB84 post-processing, E91 and the repeater chain** never free-run, so one shared per-node logical clock is the whole mechanism — no coordinator process, the endpoint is unused (result `timeline: logical`). Runs report `postprocess_sim_ps` / `sim_elapsed_ps`: reconciliation and herald latency as modeled quantities |
| Post-processing | `reconcile_link.py` → `qne.reconcile` | Cascade, **key-verification tag** (Toeplitz, `ceil(log2(2/ε_cor))` bits; mismatch → `KeyVerificationError`, no key, `verification_failed: true`), Toeplitz PA, `--finite-key` (TLGR constant) |
| Security depth | `qne.auth`, `distributed_qkd.py` | `--auth-key` HMAC + anti-replay on every link; `--basis-bias` (Z-only key, all X–X disclosed); `--eve-fraction` intercept-resend |
| Decoy source | `distributed_qkd.py` + `qne.decoy` | Poisson intensities, per-photon thinning **at the source**; TCP-descriptor transport only, `loss_where = none` — the P4 switch is bypassed; no PNS adversary |
| Entanglement | `qstate_core.py`, `quantum_state_service.py`, `remote_qm.py`, `e91.py`, `distributed_e91.py` | Werner weight `w` (the `fidelity` knob; results also report `bell_fidelity = (3w+1)/4`), QBER = (1−w)/2, CHSH S = 2√2·w; sift/QBER/Cascade/PA/finite-key over the real link |
| Repeater chains | `repeater.py`, `distributed_repeater.py`, `sequence_swap_poc.py` | BSM swap + heralded Pauli correction, K stations (K+2 processes), Werner-chain law F = (1+3wᴸ)/4; `qstate_sequence.py` = SeQUeNCe `QuantumManagerKet`-backed register |

Harnesses: `bench_throughput.py`, `sweep.py`, `plots.py`; they write `results/`, which is
scratch — regenerate rather than expecting it in a fresh checkout.
Tests: `tests/` (208, two-/three-/n-process loopback runs), run with `PYTHONPATH=.. pytest tests -q`.

## Running it

```bash
cd qne-sequence
python3.12 -m venv ../.venv-sequence && source ../.venv-sequence/bin/activate
pip install -e "..[dev,sequence]"
export PYTHONPATH=.:..        # qne_sequence + the qfabric root package (qne)

# BB84, two processes over loopback (Bob listens first)
COMMON="--distance-km 10 --attenuation 0.2 --fidelity 0.95 --efficiency 0.8 \
  --dark-count-rate 10 --num-pulses 30000 --sample-fraction 0.2 --key-length 128"
python -m qne_sequence.node_runner --role bob   --name bob   --peer alice --port 57123 --seed 2 $COMMON &
python -m qne_sequence.node_runner --role alice --name alice --peer bob   --port 57123 --seed 1 $COMMON

# E91 / BBM92 and repeater chains
python -m qne_sequence.node_runner --protocol e91 ...          # see notebooks/sequence/03
python -m qne_sequence.node_runner --protocol repeater --role alice|repeater|bob ...   # notebooks/concepts/03

# Distributed computing — spend the pairs on a gate instead of a key
python -m qne_sequence.node_runner --protocol dqc --dqc-primitive telegate ...  # notebooks/concepts/05
```

Each runner prints one JSON result line: `qber`, `sifted_bits`, `num_sampled`,
`secure_fraction`, `secure_key_bits`, `reconciled`, `verification_failed`, `finite_key`,
`short_frames`, `lookahead{on_time_events, late_events, max_lateness_ps, modeled_delay_ps,
applicable, certified}`, `remote_access_errors`, … A reconciled run reports only the
amplified secret as `key` (`null` when the accounting allows zero bits).

### Deployment modes — with or without the P4 switch

| Mode | flags | Loss applied by | Needs |
|---|---|---|---|
| ideal | `--loss none` | nobody | — |
| TCP, no switch | `--quantum-transport tcp` | software (`model`) | IP reachability |
| raw L2, no switch | `--quantum-transport raw --loss model` | software | root + a direct L2 link |
| **raw L2 + P4** (the FABRIC path) | `--quantum-transport raw --loss switch --classical-transport l2` | BMv2 P4 | the switch |

On FABRIC use `deploy_fabric.run_sequence_bb84 / run_sequence_e91 / run_sequence_repeater / run_sequence_dqc`
from `notebooks/sequence/01–03`; `deploy_fabric.setup_sequence_runtime` builds `.venv-qne`
on the nodes. Slice runs recorded 2026-08-04 used `classical_transport: l2` for both BB84 and
E91 (recorded to `../results/` by `deploy_fabric.py`; not tracked).

## Open items

1. **Raw-L2 classical backend for the repeater chain** (`--classical-transport l2` is
   refused there today; its K+1 links are TCP), and decoy on the raw `0x7101` path.
2. **Decoy on the raw path** (photon-count field in `0x7101`, per-photon thinning in P4)
   and a PNS Eve.
3. **NetSquid reference on the slice** (`.venv-nsq` needs netsquid.org credentials via
   `setup_sim_envs`) for the `sequence/02` distance comparison.
4. Multi-site repeater topologies (one station per site) and the herald-latency dataset.
5. Cascade BINARY is one RPC round-trip per block (dominates time-to-key at WAN RTT);
   batch per level.
