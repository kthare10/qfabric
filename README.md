# QFabric: Quantum Network Emulation Platform on FABRIC

QFabric is a programmable quantum network emulation platform built on the [FABRIC testbed](https://fabric-testbed.net). It emulates quantum channels using P4 programmable switches and runs BB84 QKD as its first protocol, with cross-validation against the SeQUeNCe and NetSquid simulators.

The core idea: pure quantum-network simulators (NetSquid, SeQUeNCe) assume an *ideal* classical control channel. QFabric runs the classical sifting traffic over a **real WAN** on FABRIC, so genuine latency, jitter, and congestion enter the protocol naturally — letting us measure how classical-network conditions affect quantum-protocol performance.

See [`SPEC.md`](SPEC.md) for the design and wire formats, and [`ROADMAP.md`](ROADMAP.md) for what's done and what's next.

## Architecture

```
Alice (Python)  →  BMv2 P4 Switch  →  Bob (Python)
  photon source      fiber loss         detector model
  raw socket         probabilistic      raw socket
                     packet drop        + BB84 sifting
                                        via TCP
```

- **P4 switch**: Implements fiber attenuation as probabilistic packet drop using custom EtherType `0x7101` photon frames. Classical traffic is L2-forwarded separately.
- **Python QNE**: Alice (photon source) and Bob (detector model with efficiency, dark counts, random-basis measurement).
- **Classical channel**: Standard TCP for BB84 sifting — real WAN effects enter naturally on FABRIC.

## Components

| Directory | Description |
|-----------|-------------|
| `qne/` | Python quantum node emulator — Alice, Bob, BB84 post-processing, detector, photon wire format, classical channel, metrics, CLI |
| `p4/bmv2/` | BMv2 V1Model P4 quantum-channel program (loss model + L2 forwarding) |
| `validation/` | Cross-validation framework — runs the same scenario on QFabric, SeQUeNCe, and NetSquid and checks statistical agreement |
| `scripts/` | `deploy_fabric.py` (full FABRIC slice provisioning + run), `install_bmv2.sh`, `package_artifact.sh`, and the cross-validation env setup scripts |
| `notebooks/` | Linear workflow: `00_overview` → `01_setup_slice` → `02_run_experiment` → `03_cross_validation` → `04_analysis` → `05_run_all_scenarios` |
| `kiso/` | Kiso experiment config for FABRIC runs |
| `docker/` | `Dockerfile.bmv2` for a reproducible BMv2 toolchain |
| `tests/` | Unit tests for BB84, detector, photon, metrics, and cross-validation |

## Quick Start

### Notebook workflow — run in order

The notebooks form a single linear workflow. Start at `00_overview`:

| # | Notebook | What it does | Where it runs |
|---|----------|--------------|---------------|
| 0 | `00_overview` | Orientation + environment check | Anywhere |
| 1 | `01_setup_slice` | Provision the FABRIC slice, install BMv2, compile P4, start the switch | FABRIC JupyterHub |
| 2 | `02_run_experiment` | Run BB84 across the slice, collect results, verify | FABRIC JupyterHub |
| 3 | `03_cross_validation` | Compare QFabric vs SeQUeNCe & NetSquid (one scenario, on the slice) | FABRIC JupyterHub |
| 4 | `04_analysis` | Load results and generate all plots & tables | Anywhere (ships sample results) |
| 5 | `05_run_all_scenarios` | Run **every** scenario (singles + sweeps) on the slice + QBER/key-rate sweep figures | FABRIC JupyterHub |

> Notebooks 1–2 provision and drive a FABRIC slice (BMv2 is installed on the switch node automatically). Notebooks 0, 3, 4 are pure-Python and run anywhere (including JupyterHub) — 4 works standalone on the bundled sample results.

### Prerequisites

- Python 3.11 (for the cross-validation env — see below)
- A FABRIC account + project + tokens configured in JupyterHub (for notebooks 1–2)
- BMv2/p4c are installed automatically on the slice's switch node by `scripts/install_bmv2.sh` — no local install needed

### Install

```bash
cd qfabric
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional extras: `.[dev]`, `.[fabric]`. The cross-validation simulators are **not** installed here — they live in their own per-node/per-version envs (see "Cross-Validation" below): SeQUeNCe 1.0 needs Python 3.12, NetSquid needs 3.10/3.11.

### Run Unit Tests

```bash
pytest tests/ -v
```

### Cross-Validation — on the FABRIC nodes (notebook 03)

The cross-validation compares four BB84 results for the same scenario, **all executed on the FABRIC slice**:

| Backend | Runs on | What it is |
|---------|---------|------------|
| QFabric (measured) | BMv2 data plane (notebook 02) | the real emulation over FABRIC |
| QFabric-sim | switch node (`.venv-qsim`) | pure-Python model, no traffic |
| SeQUeNCe | alice node (`.venv-seq`, Python 3.12) | SeQUeNCe 1.0 native engine |
| NetSquid | bob node (`.venv-nsq`) | NetSquid native engine |

SeQUeNCe and NetSquid each drive their **own** engine, so the comparison is genuine independent physics — not a re-run of QFabric's code. Because SeQUeNCe 1.0 needs Python ≥3.12 and NetSquid needs 3.10/3.11 (they can't share an interpreter), they live on different nodes in their own venvs. `deploy_fabric.setup_sim_envs()` builds them (SeQUeNCe via the deadsnakes Python 3.12; NetSquid needs your netsquid.org credentials in `NETSQUID_USER`/`NETSQUID_PASS`), and `run_cross_validation_on_fabric()` runs each adapter on its node and collects the results. Notebook `03_cross_validation` drives both. Unavailable/failed backends are reported **SKIPPED** (never a false pass).

> Each on-node adapter is just `python -m validation.run_<backend> scenario.yml --json -`. The same adapters can also run locally (in JupyterHub or a laptop) — `validation.compare` runs a backend in-process if importable, or in a separate interpreter set via `QFABRIC_SEQUENCE_PYTHON` / `QFABRIC_NETSQUID_PYTHON`. The `scripts/setup_sequence_env.sh` / `setup_netsquid_env.sh` helpers build those local venvs.

### FABRIC Deployment

```bash
python scripts/deploy_fabric.py --scenario validation/scenarios/fabric_1km.yml
python scripts/deploy_fabric.py --cleanup    # tear down the slice
```

Provisions a 3-node slice (Alice / switch / Bob), installs BMv2, compiles the P4 program, and runs BB84 end-to-end. Results land in `results/`.

## Key Parameters

The fiber loss model is `P(loss) = 1 − 10^(−α·L/10)`, where `α` is attenuation (dB/km) and `L` is distance (km). The P4 switch compares a per-packet 32-bit random number against `floor(P(loss) · 2³²)`.

| Parameter | Example (1 km, α=0.2) | Example (50 km) |
|-----------|----------------------|-----------------|
| Fiber loss probability | ~4.5% | ~90% |
| P4 threshold (u32) | ~193 M | ~3.87 B |
| Expected sift rate | ~50% of detected | ~50% of detected |
| Intrinsic QBER | ≈ (1−F)/2 (e.g. ~1% at F=0.98) | ≈ (1−F)/2 |

Scenarios are defined in YAML under `validation/scenarios/`. Both single-run and `sweep`-style files are supported.

## License

Licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE).

© 2026 Komal Thareja (kthare10@renci.org)
