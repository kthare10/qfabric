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
- ✅ Sweep figures generated locally via `paper/make_figures.py` (QBER + key rate vs distance/attenuation). `paper/` is git-ignored (drafts + regenerable figures), so re-run the script to produce them. Validate measured drop rate vs analytical once a clean FABRIC sweep dataset is recorded.
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
- 🟡 Error correction: **Cascade reconciliation implemented** (`qne/cascade.py`), wired into distributed E91/BBM92 so Alice's and Bob's keys match bit-for-bit; `bits_leaked` is tracked. Full privacy amplification (universal hashing) beyond the asymptotic Shor–Preskill estimate is still pending, as is Cascade for the BB84 path.
- ⬜ GPU-accelerated density-matrix tracking for quantum-memory emulation (needed for entanglement-based protocols).

## Phase 2b — QKD security & post-processing depth ⬜

The prepare-and-measure QKD path produces correct QBER / sift / secure-fraction
*estimates*, but several pieces needed for an end-to-end, defensible secret key —
and for the security story a reviewer expects — are not yet built. Ordered by value:

- ✅ **Adversary model — intercept-resend Eve.** `qne/eve.py`; taps a configurable fraction `f` of photons and is wired into the distributed BB84 path (`node_runner --eve-fraction f`). Verified end-to-end: sifted QBER ≈ 0.25·f and the secure fraction collapses to 0 past the ~11% threshold (f=1 → QBER ≈ 0.25). A beam-splitting / PNS Eve and an Eve-on-E91 variant are still open. → still pairs with an eavesdropper demo notebook (pending).
- ⬜ **Reconciliation on the BB84 path.** `qne/cascade.py` is wired into E91 only; wire it into `qne/bob.py` + `qne-sequence` `distributed_qkd.py` (same parity-oracle pattern) so prepare-and-measure keys also match bit-for-bit.
- ⬜ **Real privacy amplification.** Replace the asymptotic estimate with an actual extractor (Toeplitz / 2-universal hashing) that outputs the final secret key and a leak-adjusted length.
- ⬜ **Finite-key security.** Report a finite-key secure length (Lim et al. / Tomamichel) alongside the asymptotic Shor–Preskill rate — the honest number for a run of N pulses, and it ties directly to "how long must we run over a real WAN."
- ⬜ **Authenticated classical channel.** BB84's proof requires an *authenticated* classical channel; today it's plain TCP. Add a Wegman–Carter / HMAC tag on the sifting messages and account for the authentication-key cost — also a networking-overhead result (auth cost vs WAN RTT).
- ⬜ **Decoy on the live transport.** `qne/decoy.py` is analysis-only; have Alice emit multi-intensity pulses over the P4/TCP path and Bob bin detections by intensity, so the PNS-resilience story runs on real hardware, not just in a sweep script.
- ⬜ **Efficient (biased-basis) BB84.** Asymmetric Z/X basis probabilities to lift sifting above 50% and raise the key rate — a cheap, cited win and a natural sweep axis.
- 🟡 **Detector realism.** Model `dead_time` and `timing_jitter` (parsed today but ignored) so the detector arm is honest and adds a knob.

## Phase 3 — Cross-Validation ✅ (core)

- ✅ Platform-neutral `ValidationScenario`; standalone `--json` adapters; on-node + subprocess runners.
- ✅ **4-way comparison on the FABRIC slice**: measured QFabric (BMv2) + QFabric-sim (switch) + SeQUeNCe (alice/3.12) + NetSquid (bob), driven by `run_cross_validation_on_fabric`.
- ✅ Native engines: SeQUeNCe 1.0 (`pair_bb84_protocols` + KeyManager) and NetSquid (qubits + `DepolarNoiseModel`).
- ✅ Statistically-correct agreement test (combined-variance, `qber_sample_bits`-aware) + honest SKIPPED/INCONCLUSIVE reporting.
- ✅ The **live BMv2/socket measurement** is the QFabric data point in the comparison (not just the sim).
- 🟡 Confirm SeQUeNCe/NetSquid versions on your slice (deadsnakes 3.12 build + netsquid.org creds).
- 🟡 Quantify where **real classical-network effects** (latency, jitter, congestion) make QFabric diverge from ideal-channel simulators — the core scientific contribution. Scaffolding done: `apply_classical_netem` (impairs only TCP:5100), `run_network_conditions_experiment`, and notebook `06_network_effects` (throughput / time-to-key / QBER vs condition). Needs a recorded FABRIC dataset across conditions/sites.
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
| **BB84 QKD** | ✅ | Prepare-and-measure baseline (`qne/`, `qne-sequence/`) |
| **Decoy-state BB84** | ✅ | PNS-resilient key rate (`qne/decoy.py`): weak-coherent Poisson source, 3 intensities, full Lo–Ma–Chen Y1/e1 bounds → GLLP secure key rate; sweep + figure via `scripts/decoy_sweep.py`. Analysis/simulation arm (not yet wired into the live multi-intensity transport). |
| **E91 / BBM92 QKD** | ✅ | Entanglement-based QKD on the shared quantum-state service (`qne-sequence/qstate_core.py`, `e91.py`), running **distributed over 2 nodes** (`distributed_e91.py`, `remote_qm.py`; `--protocol e91\|bbm92`). Werner-state model ties QBER=(1−F)/2 and CHSH S=2√2·F; Bell-test coordination + basis/sample disclosure ride the real link; sift/QBER reuse `BB84Protocol`. |
| **Entanglement swapping** (repeaters) | ⬜ | Highest novelty; the n-qubit register + state service are designed for it (Bell-measurement swap on two register qubits + heralding). |
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

- The `qne/` hand-coded path models photons at the bit/basis level (no entanglement). Entanglement (E91/BBM92) lives in `qne-sequence/` on a shared multi-qubit **quantum-state service**, running distributed over 2 nodes; multi-hop entanglement swapping (repeater chains) is the next extension.
- QBER comes from a depolarizing polarization-misalignment model (≈ (1−F)/2) plus dark counts; phase/timing error sources (`dead_time`, `timing_jitter`) are not yet modeled.
- Adversary model available for the BB84 path (`qne/eve.py`, intercept-resend, `--eve-fraction`) — measured QBER then reflects channel noise **plus** eavesdropping. A beam-splitting / PNS Eve and an Eve-on-E91 path are still open (see Phase 2b).
- **Security is asymptotic** (Shor–Preskill secure fraction): no finite-key bound, no real privacy-amplification extractor, and the classical channel is unauthenticated.
- **BB84 keys are not yet reconciled** — Cascade runs on the E91 path only, so the prepare-and-measure path reports a secure-fraction *estimate* rather than a bit-for-bit matching secret key.
- Memoryless per-packet loss — no burst loss or correlated fading.
- Single wavelength, single link per run.
- P4 and Python RNGs are independent — reproducibility holds within a backend, not bit-for-bit across the P4 and Python paths.
