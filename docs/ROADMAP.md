# QFabric Roadmap

This roadmap tracks what QFabric implements today and the planned path toward a multi-protocol, multi-site quantum network emulator. It is grounded in the project's research plan (Idea 1: *Quantum Network Emulation & Simulation at Scale*).

Legend: ✅ done · 🟡 in progress / partial · ⬜ planned

---

## Status at a Glance

QFabric runs **BB84 and entanglement-based QKD (E91/BBM92) end-to-end as distributed systems on a FABRIC slice** — including a **3-node entanglement-swapping chain with heralds over a live WAN segment** — with the full key-distillation pipeline (sift → Cascade → key verification → Toeplitz privacy amplification), a security-depth stack (finite-key, authenticated channel, decoy-state accounting, biased bases, detector realism), and cross-validation against the SeQUeNCe and NetSquid simulators. The quantum channel and the quantum states are *statistical models*; the classical/herald traffic is real. See `ASSUMPTIONS.md` and `reviews/2026-09-21.md`.

| Capability | Status |
|------------|--------|
| Photon wire format (EtherType `0x7101`) + P4 fiber-loss channel (BMv2) | ✅ |
| Python QNE (Alice, Bob, detector, BB84) + classical channel (raw L2 `0x7102` on the distributed path; TCP on the raw-socket path / dev) | ✅ |
| FABRIC 3-node deployment | ✅ |
| 4-way cross-validation **on FABRIC nodes** (measured + sim + SeQUeNCe + NetSquid) | ✅ |
| Full pipeline sift → reconcile (Cascade) → amplify (Toeplitz) on all paths | ✅ |
| Entanglement (E91/BBM92) distributed over 2 nodes; CHSH > 2 across real links (emulated Werner pairs — not a physical Bell test) | ✅ |
| **Repeater chain (entanglement swapping) across 3 nodes, heralds on the live WAN** | ✅ 2026-07-13 (the single saved 3-node artifact is a QBER outlier; use the `wan_battery` replicates) |
| Security depth: finite-key (TLGR constant, reachable on both paths), authenticated channel, key verification after Cascade, decoy-state accounting, biased bases (Z-only key on both paths), intercept-resend Eve | ✅ (2026-09-21 review fixes) |
| Detector realism (efficiency, dark counts, dead time, timing jitter) | ✅ |
| Notebook workflow — `00_overview` (environment check, no slice) then `fabric/`, `sequence/`, `concepts/`; **every notebook after the overview runs on a slice** (2026-09-25) | ✅ |
| Docs: `PRIMER.md` (concepts from zero) + `CONCEPTS.md` (concept → code map) | ✅ |
| **Emulation-fidelity program: lookahead delivery + clock sync (no PTP)** — every classical message delivered at exactly `t_send + delay` in shared clock terms (BB84 timeline path, E91, repeater chains, Cascade); per-run certificate (`lookahead.certified` — only meaningful with a nonzero modeled delay; `late_events == 0` alone is vacuous); unified distance knob (`--channel-delay auto` derives delay from the same L as loss); single-site slice default | ✅ 2026-07-15 |
| **Both channels as raw L2 through the P4 switch** — classical channel off TCP onto raw EtherType `0x7102` (`--classical-transport l2`) with a reliable-datagram shim (`l2_link.ReliableLink`: seq/ack/timeout-resend/dedup + fragmentation, auth unchanged); P4 gains a `classical_channel_params` table + parser branch; P4 table-miss drop bug fixed; netem relocated to switch egress + matched by ethertype (stress only); interim orchestrator-seeded epoch (`--epoch-ns`) | ✅ live on the slice 2026-08-04 (BB84 + E91 runs with `classical_transport: l2`); the BB84 run's lookahead certificate FAILED (22/82 late at 245 µs modeled delay) — a clean certified run at nonzero delay is still to be recorded |
| **Central time authority (global timeline)** — `qne_sequence.time_authority` + `ConservativeTimeline`: LBTS grants with lookahead = channel delay and in-flight message counting; `late_events == 0` by construction; `node_runner --time-authority`, `run_sequence_bb84(time_authority=True)` starts it on Bob's node | ✅ 2026-09-21/22 — **all three protocols, validated on a live slice.** BB84 (both channels raw L2): 374/376 frames checked, 0 late, certified; wall-clock control 406 late. E91 over raw L2: 220/221 checked, 0 late, certified, CHSH 2.742; wall-clock control 220 late (max 74 ms). Repeater chain on the slice: all 3 processes certified (0 late) vs 90/19/2 late under the wall clock, identical physics (3653 swaps, CHSH 2.616). Two mechanisms — LBTS grants where a node free-runs (BB84's protocol phase), one shared per-node logical clock everywhere else. Run artifacts and the write-up are in git history at `9692ae3` (`results/timeline_2026-09-21/`); run outputs are no longer tracked |
| Tests: physics-validated unit suite + multi-process distributed suite; ruff-clean CI | ✅ |

---

## Phase 1 — P4 Quantum Channel Model ✅ (mostly)

- ✅ Fiber loss as probabilistic drop, `P(loss) = 1 − 10^(−α·L/10)`, threshold-based.
- ✅ Per-wavelength loss table; photon TX/drop counters. **Table-miss bug fixed
  (2026-07-15):** the loss branch is now gated on `quantum_channel_params.apply().hit`,
  so an unknown-wavelength photon is dropped (default action) instead of being forwarded
  out port 0 by a zero-initialized threshold.
- ✅ Classical-traffic L2 forwarding (FABRIC OVS MAC workaround).
- ✅ **Emulated classical channel in the data plane (2026-07-15, live 2026-08-04):** raw
  EtherType `0x7102` gets a parser branch + a dedicated `classical_channel_params` table
  (classify / forward / MAC-rewrite / count, no loss) — "the switch is the fiber,
  carrying both wavelengths." Ran on the slice for BB84 and E91 on 2026-08-04.
  Propagation delay stays on netem at the switch egress (BMv2 can't hold a packet) or
  the model-layer timeline. Open: the loss table is direction-blind and the counters are
  never read back (see Known Limitations).
- ⬜ Sweep figures (QBER + key rate vs distance/attenuation). The `paper/` generator was removed on 2026-09-25; `notebooks/fabric/04_all_scenarios` produces the sweep on a slice and `fabric/03_analysis` plots it. Validate measured drop rate vs analytical once a clean FABRIC sweep dataset is recorded.
- ⬜ **Timing jitter injection** in the data plane and validation against detector specs.
- ⬜ **Throughput benchmark**: sustainable photon rate / P4 processing overhead.
- ⬜ Port the model from BMv2 to **Tofino / DPDK SmartNIC** for finer timing control.
- ✅ **Global timeline (2026-09-21/22):** BB84, E91/BBM92 and the repeater chain all run on it, validated on a live slice. LBTS grants where a node free-runs (BB84's protocol phase); one shared per-node logical clock everywhere else (a station serves both its links off it). `run_sequence_e91` also gained the `--channel-delay` it never passed, so E91 slice runs no longer certify vacuously at delay 0. Optional next: wall-clock pacing (`time_scale`) on top of the grants for demos.

## Phase 2 — Quantum Node Emulator ✅ (core)

- ✅ Photon packet generation/reception over raw sockets.
- ✅ BB84 as the first protocol; detector model (efficiency, dark counts, random-basis measurement).
- ✅ Polarization fidelity modeled as a depolarizing misalignment in the detector (`polarization_error = 1 − F`), giving a realistic intrinsic QBER ≈ (1−F)/2. Used by both the sim path and the live Bob path.
- ✅ Detector realism: `dead_time` (blind window after each click, arrival-time gated) and `timing_jitter` (gaussian; clicks outside the detection window are lost — effective efficiency × erf(w/(2√2σ))) modeled in `qne/detector.py`; wired through `bob.py` (config) and `node_runner` (`--dead-time`, `--timing-jitter`, `--pulse-period-ns`).
- ✅ Consolidated the secure-key-rate math into `BB84Protocol.secure_key_fraction`, used by the sim path, the live Bob path, and the simulator adapters.
- ✅ Removed the vestigial dead code in `Alice`/`Bob._run_sifting`.
- ✅ Error correction: **Cascade reconciliation** (`qne/cascade.py`) wired into **both** the distributed E91/BBM92 and BB84 paths (shared `qne-sequence/qne_sequence/reconcile_link.py`) so Alice's and Bob's keys match bit-for-bit; `bits_leaked` tracked and subtracted in the secure-key accounting, then a Toeplitz-hash privacy-amplification extractor (`qne/privacy.py`) produces the final secret key. Finite-key sizing available via `--finite-key` (see Phase 2b).
- ⬜ GPU-accelerated density-matrix tracking for quantum-memory emulation (needed for entanglement-based protocols).

## Phase 2b — QKD security & post-processing depth ✅

All items complete (2026-07-12): the QKD paths now produce an end-to-end,
defensible secret key with the security story a reviewer expects. Remaining
follow-ons are noted inline (PNS/E91 Eve variants, decoy on the raw transport,
and the netem cost-measurement datasets).

- ✅ **Adversary model — intercept-resend Eve.** `qne/eve.py`; taps a configurable fraction `f` of photons and is wired into the distributed BB84 path (`node_runner --eve-fraction f`). Verified end-to-end: sifted QBER ≈ 0.25·f and the secure fraction collapses to 0 past the ~11% threshold (f=1 → QBER ≈ 0.25). A beam-splitting / PNS Eve and an Eve-on-E91 variant are still open. Demo: notebook `concepts/01_eavesdropper`.
- ✅ **Reconciliation on the BB84 path.** Cascade now runs on the distributed BB84 path too (`node_runner`, via `qne-sequence/qne_sequence/reconcile_link.py` — shared with E91): after the protocol's timeline finishes, Bob corrects his key toward Alice's over the same TCP link, and both report the identical key bit-for-bit. Gated on a positive secure fraction, so a run above the ~11% QBER threshold aborts instead of reconciling (verified with the intercept-resend Eve). The `qne/` raw-socket path is wired too (see the privacy-amplification item below).
- ✅ **Real privacy amplification.** `qne/privacy.py` — a seeded **Toeplitz (2-universal) hash** compresses the reconciled key to the secure length; both sides apply the same public hash and extract the identical final secret key (not just a length estimate). Wired into both distributed paths after Cascade; the reported `key` is now the amplified secret and `secure_key_bits` is its true length. The Cascade+PA driver now lives in `qne/reconcile.py` (shared; `qne-sequence/qne_sequence/reconcile_link.py` re-exports it) and the **raw-socket `qne/` path is wired too**: `bob.py` discloses only a random sample (the old everything-disclosed shortcut is gone), drives Cascade against Alice as a parity oracle over the same TCP channel, and both extract the identical amplified secret.
- ✅ **Finite-key security.** `qne/finite_key.py`: Serfling-corrected QBER upper bound + TLGR-style length ℓ = n(1 − h(Q+μ)) − leak_EC − log(ε) terms, with the *measured* Cascade leak. `--finite-key` on `node_runner` (BB84 + E91) sizes privacy amplification with the finite bound and reports `finite_key` metrics (secret_bits, qber_upper, μ) alongside the asymptotic number.
- ✅ **Authenticated classical channel.** `qne/auth.py`: per-frame HMAC-SHA256 tag + strictly-sequential anti-replay sequence numbers under a pre-shared key (the Wegman–Carter interface, computational MAC), on **both** transports — `qne/channel.py` (raw-socket path, `--auth-key` on the CLI) and the qne-sequence `Link` (`node_runner --auth-key`). Tampered/replayed/spliced frames tear the connection down; `auth_failures` reported. Measuring auth cost vs WAN RTT on FABRIC is the remaining experiment.
- ✅ **Decoy on the live transport.** Alice's `DistributedBB84` emits per-pulse intensities (signal/decoy/vacuum, Poisson photon numbers, per-photon fiber thinning); Bob detects pulses with 1−(1−η)^n (+ darks on empty pulses); measured per-intensity gains/QBERs feed `qne/decoy.py`'s Ma–Qi–Zhao–Lo/GLLP analysis unchanged; key comes from signal pulses only. `node_runner --decoy --mu-signal/--mu-decoy/--mu-vacuum --decoy-probs`. TCP transport only (the 0x7101 frame has no photon-count field yet).
- ✅ **Efficient (biased-basis) BB84.** `--basis-bias p` (and `protocol.basis_bias` in scenario YAML): both sides pick Z with probability p, sift ratio p²+(1−p)² > 50%; key from Z–Z matches, ALL X–X matches disclosed for the phase-error estimate, rate 1 − h(e_z) − h(e_x) (`BB84Protocol.efficient_secure_fraction`). On both the distributed and raw-socket paths.
- ✅ **Detector realism.** `dead_time` / `timing_jitter` modeled (see Phase 2 above).
- ✅ **Demo notebook.** `concepts/04_qkd_security` (on the slice): finite-key rate-vs-block-size curves + a live `--finite-key` run, auth tamper/replay demos + an authenticated run, biased-basis sift ratio + efficient rate, and the live decoy pipeline with measured gains vs the weak-coherent model.

## Phase 3 — Cross-Validation ✅ (core)

- ✅ Platform-neutral `ValidationScenario`; standalone `--json` adapters; on-node + subprocess runners.
- ✅ **4-way comparison on the FABRIC slice**: measured QFabric (BMv2) + QFabric-sim (switch) + SeQUeNCe (alice/3.12) + NetSquid (bob), driven by `run_cross_validation_on_fabric`.
- ✅ Native engines: SeQUeNCe 1.0 (`pair_bb84_protocols` + KeyManager) and NetSquid (qubits + `DepolarNoiseModel`).
- ✅ Statistically-correct agreement test (combined-variance, `qber_sample_bits`-aware) + honest SKIPPED/INCONCLUSIVE reporting.
- ✅ The **live BMv2/socket measurement** is the QFabric data point in the comparison (not just the sim).
- 🟡 Confirm SeQUeNCe/NetSquid versions on your slice (deadsnakes 3.12 build + netsquid.org creds).
- 🟡 Quantify where **real classical-network effects** (latency, jitter, congestion) make QFabric diverge from ideal-channel simulators — the core scientific contribution. Scaffolding done: `apply_classical_netem` (TCP:5100 on the endpoints, or — for the L2 path — the switch egress ports matched by EtherType `0x7102`, relabeled as stress), `run_network_conditions_experiment`, and notebook `fabric/05_network_effects` (throughput / time-to-key / QBER vs condition). The realistic headline point is the model-layer delay (`--channel-delay auto`), not netem. Needs a recorded FABRIC dataset across conditions/sites.
- ⬜ Publish the cross-validation **dataset**.

## Phase 4 — Scale-Up Experiments 🟡

- ✅ Multi-hop **quantum repeater chain (3 nodes)** — in-process, 3-process-distributed, AND **validated on a live FABRIC slice (2026-07-13)**.
  - In-process: `qstate_core.bell_measure` (BSM/swap op, group merge, heralded X^m2·Z^m1 Pauli correction) + `qne-sequence/qne_sequence/repeater.py` (n-node local chain, per-link loss, BBM92/CHSH end-to-end measurement, `python -m qne_sequence.repeater`). Validated against the Werner-chain law F = (1+3·f^L)/4 / QBER = (1−f^L)/2 / S = 2√2·f^L, plus the no-correction control (QBER → 0.5: the herald channel is load-bearing).
  - Distributed: `distributed_repeater.py` — THREE processes over three TCP links (`node_runner --protocol repeater --role alice|repeater|bob`, per-link `--bob-host`/`--repeater-host`). Alice hosts the register + creates both link pairs; the repeater process performs the BSMs via RPC and forwards the heralds to Bob over its OWN link; Bob applies the corrections and runs the full BBM92/E91 tail (sift, QBER sample, CHSH, Cascade+PA, `--finite-key`, `--auth-key` on all three links). Verified over loopback: Werner-law QBER, CHSH > 2 across the swapped chain, identical extracted secrets, and the no-correction control.
  - **Live WAN validation (2026-07-13):** the station ran on the switch node, physically between the endpoints; Alice and Bob extracted an **identical secret key** (Cascade-reconciled + Toeplitz-amplified) with the station performing thousands of entanglement swaps and the heralds crossing a real ~25 ms WAN segment on their own connection. Driven both manually and end-to-end from notebook `concepts/03_repeater`; per-run artifacts land in `results/fabric_repeater_*.json` (gitignored run outputs).
  - Deployment: `deploy_fabric.setup_repeater_bridge` — hardened by the live debug: stops the **bmv2 Docker container** (pkill can't), `modprobe bridge` (fresh VMs lack the module), Linux bridge `br-qne` carrying station IP 10.10.1.3 with the **alice-side port MAC** (FABRIC/OVS MAC-learning workaround), kernel forwarding + `FORWARD ACCEPT` + `rp_filter/send_redirects` off, static ARP on all three nodes; pings all three links and **raises** on failure. `run_sequence_repeater` launches the three roles; `setup_sequence_runtime(nodes=("alice","bob","switch"))` builds the runtime on the station. Mutually exclusive with BMv2 (restore via `configure_switch`). Notebooks: `12_repeater` (local, committed) + `12_repeater_fabric` (slice variant, gitignored).
  - ✅ **n-node chains (2026-07-13):** `distributed_repeater.py` generalized to K stations (K+2 nodes, K+1 links; station i listens on port+2i−1, bob on port+2i; `--num-stations/--station-index/--repeater-hosts`). Each station is oblivious to its siblings; Bob XOR-composes the K herald streams into one Pauli correction. Loopback tests: 4-node vs the L=3 law, 5-node CHSH violation (S≈2.50 across five processes), no-herald control. **4-node chain validated over the live WAN** (two station processes co-located on the switch node): QBER 6.8% vs law 7.1%, identical keys. `run_sequence_repeater(num_stations=K)` launches co-located stations; distinct-site stations need a bigger slice topology.
  - NEXT: multi-site topologies (one station per intermediate site — needs new slice layouts), 5–10 sites, herald-latency dataset via `apply_classical_netem` (first sweep measured: time-to-key 14→42 s for +0→100 ms segment RTT; see `results/wan_battery_2026-07-13/`).
- ⬜ Entanglement distribution under **baseline / congested / asymmetric-latency / link-failure** conditions.
- ⬜ Measure entanglement-fidelity degradation per condition.
- ⬜ WDM: multiple wavelengths per link (loss table is already wavelength-keyed).

## Phase 5 — Packaging & Reproducibility ⬜

- ⬜ Parameterized experiment templates (the Kiso config was removed 2026-09-24; `scripts/deploy_fabric.py` is the supported path).
- ✅ Containerized BMv2 toolchain (`docker/Dockerfile.bmv2`, Ubuntu-based) + GHCR publish workflow; switch can pull a prebuilt image instead of building from source (`QFABRIC_BMV2_IMAGE`).
- ⬜ One-click parameterized topology template.
- ✅ Artifact packaging enforces its own guarantee: `scripts/package_artifact.sh` excludes `results/` at any depth plus `CLAUDE.md`, `paper/`, `secrets/`, `.env*` and `*.log`/`*.pcap`, then re-inspects the finished tarball and fails the build if any slipped in (`tar` does not read `.gitignore`).
- ⬜ Artifact submission for reproducibility evaluation.

---

## Phase 6 — Distributed Quantum Computing 🟡

Entanglement as a *compute* resource, not only a key resource: two QPUs that share
no qubit run one circuit by consuming Bell pairs and classical bits — the same two
currencies the rest of the platform already measures.

- ✅ **Circuit-capable quantum layer** (`qne-sequence/qne_sequence/qstate_qiskit.py`).
  `QiskitRegister` is a drop-in for `qstate_core.QStateRegister` (same interface, so
  `RemoteQuantumManager` and everything above it are untouched — the argument
  `qstate_sequence.py` made for SeQUeNCe) backed by `qiskit.quantum_info.Statevector`,
  plus the one thing a computation needs and a QKD register lacks: `apply_circuit`,
  which runs an ordinary `QuantumCircuit` over a node's local qubits. No Aer needed.
  `qstate_core` gained `alloc` / `apply_gate` / `statevector` so both backends carry
  the primitives. Endianness (qfabric groups are MSB-first, Qiskit is little-endian)
  is pinned by a CNOT truth-table test on both.
- ✅ **Primitives** (`qne-sequence/qne_sequence/dqc.py`), each split into the steps one
  node executes locally with the classical bits returned explicitly, so the
  distributed runner only has to put those bits on the wire:
  - `teleport_send` / `teleport_recv` — 1 pair + 2 bits, A→B. The send step is exactly
    `bell_measure(data, epr_a)`: swapping *is* teleporting half a pair.
  - `telegate_cnot_send` / `_apply` / `_finish` — non-local CNOT, 1 pair + 1 bit each
    way, control stays put. This is the primitive a distributed compiler emits.
- ✅ **Validated on both backends, shot for shot** (`tests/test_dqc.py`):
  exact transfer / truth table at w=1; telegate on |+⟩|0⟩ produces Φ+ (correlated in
  Z *and* X — coherent, not a classical copy); **error rate (1−w)/2 for teleport, and
  for the telegate's bit and phase channels alike — the same curve as the E91 QBER**;
  a permanently lost correction bit costs 50% on the single Pauli channel it
  protects, while a *late* one is recovered exactly by folding it into the outcome
  (tracked Pauli frame) — so herald latency is a memory-time cost, not an error
  source. `run_dqc_session` models the two regimes separately (`--drop` vs `--late`)
  and validates its inputs rather than clamping them.
- ✅ **Distributed over a real link** (`qne-sequence/qne_sequence/distributed_dqc.py`,
  `node_runner --protocol dqc`). Alice is the control node and register authority;
  bob is the target node and holds no local register, so his half of each gate is an
  op over the wire — the same seam the repeater station uses for its BSMs. The
  corrections are genuine protocol messages (m1 in `PLAN`, m2 in `ACK`) and **each
  side applies the value as it arrived**, not one it could have computed locally,
  which is what makes `--dqc-drop` a real control rather than a simulated one. A
  *deferred* correction travels too, as an explicit Pauli frame (`frame_m1` /
  `frame_m2`) built from the bits the peer actually received — never a local copy,
  or a corrupted wire value would be repaired out of clean local state and
  `--dqc-late` would claim a recovery that never happened
  (`tests/test_dqc_wire_integrity.py` corrupts bits in flight to pin this).
  `QuantumStateService` gained `alloc_batch` / `telegate_apply_batch` /
  `teleport_recv_batch` / `discard_pair`. Both primitives, both Pauli channels,
  `--dqc-drop` and `--dqc-late`, HMAC auth, fiber loss and the global timeline all
  work; the raw-L2 classical backend is selectable but not yet exercised on a slice.
  `deploy_fabric.run_sequence_dqc` and `notebooks/concepts/05_distributed_computing`
  drive it from a slice, but **neither has been run against FABRIC hardware yet**.
  Validated in `tests/test_two_node_dqc.py` (two processes over loopback):
  exact gate at w=1, (1−w)/2 at w=0.9 on both channels, each dropped bit breaking
  exactly the one channel it protects, every late bit recovered, loss costing gates
  rather than fidelity, and the run **certified on the global timeline** at a 100 km
  modeled delay (0 late events, sim-elapsed 3.92 ms) where the wall-clock control
  is not certified.
- ⬜ **Run it on the slice.** The deploy path exists (`run_sequence_dqc`, notebook
  `concepts/05_distributed_computing`) but has never talked to FABRIC; the protocol
  itself is validated over loopback only. Raw L2 through the P4 switch
  (`--classical-transport l2`) is the target, the way BB84 and E91 already run.
- ⬜ **Decohere the pair while it waits.** Fidelity is static today — it does not decay
  over the herald's flight time. `F(t) = F₀·e^(−t/T₂)` keyed on the *measured* WAN
  latency is the highest-value addition: at ~25 ms RTT against ms-scale memory
  coherence, the pair dies before the correction bit lands, and quantifying that
  crossover is the platform's own result rather than a simulator's.
- ⬜ **A split application**: a circuit cut across two sites (e.g. 4-qubit QFT or
  Grover, 2+2), reporting success probability and time-to-result against *measured*
  pair rate, hop count and latency.
- ⬜ Entanglement distillation to trade pairs for fidelity once the above shows the
  fidelity floor.

---

## Protocol Backlog

Priority order from the research plan:

| Protocol | Status | Notes |
|----------|--------|-------|
| **BB84 QKD** | ✅ | Prepare-and-measure baseline (`qne/`, `qne-sequence/`) |
| **Decoy-state BB84** | 🟡 | PNS-*aware* key-rate accounting (no PNS adversary is modeled; the bounds are asserted, not attacked) (`qne/decoy.py`): weak-coherent Poisson source, 3 intensities, full Ma–Qi–Zhao–Lo Y1/e1 bounds → GLLP secure key rate; sweep + figure via `scripts/decoy_sweep.py`. **Runs on the live transport** (`node_runner --decoy`: real per-pulse photon numbers, measured per-intensity gains/QBERs feed the analysis); TCP transport only — the raw 0x7101 frame has no photon-count field yet. |
| **E91 / BBM92 QKD** | ✅ | Entanglement-based QKD on the shared quantum-state service (`qne-sequence/qne_sequence/qstate_core.py`, `e91.py`), running **distributed over 2 nodes** (`distributed_e91.py`, `remote_qm.py`; `--protocol e91\|bbm92`). Werner-state model ties QBER=(1−F)/2 and CHSH S=2√2·F; Bell-test coordination + basis/sample disclosure ride the real link; sift/QBER reuse `BB84Protocol`. |
| **Entanglement swapping** (repeaters) | ✅ (3-node) | BSM swap op + heralded correction validated in-process (`repeater.py`), across 3 processes (`distributed_repeater.py`), and **on a live FABRIC slice (2026-07-13)** — identical keys over a swapped chain with heralds on a real WAN segment. n-node chains (>1 station) are next. |
| **Quantum teleportation** | ✅ | `dqc.teleport_*` — the send step IS `bell_measure`, so the validated repeater code already contained it. Exact state transfer at w=1; error (1−w)/2 at Werner weight w. 1 pair + 2 classical bits. Distributed over a link via `--protocol dqc --dqc-primitive teleport`; loopback-validated, not yet run on a slice. |
| **Non-local CNOT (telegate)** | ✅ | `dqc.telegate_cnot_*` — cat-entangler / cat-disentangler (Eisert et al. 2000). 1 pair + 1 bit each way; the control stays on its node. Bit *and* phase error both (1−w)/2. Distributed over a link via `--protocol dqc` (default); loopback-validated, not yet run on a slice. |

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

*(Updated by the 2026-09-21 review — see `reviews/2026-09-21.md` for what was fixed.)*

- The `qne/` hand-coded path models photons at the bit/basis level (no entanglement). Entanglement (E91/BBM92 + the repeater chain) lives in `qne-sequence/` on a shared multi-qubit **quantum-state service**: 2-node E91 and the 3-node swapped chain both run distributed and are validated on real FABRIC hardware. Chains with more than one repeater station (n-node) are the next extension.
- QBER comes from a depolarizing polarization-misalignment model (≈ (1−F)/2) plus dark counts; `dead_time` and `timing_jitter` are now modeled in the detector (dead-time gating needs a pulse period / arrival times).
- Adversary model available for the BB84 path (`qne/eve.py`, intercept-resend, `--eve-fraction`) — measured QBER then reflects channel noise **plus** eavesdropping. A beam-splitting / PNS Eve and an Eve-on-E91 path are still open (see Phase 2b).
- Security accounting: asymptotic Shor–Preskill by default, **finite-key** (Serfling + TLGR-style length) with `--finite-key`; classical channel can be **HMAC-authenticated** with `--auth-key` (computational MAC standing in for Wegman–Carter; unauthenticated remains the default). Cascade + Toeplitz PA run on all three paths (distributed BB84, E91, raw-socket `qne/`) — both sides extract an identical secret key.
- Decoy-state runs live on the TCP-descriptor transport only, with per-photon fiber thinning at the source (`loss_where = none`) — the P4 switch is **bypassed**. The raw 0x7101 frame has no photon-count field yet. **Decoy has never run through the emulated fiber**, and there is no PNS Eve; treat decoy as source-realism + honest accounting.
- P4 loss table is direction-blind (keyed on wavelength only; the single entry hard-codes egress port 1) — fine while photons flow Alice→Bob only. P4 counters are never read back, so the switch's own drop count has not been compared with the table threshold on a live run.
- Cross-validation backends do not share every assumption: the SeQUeNCe adapter ignores `num_photons`/`sample_fraction` and uses a weak-coherent μ = 0.1 source; `detection_window` is not a scenario field.
- No measured distance curve is on record. The 2026-07-03 sweep was frozen by the switch-loss bug; that is fixed, but run outputs are no longer tracked (2026-09-25) so the curve must be re-recorded on a slice via `notebooks/fabric/04_all_scenarios`.
- Memoryless per-packet loss — no burst loss or correlated fading.
- Single wavelength, single link per run.
- P4 and Python RNGs are independent — reproducibility holds within a backend, not bit-for-bit across the P4 and Python paths.
