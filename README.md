# QFabric: Quantum Network Emulation Platform on FABRIC

QFabric is a programmable quantum network emulation platform built on the [FABRIC testbed](https://fabric-testbed.net). It runs quantum-network protocols — BB84 QKD (with decoy-state accounting), entanglement-based QKD (E91/BBM92), entanglement-swapping repeater chains, and distributed quantum *computing* (teleportation and the non-local CNOT between two QPUs) — as **real distributed systems** on testbed nodes, with the *fiber* emulated in a P4/BMv2 data plane and cross-validated against the SeQUeNCe and NetSquid simulators.

**What is emulated and what is real (read `docs/ASSUMPTIONS.md` before quoting any number):**

| | Quantum channel | Classical channel | Quantum state |
|---|---|---|---|
| Carries | photon descriptors (`0x7101` frames) | sifting, QBER sample, Cascade parities, heralds (`0x7102` frames) | Bell pairs for E91 / swapping |
| Implemented as | P4 switch drops each frame with the Beer–Lambert fiber-loss probability | raw L2 through the **same** switch, reliable-datagram shim; TCP as a dev fallback | numpy Werner-state register on one node, reached by RPC |
| Real? | statistical model of loss + depolarizing noise; **no photons, no quantum states on the wire** | real frames on real links | **no physical entanglement**; a correctness/distribution check, not a Bell test |

The design bet: *the switch is the fiber*, carrying both "wavelengths". Slices are **single-site** by default — a photon cannot cross a WAN — and one distance knob (`--distance-km` / `--channel-delay auto`) drives both fiber loss and classical propagation delay (~5 µs/km). Cross-site slices and netem impairments are **stress studies**, not operating conditions: a real QKD classical channel has no loss and ~zero delay (SeQUeNCe-team feedback, 2026-07; see `docs/ASSUMPTIONS.md`).

All documentation lives in [`docs/`](docs/). New to quantum networking? Start with [`docs/PRIMER.md`](docs/PRIMER.md) (concepts from zero, no code), then [`docs/CONCEPTS.md`](docs/CONCEPTS.md) (concept → code map). [`docs/SPEC.md`](docs/SPEC.md) has the wire formats and protocol messages, [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) the modeling assumptions, [`docs/ROADMAP.md`](docs/ROADMAP.md) status and open items, and [`docs/reviews/2026-09-21.md`](docs/reviews/2026-09-21.md) the latest code review.

Repository: <https://github.com/kthare10/qfabric>

## Architecture

```
 Alice ──0x7101 photon frames──►  BMv2 P4 switch  ──►  Bob
        ◄──0x7102 classical────►  (loss table,       (detector model:
            reliable-datagram        classical fwd,    efficiency, dark counts,
            shim, HMAC optional)     counters)         dead time, jitter, F)

 sift → QBER sample → Cascade → key-verification tag → Toeplitz PA → identical secret
 accounting: asymptotic 1−2h(Q) | efficient 1−h(e_z)−h(e_x) | finite-key (TLGR) | decoy (GLLP)
```

Two implementations share the physics and post-processing code (`qne/bb84.py`, `qne/detector.py`, `qne/reconcile.py`, `qne/finite_key.py`, `qne/decoy.py`, `qne/auth.py`):

- **`qne/`** — the hand-coded raw-socket BB84 path: `qfabric alice` / `qfabric bob` CLIs, photons as `0x7101` frames through the switch, classical post-processing over TCP. This is the measured data point in the simulator cross-validation.
- **`qne-sequence/`** — the distributed runtime built on SeQUeNCe's timeline: BB84 (incl. decoy source, biased bases, Eve), E91/BBM92 over a shared quantum-state service, n-node repeater chains, distributed computing (`--protocol dqc`), raw-L2 classical transport, and a global timeline with a per-run fidelity certificate. See [`qne-sequence/README.md`](qne-sequence/README.md).

## Components

| Directory | Description |
|-----------|-------------|
| `qne/` | Core library + raw-socket BB84 path (Alice, Bob, detector, BB84 math, Cascade, PA, finite-key, decoy analysis, Eve, HMAC auth, CLI) |
| `qne-sequence/` | Distributed SeQUeNCe runtime: BB84 / E91 / repeater protocols, `0x7102` reliable L2 link, state service, lookahead timeline |
| `p4/` | BMv2 P4 program: per-wavelength fiber-loss drop for `0x7101`, lossless forwarding for `0x7102`, counters; PTF tests |
| `scripts/` | `deploy_fabric.py` (slice, switch, runs, sweeps, cross-validation, netem, repeater bridge), `decoy_sweep.py`, env setup |
| `validation/` | Platform-neutral scenarios + adapters for QFabric-sim, SeQUeNCe, NetSquid and the statistical agreement test |
| `notebooks/` | `00_overview` (orientation + environment check, no slice), then the slice notebooks: `fabric/` (deploy, run, analyse), `sequence/` (distributed runtime), `concepts/` (one capability each) |
| `docker/` | Prebuilt BMv2 image (GHCR) |
| `docs/` | All documentation: `PRIMER` → `CONCEPTS`, plus `SPEC`, `ASSUMPTIONS`, `ROADMAP`, release process, `reviews/`, `refs/` |
| `tests/`, `qne-sequence/tests/` | Physics-validated unit tests + multi-process loopback runs; both suites run in CI (see Install for how to run them) |

## Quick Start

### Notebooks — run in order

Open `00_overview` first — it checks your environment and needs no slice. **Every notebook
after it runs on a FABRIC slice**, numbered within its folder, so a notebook is named by its
folder (`fabric/02`, `concepts/05`). Start with `fabric/01`; the other two tracks assume the
slice it builds.

| Folder | # | Notebook | What it does |
|---|---|----------|--------------|
| `fabric/` | 01 | `01_setup_slice` | Provision the (single-site) slice, start BMv2, load the P4 tables |
| | 02 | `02_run_experiment` | Raw-socket BB84 across the slice, collect + verify |
| | 03 | `03_all_scenarios` | **Every** scenario (singles + distance/attenuation sweeps) with 4-way cross-validation; switch loss updated in place per point |
| | 04 | `04_analysis` | Plots and tables from everything `02` and `03` recorded |
| `sequence/` | 01 | `01_emulator` | Distributed BB84, both channels raw L2 through the switch, timeline certificate |
| | 02 | `02_scenarios` | Distance sweep of the distributed emulator vs the NetSquid reference |
| | 03 | `03_entanglement_e91` | E91/BBM92 over two nodes, CHSH test, Cascade + PA |
| `concepts/` | 01 | `01_eavesdropper` | Intercept-resend Eve: QBER vs tap fraction, the ~11% threshold |
| | 02 | `02_reconciliation` | Cascade makes the two keys match bit-for-bit |
| | 03 | `03_repeater` | Entanglement swapping across three real nodes |
| | 04 | `04_qkd_security` | Finite-key bounds, authenticated channel, biased bases, live decoy |
| | 05 | `05_distributed_computing` | A **gate** between two QPUs: teleport / non-local CNOT, and the cost of a lost correction |

Tracks: **deploy** `fabric/01→02→03→04`; **learn QKD** `fabric/02` → `concepts/01→02→04`;
**entanglement & compute** `sequence/03` → `concepts/03` → `concepts/05`.

### Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # core + tests
pytest tests/ -v
# distributed runtime (Python 3.12, sequence==1.0.0):
python3.12 -m venv .venv-sequence && source .venv-sequence/bin/activate
pip install -e ".[dev,sequence]" && (cd qne-sequence && PYTHONPATH=.. pytest tests -q)
```

The cross-validation simulators live in their own per-node envs on the slice (`deploy_fabric.setup_sim_envs`: SeQUeNCe needs Python 3.12, NetSquid needs 3.10/3.11 and netsquid.org credentials in `NETSQUID_USER`/`NETSQUID_PASS`). Missing backends are reported **SKIPPED**; an empty comparison is **INCONCLUSIVE**, never a pass.

### Run on FABRIC from the command line

```bash
python scripts/deploy_fabric.py --scenario validation/scenarios/fabric_1km.yml
python scripts/deploy_fabric.py --cleanup
```

## Key parameters

Fiber loss `P(loss) = 1 − 10^(−α·L/10)` is installed in the switch as `threshold = floor(P·2³²)` (clamped to 2³²−1) and compared against a per-frame 32-bit random draw. Intrinsic QBER ≈ (1−F)/2 for polarization fidelity F, plus dark counts (`dark_count_rate × detection_window`, drawn in **every** slot including lost photons). Scenarios are YAML under `validation/scenarios/`.

**Randomness and reproducibility.** `seed` is `None` by default: bits, bases and samples come from OS entropy and two runs give two different keys. Set an integer seed only for reproducible emulation runs (cross-validation, tests) — a seeded run's "secret" is derivable from the scenario file and is not a key.

## What the numbers mean (and do not)

- A **measured** QFabric point is a real distributed run whose fiber loss and noise are statistical models in the switch and the detector. It validates the *protocol and the platform*, not photonics.
- The **decoy-state** pipeline (Ma–Qi–Zhao–Lo bounds, GLLP rate) runs on the distributed TCP-descriptor transport with a Poisson source; it has **not** run through the P4 photon path (the `0x7101` frame has no photon-count field) and no photon-number-splitting adversary is modeled. It is honest key-rate accounting for a realistic source, not a demonstrated defense.
- **CHSH > 2** across nodes shows the shared-state service and the heralded corrections are correct across real links; it is **not** a physical Bell-inequality test.
- A **distributed gate** (`--protocol dqc`) spends a Bell pair and puts its correction bits on the real link, and errs at `(1−w)/2` — the same curve as the E91 QBER. The two QPUs share one register authority, and a pair's fidelity does not decay while it waits, so distributed-gate fidelity is **not** yet a function of distance.
- The **lookahead certificate** (`lookahead.certified`) is meaningful only when a nonzero channel delay was modeled; with `channel_delay = 0` it is reported as not applicable.
- Finite-key lengths use the TLGR constant; small blocks (≲ 10⁴ sampled bits) legitimately yield **zero** secret bits. Every reconciled run ends with a key-verification tag; a mismatch aborts with no key.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE). © 2026 Komal Thareja (kthare10@renci.org)
