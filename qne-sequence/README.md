# qne-sequence — distributed runtime for SeQUeNCe (Phases A–C1)

Run real **SeQUeNCe** `QKDNode` / BB84 instances as separate processes that exchange
BB84 control messages as **real traffic on the wire**, on a wall-clock-driven
timeline. See [`DESIGN.md`](DESIGN.md) for the full architecture; this README covers
what Phase A delivers and how to run it.

## What Phase A delivers

| Module | Role |
|---|---|
| `qne_sequence/rt_timeline.py` | `RealTimeTimeline` — wall-clock event loop with thread-safe event injection from network listeners (DESIGN §4). |
| `qne_sequence/wire_codec.py` | `WireCodec` / `WireMessage` — JSON envelope for SeQUeNCe traffic, no pickle (DESIGN §7). |
| `qne_sequence/listener.py` | `Link` (framed TCP) + `Listener` (decode → inject event) (DESIGN §5). |
| `qne_sequence/remote_channel.py` | `RemoteClassicalChannel` / `RemoteQuantumChannel` — transmit over a socket instead of the local heap. |
| `qne_sequence/guarded_stub.py` | `GuardedRemoteStub` — runtime gate proving every `another` access is converted (DESIGN §8.1). |
| `qne_sequence/distributed_qkd.py` | `DistributedBB84` — BB84 with all ~10 peer-state pokes turned into messages. |
| `qne_sequence/node_runner.py` | CLI that builds one node, wires remote channels, runs BB84. |

**Phase A exit (met):** two runners on loopback exchange BB84 control messages and
produce an **identical key**, with **zero `RemoteAccessError`s** — no code path reaches
into the peer protocol's memory.

**Phase B exit (met):** realistic physics on the descriptor-on-wire channel — fiber
loss `P=1-10^(-αL/10)` + qfabric's `Detector` (efficiency, dark counts, polarization
error) at Bob — and **QBER estimated by sample disclosure** over the classical channel
(no peer-secret-key read). The sampled QBER matches an in-process reference and the
analytical `(1-F)/2` within statistical noise. Reuses `qne.detector` / `qne.bb84` so
numbers align with qfabric's cross-validated model.

### Run with realistic physics (Phase B)

```bash
COMMON="--distance-km 10 --attenuation 0.2 --fidelity 0.95 --efficiency 0.8 \
  --dark-count-rate 10 --num-pulses 30000 --sample-fraction 0.2 --key-length 128"
$PY -m qne_sequence.node_runner --role bob   --name bob   --peer alice --port 57123 --seed 2 $COMMON &
$PY -m qne_sequence.node_runner --role alice --name alice --peer bob   --port 57123 --seed 1 $COMMON
# alice result includes: qber, sifted_bits, num_sampled, secure_fraction, final_key_bits, loss_probability
```

## Environment

Requires Python ≥ 3.12 with `sequence==1.0.0` plus the qfabric root package on the path:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install 'sequence==1.0.0' numpy
```

The package targets the **pinned `sequence==1.0.0`** (qfabric's dependency), whose
`QKDNode.receive_message` routes by a truthy `protocol_type` — `DistributedBB84` sets
one. (The newer SeQUeNCe `master` checkout in `../../SeQUeNCe` routes by `msg.receiver`;
both are handled.)

## Run it

```bash
cd qne-sequence
export PYTHONPATH=.:..   # qne_sequence + the qfabric root package (qne)
PY=$(command -v python)

# Bob first (it listens), then Alice (it connects)
$PY -m qne_sequence.node_runner --role bob   --name bob   --peer alice --port 57123 --key-length 128 --seed 2 &
$PY -m qne_sequence.node_runner --role alice --name alice --peer bob   --port 57123 --key-length 128 --seed 1
```

Each prints one JSON result line, e.g.:

```json
{"role": 0, "name": "alice", "key": 8244..., "key_bits": 128,
 "tx_frames": 4, "rx_frames": 2, "remote_access_errors": 0}
```

## Test

```bash
cd qne-sequence
python -m pytest tests/ -v
```

`tests/test_two_node_bb84.py` (Phase A):
- `test_two_node_bb84_over_loopback` — two subprocesses, identical key, zero remote-access errors, real frames both directions.
- `test_guard_catches_stock_bb84_violation` — the guard genuinely fires on *stock* `sequence.qkd.BB84` (the §8.1 violation is real, not hypothetical).

`tests/test_phase_b_qber.py` (Phase B):
- `test_qber_matches_reference_within_noise` — distributed sampled QBER ≈ in-process reference ≈ analytical `(1-F)/2`; keys produced; secure fraction in (0, 1]; zero remote-access errors.
- `test_lossless_perfect_channel_qber_is_zero` — ideal physics → QBER 0 and identical keys.

`tests/test_phase_c_throughput.py` (Phase C1):
- `test_mode_produces_correct_key[bulk|per_event]` — both photon strategies produce correct keys/QBER, zero remote-access errors.
- `test_bulk_outperforms_per_event` — BulkStream throughput > 1.5× PerPhotonEvent.

### Throughput modes (Phase C1)

`--photon-mode bulk` (default) ships the pulse train in one frame; `--photon-mode
per_event` sends one Event + one frame per photon (max per-photon fidelity). Benchmark:

```bash
$PY bench_throughput.py 2000 20000 100000
```

Measured over loopback (macOS): `per_event` plateaus ~133k photons/s; `bulk` scales to
~571k/s. Default `bulk`; use `per_event` for per-photon-timing studies. Neither pure-
Python mode hits ~1 MHz over loopback — the raw-socket/0x7101/BMv2 fast path (Phase C2,
needs Linux/FABRIC) is required for that, and the strategy/channel interface is already
isolated for the swap.

## Scenario sweeps + visualizations

Run the emulator across a scenario matrix and capture results + figures:

```bash
cd qne-sequence
export PYTHONPATH=.:..
PY=$(command -v python)
$PY sweep.py                 # full matrix, 5 reps -> results/sequence_scenarios.json
$PY plots.py                 # -> results/figures/seq_*.png (9 figures)
$PY sweep.py --quick         # fast smoke matrix
$PY sweep.py --reps 10       # more repetitions -> tighter error bars
```

- `sweep.py` — slice-free loopback sweep (same model as the FABRIC raw/P4 path); one
  axis at a time over **every behavioral knob**, with **repetitions** (mean ± std).
  Axes: `fidelity`, `distance`, `attenuation`, `efficiency`, `dark_count`,
  `sample_fraction`, `key_length`, `throughput` (pulse count × `bulk`/`per_event`).
- `plots.py` — error-barred figures: `fig_qber_vs_fidelity` (vs analytical `(1-F)/2`),
  `fig_secure_fraction` (Shor-Preskill cutoff), `fig_distance`, `fig_attenuation`,
  `fig_efficiency`, `fig_dark_count`, `fig_sample_fraction`, `fig_key_length`,
  `fig_throughput`, and `fig_network_effects` (FABRIC).
- **Notebook `../notebooks/08_sequence_scenarios.ipynb`** — local sweep + all figures
  inline (Section A), then on-testbed distance + classical-network-effects sweeps (Section B).

**Coverage / limits:** not swept here — `quantum_transport=raw` (real P4 photons; see
notebook 08 §B), `time_scale`/`channel_delay` (wall-clock pacing only, not QBER/key
yield), and `key_num` (>1 key per request is **not supported** by `DistributedBB84`).

## Deployment modes — with or without the P4 switch

The P4 switch's only job is applying the **fiber-loss drop in the data plane**. That's
orthogonal to *how photons cross the wire*, so loss location is a separate choice
(`--loss`) from transport (`--quantum-transport`). Three practical modes:

| Mode | `--quantum-transport` / `--loss` | Loss by | Needs | Use |
|---|---|---|---|---|
| **Lossless / ideal** | `--loss none` | nobody | — | ideal channel — QBER from fidelity only, no photon drop (any transport) |
| **TCP (no switch)** | `tcp` (loss `auto`→model) | software | just IP reachability | dev/loopback **or 2 hosts over WAN** — no switch, no root |
| **Raw direct (no switch)** | `raw --loss model` | software | raw socket (root) + a direct L2 link | real `0x7101` L2 photons, **no programmable switch** |
| **Raw + P4** | `raw --loss switch` (raw `auto`→switch) | BMv2 P4 | the P4 switch | hardware/data-plane loss (the FABRIC path) |

`--loss none` gives a **lossless channel** regardless of `--distance-km`/`--attenuation`
(combine with `--fidelity 1 --efficiency 1 --dark-count-rate 0` — the defaults — for a
fully ideal run). The `node_runner` defaults are already ideal; setting `--distance-km 0`
or `--attenuation 0` also yields zero loss. `--loss auto` (default) keeps the
conventional choice per transport, so existing runs are unchanged. The simplest
switch-free path is **`tcp`** (no root, no raw sockets). Each result reports
`quantum_transport` and `loss_where`.

```bash
# lossless / ideal — no loss even with a distance set, QBER 0 at F=1
$PY -m qne_sequence.node_runner --role bob   --name bob   --peer alice --host 0.0.0.0  --port 5100 --loss none &
$PY -m qne_sequence.node_runner --role alice --name alice --peer bob   --host 127.0.0.1 --port 5100 --loss none
```

```bash
# no P4 at all — descriptors over TCP, software loss (works loopback or host-to-host)
$PY -m qne_sequence.node_runner --role bob   --name bob   --peer alice --host 0.0.0.0     --port 5100 --distance-km 10 --attenuation 0.2 --fidelity 0.95 &
$PY -m qne_sequence.node_runner --role alice --name alice --peer bob   --host <bob-ip>    --port 5100 --distance-km 10 --attenuation 0.2 --fidelity 0.95
# real L2 0x7101 photons, software loss, still no switch (needs root + a direct link):
#   ... --quantum-transport raw --loss model --photon-iface <if>
```

On FABRIC, `deploy_fabric.run_sequence_bb84(slice_obj, transport='tcp')` runs switch-free
(a 2-node slice with a direct L2 link suffices — no `switch` node needed); the default
`transport='raw'` uses the P4 switch.

## Running on FABRIC

**Recommended:** use the notebook **`../notebooks/07_sequence_emulator.ipynb`** on FABRIC
JupyterHub. It reuses the slice/switch/data-plane from notebook 1, builds a `.venv-qne`
(sequence 1.0.0) on both nodes via `deploy_fabric.setup_sequence_runtime`, arms the P4
loss model, and runs `deploy_fabric.run_sequence_bb84` (raw 0x7101 photons + TCP
sifting). The manual commands below are what that notebook runs under the hood.

### Raw 0x7101 photons through the P4 switch (Phase C2)

The classical control plane stays TCP (real WAN); the quantum plane becomes real
`0x7101` Ethernet frames on a photon interface, forwarded through the **existing BMv2 P4
switch** (`../p4/bmv2/`) that applies fiber loss as a probabilistic drop. The frame
format is qfabric's `qne.photon.PhotonPacket`, so the same P4 program and per-wavelength
loss table apply unchanged — install the loss-table entries exactly as for the current
qfabric BB84 run (see `../SPEC.md`).

```bash
# Bob (host B / FABRIC site 2) — listens for classical TCP + raw photons on veth3
$PY -m qne_sequence.node_runner --role bob --name bob --peer alice \
    --host 0.0.0.0 --port 5100 \
    --quantum-transport raw --photon-iface veth3 \
    --fidelity 0.95 --efficiency 0.8 --dark-count-rate 10 \
    --num-pulses 100000 --key-length 256 --photon-drain-ms 500

# Alice (host A / FABRIC site 1) — connects to Bob, emits photons on veth1
$PY -m qne_sequence.node_runner --role alice --name alice --peer bob \
    --host <bob-data-plane-ip> --port 5100 \
    --quantum-transport raw --photon-iface veth1 \
    --src-mac 02:00:00:00:00:01 --dst-mac 02:00:00:00:00:02 \
    --num-pulses 100000 --key-length 256
```

Notes for the slice:
- `--quantum-transport raw` needs `AF_PACKET` (Linux) and the photon interface up;
  loss is the **P4 switch's** job (not applied in `RawQuantumChannel`), so set the
  loss-table threshold from `distance/attenuation` as today.
- `--photon-drain-ms` covers the race between the photon path (through the switch) and
  the TCP `QUBITS_DONE` marker; raise it if Bob's sifted count looks short (stragglers
  still in flight). `tcp` mode needs no drain (single ordered link).
- MACs default to qfabric's `02:..:01` (Alice) → `02:..:02` (Bob); match the P4 table.

## Current simplifications (see DESIGN §11 for the path forward)

- Quantum channel is **descriptor-on-wire over TCP** with a probabilistic loss model —
  no real photons / P4 yet. Phase C swaps in the raw-socket / `0x7101` / BMv2 fast path.
- `key_num == 1` per request (no multi-key buffering).
- **No error correction**: with QBER > 0, Alice's and Bob's keys differ on the error
  positions by design. Cascade (`DistributedCascade`) is a later phase; the secure-key
  estimate is the asymptotic Shor–Preskill bound from `qne.bb84`.
- Both classical and quantum frames share one TCP `Link`, tagged by `kind`.
