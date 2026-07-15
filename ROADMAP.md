# QFabric Roadmap

This roadmap tracks what QFabric implements today and the planned path toward a multi-protocol, multi-site quantum network emulator. It is grounded in the project's research plan (Idea 1: *Quantum Network Emulation & Simulation at Scale*).

Legend: ✅ done · 🟡 in progress / partial · ⬜ planned

---

## Status at a Glance

QFabric runs **BB84 and entanglement-based QKD (E91/BBM92) end-to-end on a real FABRIC slice** — including a **3-node quantum-repeater chain validated over the live WAN** — with the full key-distillation pipeline (sift → Cascade → Toeplitz privacy amplification), a security-depth stack (finite-key, authenticated channel, live decoy states, biased bases, detector realism), and cross-validation against the SeQUeNCe and NetSquid simulators.

| Capability | Status |
|------------|--------|
| Photon wire format (EtherType `0x7101`) + P4 fiber-loss channel (BMv2) | ✅ |
| Python QNE (Alice, Bob, detector, BB84) + classical TCP channel | ✅ |
| FABRIC 3-node deployment | ✅ |
| 4-way cross-validation **on FABRIC nodes** (measured + sim + SeQUeNCe + NetSquid) | ✅ |
| Full pipeline sift → reconcile (Cascade) → amplify (Toeplitz) on all paths | ✅ |
| Entanglement (E91/BBM92) distributed over 2 nodes; CHSH > 2 on real hardware | ✅ |
| **Repeater chain (entanglement swapping) across 3 nodes on the live WAN** | ✅ validated 2026-07-13 |
| Security depth: finite-key, authenticated channel, live decoy, biased bases, Eve | ✅ |
| Detector realism (efficiency, dark counts, dead time, timing jitter) | ✅ |
| Notebook workflow 00–13 (slice workflow + local demos) | ✅ |
| Docs: `PRIMER.md` (concepts from zero) + `CONCEPTS.md` (concept → code map) | ✅ |
| **Emulation-fidelity program: lookahead delivery + clock sync (no PTP)** — every classical message delivered at exactly `t_send + delay` in shared clock terms (BB84 timeline path, E91, repeater chains, Cascade); per-run certificate (`lookahead.late_events == 0` ⇒ the run executed the simulator's schedule); unified distance knob (`--channel-delay auto` derives delay from the same L as loss); single-site slice default | ✅ 2026-07-15 |
| Tests: 112 core + 85 distributed, physics-validated; ruff-clean CI | ✅ |

---

## Phase 1 — P4 Quantum Channel Model ✅ (mostly)

- ✅ Fiber loss as probabilistic drop, `P(loss) = 1 − 10^(−α·L/10)`, threshold-based.
- ✅ Per-wavelength loss table; photon TX/drop counters.
- ✅ Classical-traffic L2 forwarding (FABRIC OVS MAC workaround).
- ✅ Sweep figures generated locally via `paper/make_figures.py` (QBER + key rate vs distance/attenuation). `paper/` is git-ignored (drafts + regenerable figures), so re-run the script to produce them. Validate measured drop rate vs analytical once a clean FABRIC sweep dataset is recorded.
- ⬜ **Timing jitter injection** in the data plane and validation against detector specs.
- ⬜ **Throughput benchmark**: sustainable photon rate / P4 processing overhead.
- ⬜ Port the model from BMv2 to **Tofino / DPDK SmartNIC** for finer timing control.

## Phase 2 — Quantum Node Emulator ✅ (core)

- ✅ Photon packet generation/reception over raw sockets.
- ✅ BB84 as the first protocol; detector model (efficiency, dark counts, random-basis measurement).
- ✅ Polarization fidelity modeled as a depolarizing misalignment in the detector (`polarization_error = 1 − F`), giving a realistic intrinsic QBER ≈ (1−F)/2. Used by both the sim path and the live Bob path.
- ✅ Detector realism: `dead_time` (blind window after each click, arrival-time gated) and `timing_jitter` (gaussian; clicks outside the detection window are lost — effective efficiency × erf(w/(2√2σ))) modeled in `qne/detector.py`; wired through `bob.py` (config) and `node_runner` (`--dead-time`, `--timing-jitter`, `--pulse-period-ns`).
- ✅ Consolidated the secure-key-rate math into `BB84Protocol.secure_key_fraction`, used by the sim path, the live Bob path, and the simulator adapters.
- ✅ Removed the vestigial dead code in `Alice`/`Bob._run_sifting`.
- ✅ Error correction: **Cascade reconciliation** (`qne/cascade.py`) wired into **both** the distributed E91/BBM92 and BB84 paths (shared `qne-sequence/reconcile_link.py`) so Alice's and Bob's keys match bit-for-bit; `bits_leaked` tracked and subtracted in the secure-key accounting, then a Toeplitz-hash privacy-amplification extractor (`qne/privacy.py`) produces the final secret key. Finite-key sizing available via `--finite-key` (see Phase 2b).
- ⬜ GPU-accelerated density-matrix tracking for quantum-memory emulation (needed for entanglement-based protocols).

## Phase 2b — QKD security & post-processing depth ✅

All items complete (2026-07-12): the QKD paths now produce an end-to-end,
defensible secret key with the security story a reviewer expects. Remaining
follow-ons are noted inline (PNS/E91 Eve variants, decoy on the raw transport,
and the netem cost-measurement datasets).

- ✅ **Adversary model — intercept-resend Eve.** `qne/eve.py`; taps a configurable fraction `f` of photons and is wired into the distributed BB84 path (`node_runner --eve-fraction f`). Verified end-to-end: sifted QBER ≈ 0.25·f and the secure fraction collapses to 0 past the ~11% threshold (f=1 → QBER ≈ 0.25). A beam-splitting / PNS Eve and an Eve-on-E91 variant are still open. Demo: notebook `10_eavesdropper` (+ FABRIC variant).
- ✅ **Reconciliation on the BB84 path.** Cascade now runs on the distributed BB84 path too (`node_runner`, via `qne-sequence/reconcile_link.py` — shared with E91): after the protocol's timeline finishes, Bob corrects his key toward Alice's over the same TCP link, and both report the identical key bit-for-bit. Gated on a positive secure fraction, so a run above the ~11% QBER threshold aborts instead of reconciling (verified with the intercept-resend Eve). The `qne/` raw-socket path is wired too (see the privacy-amplification item below).
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

- ✅ Multi-hop **quantum repeater chain (3 nodes)** — in-process, 3-process-distributed, AND **validated on a live FABRIC slice (2026-07-13)**.
  - In-process: `qstate_core.bell_measure` (BSM/swap op, group merge, heralded X^m2·Z^m1 Pauli correction) + `qne-sequence/qne_sequence/repeater.py` (n-node local chain, per-link loss, BBM92/CHSH end-to-end measurement, `python -m qne_sequence.repeater`). Validated against the Werner-chain law F = (1+3·f^L)/4 / QBER = (1−f^L)/2 / S = 2√2·f^L, plus the no-correction control (QBER → 0.5: the herald channel is load-bearing).
  - Distributed: `distributed_repeater.py` — THREE processes over three TCP links (`node_runner --protocol repeater --role alice|repeater|bob`, per-link `--bob-host`/`--repeater-host`). Alice hosts the register + creates both link pairs; the repeater process performs the BSMs via RPC and forwards the heralds to Bob over its OWN link; Bob applies the corrections and runs the full BBM92/E91 tail (sift, QBER sample, CHSH, Cascade+PA, `--finite-key`, `--auth-key` on all three links). Verified over loopback: Werner-law QBER, CHSH > 2 across the swapped chain, identical extracted secrets, and the no-correction control.
  - **Live WAN validation (2026-07-13):** the station ran on the switch node, physically between the endpoints; Alice and Bob extracted an **identical secret key** (Cascade-reconciled + Toeplitz-amplified) with the station performing thousands of entanglement swaps and the heralds crossing a real ~25 ms WAN segment on their own connection. Driven both manually and end-to-end from notebook `12_repeater_fabric`; per-run artifacts land in `results/fabric_repeater_*.json` (gitignored run outputs).
  - Deployment: `deploy_fabric.setup_repeater_bridge` — hardened by the live debug: stops the **bmv2 Docker container** (pkill can't), `modprobe bridge` (fresh VMs lack the module), Linux bridge `br-qne` carrying station IP 10.10.1.3 with the **alice-side port MAC** (FABRIC/OVS MAC-learning workaround), kernel forwarding + `FORWARD ACCEPT` + `rp_filter/send_redirects` off, static ARP on all three nodes; pings all three links and **raises** on failure. `run_sequence_repeater` launches the three roles; `setup_sequence_runtime(nodes=("alice","bob","switch"))` builds the runtime on the station. Mutually exclusive with BMv2 (restore via `configure_switch`). Notebooks: `12_repeater` (local, committed) + `12_repeater_fabric` (slice variant, gitignored).
  - ✅ **n-node chains (2026-07-13):** `distributed_repeater.py` generalized to K stations (K+2 nodes, K+1 links; station i listens on port+2i−1, bob on port+2i; `--num-stations/--station-index/--repeater-hosts`). Each station is oblivious to its siblings; Bob XOR-composes the K herald streams into one Pauli correction. Loopback tests: 4-node vs the L=3 law, 5-node CHSH violation (S≈2.50 across five processes), no-herald control. **4-node chain validated over the live WAN** (two station processes co-located on the switch node): QBER 6.8% vs law 7.1%, identical keys. `run_sequence_repeater(num_stations=K)` launches co-located stations; distinct-site stations need a bigger slice topology.
  - NEXT: multi-site topologies (one station per intermediate site — needs new slice layouts), 5–10 sites, herald-latency dataset via `apply_classical_netem` (first sweep measured: time-to-key 14→42 s for +0→100 ms segment RTT; see `results/wan_battery_2026-07-13/`).
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
| **Decoy-state BB84** | ✅ | PNS-resilient key rate (`qne/decoy.py`): weak-coherent Poisson source, 3 intensities, full Lo–Ma–Chen Y1/e1 bounds → GLLP secure key rate; sweep + figure via `scripts/decoy_sweep.py`. **Runs on the live transport** (`node_runner --decoy`: real per-pulse photon numbers, measured per-intensity gains/QBERs feed the analysis); TCP transport only — the raw 0x7101 frame has no photon-count field yet. |
| **E91 / BBM92 QKD** | ✅ | Entanglement-based QKD on the shared quantum-state service (`qne-sequence/qstate_core.py`, `e91.py`), running **distributed over 2 nodes** (`distributed_e91.py`, `remote_qm.py`; `--protocol e91\|bbm92`). Werner-state model ties QBER=(1−F)/2 and CHSH S=2√2·F; Bell-test coordination + basis/sample disclosure ride the real link; sift/QBER reuse `BB84Protocol`. |
| **Entanglement swapping** (repeaters) | ✅ (3-node) | BSM swap op + heralded correction validated in-process (`repeater.py`), across 3 processes (`distributed_repeater.py`), and **on a live FABRIC slice (2026-07-13)** — identical keys over a swapped chain with heralds on a real WAN segment. n-node chains (>1 station) are next. |
| **Quantum teleportation** | ⬜ | Stretch goal; classical bits per teleport |

---

## Engineering / Hygiene Backlog

- ✅ License (Apache 2.0) + author headers across all source files.
- ✅ Public GitHub repo with GPG-signed history (github.com/kthare10/qfabric).
- ✅ CI: `pytest` + ruff + simulation-mode cross-validation on every push (`.github/workflows/tests.yml`).
- ✅ Lint/format gate (`ruff` clean; config + per-file-ignores in `pyproject.toml`).
- ✅ Pin simulator versions + document install (SeQUeNCe pinned; NetSquid documented; per-node env scripts).
- ✅ Concept documentation: `PRIMER.md` (quantum networking from zero, no code) + `CONCEPTS.md` (every concept mapped to the implementing code, tests, and notebooks); README reading path for newcomers.
- ⬜ Type-check pass and docstring coverage.

---

## Known Limitations (today)

- The `qne/` hand-coded path models photons at the bit/basis level (no entanglement). Entanglement (E91/BBM92 + the repeater chain) lives in `qne-sequence/` on a shared multi-qubit **quantum-state service**: 2-node E91 and the 3-node swapped chain both run distributed and are validated on real FABRIC hardware. Chains with more than one repeater station (n-node) are the next extension.
- QBER comes from a depolarizing polarization-misalignment model (≈ (1−F)/2) plus dark counts; `dead_time` and `timing_jitter` are now modeled in the detector (dead-time gating needs a pulse period / arrival times).
- Adversary model available for the BB84 path (`qne/eve.py`, intercept-resend, `--eve-fraction`) — measured QBER then reflects channel noise **plus** eavesdropping. A beam-splitting / PNS Eve and an Eve-on-E91 path are still open (see Phase 2b).
- Security accounting: asymptotic Shor–Preskill by default, **finite-key** (Serfling + TLGR-style length) with `--finite-key`; classical channel can be **HMAC-authenticated** with `--auth-key` (computational MAC standing in for Wegman–Carter; unauthenticated remains the default). Cascade + Toeplitz PA run on all three paths (distributed BB84, E91, raw-socket `qne/`) — both sides extract an identical secret key.
- Decoy-state runs live on the TCP transport only; the raw 0x7101 frame has no photon-count field yet (per-photon thinning happens at the source).
- Memoryless per-packet loss — no burst loss or correlated fading.
- Single wavelength, single link per run.
- P4 and Python RNGs are independent — reproducibility holds within a backend, not bit-for-bit across the P4 and Python paths.
