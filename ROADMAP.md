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
- ✅ Detector realism: `dead_time` (blind window after each click, arrival-time gated) and `timing_jitter` (gaussian; clicks outside the detection window are lost — effective efficiency × erf(w/(2√2σ))) modeled in `qne/detector.py`; wired through `bob.py` (config) and `node_runner` (`--dead-time`, `--timing-jitter`, `--pulse-period-ns`).
- ✅ Consolidated the secure-key-rate math into `BB84Protocol.secure_key_fraction`, used by the sim path, the live Bob path, and the simulator adapters.
- ✅ Removed the vestigial dead code in `Alice`/`Bob._run_sifting`.
- ✅ Error correction: **Cascade reconciliation** (`qne/cascade.py`) wired into **both** the distributed E91/BBM92 and BB84 paths (shared `qne-sequence/reconcile_link.py`) so Alice's and Bob's keys match bit-for-bit; `bits_leaked` tracked and subtracted in the secure-key accounting, then a Toeplitz-hash privacy-amplification extractor (`qne/privacy.py`) produces the final secret key. Finite-key bounds are still asymptotic.
- ⬜ GPU-accelerated density-matrix tracking for quantum-memory emulation (needed for entanglement-based protocols).

## Phase 2b — QKD security & post-processing depth ⬜

The prepare-and-measure QKD path produces correct QBER / sift / secure-fraction
*estimates*, but several pieces needed for an end-to-end, defensible secret key —
and for the security story a reviewer expects — are not yet built. Ordered by value:

- ✅ **Adversary model — intercept-resend Eve.** `qne/eve.py`; taps a configurable fraction `f` of photons and is wired into the distributed BB84 path (`node_runner --eve-fraction f`). Verified end-to-end: sifted QBER ≈ 0.25·f and the secure fraction collapses to 0 past the ~11% threshold (f=1 → QBER ≈ 0.25). A beam-splitting / PNS Eve and an Eve-on-E91 variant are still open. Demo: notebook `10_eavesdropper` (+ FABRIC variant).
- ✅ **Reconciliation on the BB84 path.** Cascade now runs on the distributed BB84 path too (`node_runner`, via `qne-sequence/reconcile_link.py` — shared with E91): after the protocol's timeline finishes, Bob corrects his key toward Alice's over the same TCP link, and both report the identical key bit-for-bit. Gated on a positive secure fraction, so a run above the ~11% QBER threshold aborts instead of reconciling (verified with the intercept-resend Eve). The `qne/` raw-socket Bob (`bob.py`) is not yet wired.
- ✅ **Real privacy amplification.** `qne/privacy.py` — a seeded **Toeplitz (2-universal) hash** compresses the reconciled key to the secure length; both sides apply the same public hash and extract the identical final secret key (not just a length estimate). Wired into both distributed paths after Cascade; the reported `key` is now the amplified secret and `secure_key_bits` is its true length. The Cascade+PA driver now lives in `qne/reconcile.py` (shared; `qne-sequence/reconcile_link.py` re-exports it) and the **raw-socket `qne/` path is wired too**: `bob.py` discloses only a random sample (the old everything-disclosed shortcut is gone), drives Cascade against Alice as a parity oracle over the same TCP channel, and both extract the identical amplified secret.
- ✅ **Finite-key security.** `qne/finite_key.py`: Serfling-corrected QBER upper bound + TLGR-style length ℓ = n(1 − h(Q+μ)) − leak_EC − log(ε) terms, with the *measured* Cascade leak. `--finite-key` on `node_runner` (BB84 + E91) sizes privacy amplification with the finite bound and reports `finite_key` metrics (secret_bits, qber_upper, μ) alongside the asymptotic number.
- ✅ **Authenticated classical channel.** `qne/auth.py`: per-frame HMAC-SHA256 tag + strictly-sequential anti-replay sequence numbers under a pre-shared key (the Wegman–Carter interface, computational MAC), on **both** transports — `qne/channel.py` (raw-socket path, `--auth-key` on the CLI) and the qne-sequence `Link` (`node_runner --auth-key`). Tampered/replayed/spliced frames tear the connection down; `auth_failures` reported. Measuring auth cost vs WAN RTT on FABRIC is the remaining experiment.
- ✅ **Decoy on the live transport.** Alice's `DistributedBB84` emits per-pulse intensities (signal/decoy/vacuum, Poisson photon numbers, per-photon fiber thinning); Bob detects pulses with 1−(1−η)^n (+ darks on empty pulses); measured per-intensity gains/QBERs feed `qne/decoy.py`'s Lo–Ma–Chen/GLLP analysis unchanged; key comes from signal pulses only. `node_runner --decoy --mu-signal/--mu-decoy/--mu-vacuum --decoy-probs`. TCP transport only (the 0x7101 frame has no photon-count field yet).
- ✅ **Efficient (biased-basis) BB84.** `--basis-bias p` (and `protocol.basis_bias` in scenario YAML): both sides pick Z with probability p, sift ratio p²+(1−p)² > 50%; key from Z–Z matches, ALL X–X matches disclosed for the phase-error estimate, rate 1 − h(e_z) − h(e_x) (`BB84Protocol.efficient_secure_fraction`). On both the distributed and raw-socket paths.
- ✅ **Detector realism.** `dead_time` / `timing_jitter` modeled (see Phase 2 above).
- ✅ **Demo notebook.** `13_qkd_security` (local, executed): finite-key rate-vs-block-size curves + a live `--finite-key` run, auth tamper/replay demos + an authenticated run, biased-basis sift ratio + efficient rate, and the live decoy pipeline with measured gains vs the weak-coherent model.

## Phase 3 — Cross-Validation ✅ (core)

- ✅ Platform-neutral `ValidationScenario`; standalone `--json` adapters; on-node + subprocess runners.
- ✅ **4-way comparison on the FABRIC slice**: measured QFabric (BMv2) + QFabric-sim (switch) + SeQUeNCe (alice/3.12) + NetSquid (bob), driven by `run_cross_validation_on_fabric`.
- ✅ Native engines: SeQUeNCe 1.0 (`pair_bb84_protocols` + KeyManager) and NetSquid (qubits + `DepolarNoiseModel`).
- ✅ Statistically-correct agreement test (combined-variance, `qber_sample_bits`-aware) + honest SKIPPED/INCONCLUSIVE reporting.
- ✅ The **live BMv2/socket measurement** is the QFabric data point in the comparison (not just the sim).
- 🟡 Confirm SeQUeNCe/NetSquid versions on your slice (deadsnakes 3.12 build + netsquid.org creds).
- 🟡 Quantify where **real classical-network effects** (latency, jitter, congestion) make QFabric diverge from ideal-channel simulators — the core scientific contribution. Scaffolding done: `apply_classical_netem` (impairs only TCP:5100), `run_network_conditions_experiment`, and notebook `06_network_effects` (throughput / time-to-key / QBER vs condition). Needs a recorded FABRIC dataset across conditions/sites.
- ⬜ Publish the cross-validation **dataset**.

## Phase 4 — Scale-Up Experiments 🟡

- 🟡 Multi-hop **quantum repeater chain**: in-process AND 3-process-distributed halves are DONE.
  - In-process: `qstate_core.bell_measure` (BSM/swap op, group merge, heralded X^m2·Z^m1 Pauli correction) + `qne-sequence/qne_sequence/repeater.py` (n-node local chain, per-link loss, BBM92/CHSH end-to-end measurement, `python -m qne_sequence.repeater`). Validated against the Werner-chain law F = (1+3·f^L)/4 / QBER = (1−f^L)/2 / S = 2√2·f^L, plus the no-correction control (QBER → 0.5: the herald channel is load-bearing).
  - Distributed: `distributed_repeater.py` — THREE processes over three TCP links (`node_runner --protocol repeater --role alice|repeater|bob`, per-link `--bob-host`/`--repeater-host`). Alice hosts the register + creates both link pairs; the repeater process performs the BSMs via RPC and forwards the heralds to Bob over its OWN link; Bob applies the corrections and runs the full BBM92/E91 tail (sift, QBER sample, CHSH, Cascade+PA, `--finite-key`, `--auth-key` on all three links). Verified over loopback: Werner-law QBER, CHSH > 2 across the swapped chain, identical extracted secrets, and the no-correction control.
  - FABRIC deployment support (needs a live-slice validation run): `deploy_fabric.setup_repeater_bridge` (stops BMv2, joins the switch's data-plane ifaces in a Linux bridge carrying station IP 10.10.1.3 + kernel forwarding + static ARP) and `deploy_fabric.run_sequence_repeater` (station on the switch node; results to `results/fabric_repeater_*.json`); `setup_sequence_runtime(nodes=("alice","bob","switch"))` builds the runtime on the station. Notebook `12_repeater` (local, executed) + `12_repeater_fabric` (slice variant, gitignored). NEXT: validate on a live slice, then 5–10 sites / n-node chains.
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
| **Entanglement swapping** (repeaters) | 🟡 | BSM swap op + heralded correction validated in-process (`repeater.py`) AND across 3 processes with real herald links (`distributed_repeater.py`); FABRIC slice run + n-node chains are next. |
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

- The `qne/` hand-coded path models photons at the bit/basis level (no entanglement). Entanglement (E91/BBM92) lives in `qne-sequence/` on a shared multi-qubit **quantum-state service**, running distributed over 2 nodes; entanglement swapping + a LOCAL in-process repeater chain are built and validated (`repeater.py`) — distributing the heralds across 3+ processes is the next extension.
- QBER comes from a depolarizing polarization-misalignment model (≈ (1−F)/2) plus dark counts; `dead_time` and `timing_jitter` are now modeled in the detector (dead-time gating needs a pulse period / arrival times).
- Adversary model available for the BB84 path (`qne/eve.py`, intercept-resend, `--eve-fraction`) — measured QBER then reflects channel noise **plus** eavesdropping. A beam-splitting / PNS Eve and an Eve-on-E91 path are still open (see Phase 2b).
- Security accounting: asymptotic Shor–Preskill by default, **finite-key** (Serfling + TLGR-style length) with `--finite-key`; classical channel can be **HMAC-authenticated** with `--auth-key` (computational MAC standing in for Wegman–Carter; unauthenticated remains the default). Cascade + Toeplitz PA run on all three paths (distributed BB84, E91, raw-socket `qne/`) — both sides extract an identical secret key.
- Decoy-state runs live on the TCP transport only; the raw 0x7101 frame has no photon-count field yet (per-photon thinning happens at the source).
- Memoryless per-packet loss — no burst loss or correlated fading.
- Single wavelength, single link per run.
- P4 and Python RNGs are independent — reproducibility holds within a backend, not bit-for-bit across the P4 and Python paths.
