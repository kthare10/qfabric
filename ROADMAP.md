# QFabric Roadmap

This roadmap tracks what QFabric implements today and the planned path toward a multi-protocol, multi-site quantum network emulator. It is grounded in the project's research plan (Idea 1: *Quantum Network Emulation & Simulation at Scale*).

Legend: ✅ done · 🟡 in progress / partial · ⬜ planned

---

## Status at a Glance (v0.1.0)

QFabric runs **BB84 QKD over a single emulated link**, end-to-end on a real FABRIC slice, and cross-validates the model against the SeQUeNCe and NetSquid simulators.

| Capability | Status |
|------------|--------|
| Photon wire format (EtherType `0x7101`) | ✅ |
| P4 fiber-loss channel model (BMv2) | ✅ |
| Python QNE (Alice, Bob, detector, BB84) | ✅ |
| Classical TCP sifting channel | ✅ |
| FABRIC 3-node deployment | ✅ |
| 4-way cross-validation **on FABRIC nodes** (measured + sim + SeQUeNCe + NetSquid) | ✅ |
| Native SeQUeNCe 1.0 (alice/3.12) & NetSquid (bob) engines | ✅ wired; confirm versions on your slice |
| Statistically-correct agreement test (combined-variance, sample-size aware) | ✅ |
| Intrinsic QBER model (polarization fidelity) | ✅ |
| Linear notebook workflow (overview→setup→run→validate→analysis) | ✅ |
| Unit tests (bb84, detector, photon, metrics, validation) | ✅ |

---

## Phase 1 — P4 Quantum Channel Model ✅ (mostly)

- ✅ Fiber loss as probabilistic drop, `P(loss) = 1 − 10^(−α·L/10)`, threshold-based.
- ✅ Per-wavelength loss table; photon TX/drop counters.
- ✅ Classical-traffic L2 forwarding (FABRIC OVS MAC workaround).
- ✅ Sweep figures checked in (`paper/figures/`, via `paper/make_figures.py`) — QBER + key rate vs distance/attenuation. Validate measured drop rate vs analytical once a clean FABRIC sweep dataset is recorded.
- ⬜ **Timing jitter injection** in the data plane and validation against detector specs.
- ⬜ **Throughput benchmark**: sustainable photon rate / P4 processing overhead.
- ⬜ Port the model from BMv2 to **Tofino / DPDK SmartNIC** for finer timing control.

## Phase 2 — Quantum Node Emulator 🟡

- ✅ Photon packet generation/reception over raw sockets.
- ✅ BB84 as the first protocol; detector model (efficiency, dark counts, random-basis measurement).
- ✅ Polarization fidelity modeled as a depolarizing misalignment in the detector (`polarization_error = 1 − F`), giving a realistic intrinsic QBER ≈ (1−F)/2. Used by both the sim path and the live Bob path.
- 🟡 Detector realism: `dead_time` and `timing_jitter` are parsed from config but **not yet modeled**.
- ✅ Consolidated the secure-key-rate math into `BB84Protocol.secure_key_fraction`, used by the sim path, the live Bob path, and the simulator adapters.
- ✅ Removed the vestigial dead code in `Alice`/`Bob._run_sifting`.
- ⬜ Error correction (e.g., Cascade) and privacy amplification beyond the asymptotic Shor–Preskill estimate.
- ⬜ GPU-accelerated density-matrix tracking for quantum-memory emulation (needed for entanglement-based protocols).

## Phase 3 — Cross-Validation ✅ (core)

- ✅ Platform-neutral `ValidationScenario`; standalone `--json` adapters; on-node + subprocess runners.
- ✅ **4-way comparison on the FABRIC slice**: measured QFabric (BMv2) + QFabric-sim (switch) + SeQUeNCe (alice/3.12) + NetSquid (bob), driven by `run_cross_validation_on_fabric`.
- ✅ Native engines: SeQUeNCe 1.0 (`pair_bb84_protocols` + KeyManager) and NetSquid (qubits + `DepolarNoiseModel`).
- ✅ Statistically-correct agreement test (combined-variance, `qber_sample_bits`-aware) + honest SKIPPED/INCONCLUSIVE reporting.
- ✅ The **live BMv2/socket measurement** is the QFabric data point in the comparison (not just the sim).
- 🟡 Confirm SeQUeNCe/NetSquid versions on your slice (deadsnakes 3.12 build + netsquid.org creds).
- ⬜ Quantify where **real classical-network effects** (latency, jitter, congestion) make QFabric diverge from ideal-channel simulators — the core scientific contribution.
- ⬜ Publish the cross-validation **dataset**.

## Phase 4 — Scale-Up Experiments ⬜

- ⬜ Multi-hop **quantum repeater chain** across 5–10 FABRIC sites (header already carries seq/wavelength for this).
- ⬜ Entanglement distribution under **baseline / congested / asymmetric-latency / link-failure** conditions.
- ⬜ Measure entanglement-fidelity degradation per condition.
- ⬜ WDM: multiple wavelengths per link (loss table is already wavelength-keyed).

## Phase 5 — Packaging & Reproducibility ⬜

- 🟡 Kiso experiment templates (local + FABRIC configs exist; parameterize topology).
- ✅ Containerized BMv2 toolchain (`docker/Dockerfile.bmv2`, Ubuntu-based) + GHCR publish workflow; switch can pull a prebuilt image instead of building from source (`QFABRIC_BMV2_IMAGE`).
- ⬜ One-click parameterized topology template.
- ⬜ Artifact submission for reproducibility evaluation.

---

## Protocol Backlog

Priority order from the research plan:

| Protocol | Status | Notes |
|----------|--------|-------|
| **BB84 QKD** | ✅ | Prepare-and-measure baseline |
| **E91 QKD** | ⬜ | Requires Bell-test coordination over the classical channel |
| **Entanglement swapping** (repeaters) | ⬜ | Highest novelty; needs quantum-memory + herald signaling |
| **Quantum teleportation** | ⬜ | Stretch goal; classical bits per teleport |

---

## Engineering / Hygiene Backlog

- ✅ License (Apache 2.0) + author headers across all source files.
- ✅ Public GitHub repo with GPG-signed history (github.com/kthare10/qfabric).
- ✅ CI: `pytest` + ruff + simulation-mode cross-validation on every push (`.github/workflows/tests.yml`).
- ✅ Lint/format gate (`ruff` clean; config + per-file-ignores in `pyproject.toml`).
- ✅ Pin simulator versions + document install (SeQUeNCe pinned; NetSquid documented; per-node env scripts).
- ⬜ Type-check pass and docstring coverage.

---

## Known Limitations (today)

- Photons are modeled at the bit/basis level, not as full quantum states — no entanglement or multi-qubit support yet.
- QBER comes from a depolarizing polarization-misalignment model (≈ (1−F)/2) plus dark counts; phase/timing error sources (`dead_time`, `timing_jitter`) are not yet modeled.
- Memoryless per-packet loss — no burst loss or correlated fading.
- Single wavelength, single link per run.
- P4 and Python RNGs are independent — reproducibility holds within a backend, not bit-for-bit across the P4 and Python paths.
