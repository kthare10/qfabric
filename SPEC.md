# QFabric Technical Specification

This document specifies the data formats, models, and protocol flow that QFabric implements. It reflects the current implementation in `qne/`, `p4/`, and `validation/`.

- **Status**: v0.1.0 — BB84 QKD over a single emulated link
- **Audience**: contributors, reviewers, and anyone reproducing or extending the platform
- **Companion docs**: [`README.md`](README.md) (usage), [`ROADMAP.md`](ROADMAP.md) (plan)

---

## 1. System Overview

QFabric emulates a quantum link with three roles:

```
  Alice (qne/alice.py)        P4 switch (p4/bmv2)        Bob (qne/bob.py)
  ─────────────────────       ─────────────────────      ─────────────────────
  generate random             probabilistic photon       apply detector model
  (basis, state) photons      drop = fiber loss          (efficiency, dark counts,
  send raw 0x7101 frames  ──► forward survivors      ──► random-basis measurement)
                              L2-forward classical
  ◄───────────────  classical channel: TCP/JSON sifting  ───────────────►
```

The **quantum channel** is the photon path through the P4 switch (lossy, one-way).
The **classical channel** is a standard TCP connection used for BB84 post-processing. On FABRIC this rides a real WAN link, which is the central research lever: it injects real latency/jitter/congestion into the classical-quantum feedback loop.

A pure-Python **simulation mode** (`validation/run_qfabric.py`) reproduces the same loss/detector/BB84 logic without BMv2 or raw sockets, for cross-validation and CI.

---

## 2. Photon Wire Format

Photons travel as custom Ethernet frames. Defined in `qne/photon.py` and mirrored in `p4/bmv2/includes/headers.p4`.

### 2.1 Ethernet header (14 bytes)

| Field | Bytes | Value |
|-------|-------|-------|
| Destination MAC | 6 | configurable (default `02:00:00:00:00:02`) |
| Source MAC | 6 | configurable (default `02:00:00:00:00:01`) |
| EtherType | 2 | `0x7101` (photon) |

### 2.2 Photon header (17 bytes)

Packed as `struct` format `!4B3IB`:

| Field | Type | Description |
|-------|------|-------------|
| `version` | u8 | Protocol version (`0x01`) |
| `basis` | u8 | `0` = Z (rectilinear), `1` = X (diagonal) |
| `state` | u8 | `0` = \|0⟩/\|+⟩, `1` = \|1⟩/\|−⟩ — the classical bit value |
| `wavelength` | u8 | Channel tag (keys the loss table; enables future WDM) |
| `sequence_num` | u32 | Monotonic photon identifier |
| `timestamp_hi` | u32 | TX timestamp, upper 32 bits (picoseconds) |
| `timestamp_lo` | u32 | TX timestamp, lower 32 bits (picoseconds) |
| `padding` | u8 | Reserved |

Frames are zero-padded to the 60-byte Ethernet minimum. `EtherType 0x0800` (IPv4) is reserved in the P4 headers for classical traffic.

---

## 3. Quantum Channel Model (P4)

Implemented in `p4/bmv2/quantum_channel.p4` for the BMv2 V1Model.

### 3.1 Fiber loss

Fiber attenuation is modeled as probabilistic packet drop:

```
P(loss) = 1 − 10^(−α·L/10)
```

where `α` = attenuation (dB/km) and `L` = distance (km). The probability is pre-computed off-switch into a 32-bit threshold:

```
threshold = floor(P(loss) · 2³²)
```

installed in the `quantum_channel_params` table keyed by `wavelength`. Per photon:

1. Draw `random_value ∈ [0, 2³²)` via the P4 `random` extern.
2. Increment `photon_tx_counter[wavelength]`.
3. If `random_value < threshold` → drop the photon (lost in fiber), increment `photon_drop_counter[wavelength]`.
4. Otherwise forward to the egress port and rewrite src/dst MAC.

The table action `set_channel_params(threshold, port, src_mac, dst_mac)` carries the egress port and MAC rewrite. Default action drops, so unconfigured wavelengths are fully attenuated.

### 3.2 Classical traffic forwarding

Non-photon packets are forwarded by the `port_forwarding` table keyed on `ingress_port` (a bidirectional pipe), rewriting the source MAC to the switch port's own MAC. This is a deliberate workaround for FABRIC's OVS dropping frames with unknown destination MACs (MAC-learning issue). Default action drops.

### 3.3 Pipeline

`PhotonParser → PhotonVerifyChecksum → PhotonIngress → PhotonEgress (pass-through) → PhotonComputeChecksum → PhotonDeparser`. All channel logic lives in ingress; egress is a no-op.

---

## 4. Quantum Node Models (Python)

### 4.1 Alice — photon source (`qne/alice.py`)

For each of `num_photons` slots, draws `basis` and `state` uniformly at random, builds a `PhotonPacket`, and transmits it over an `AF_PACKET` raw socket bound to the egress interface. Optional rate limiting via `send_rate_hz`. Records each `(sequence_num, basis, bit_value)` in `sent_log`, then connects to Bob for sifting.

### 4.2 Detector model (`qne/detector.py`)

Per incoming photon:

1. Bob chooses a measurement basis uniformly at random.
2. **Basis match** → measured bit = photon's `state`, **unless** polarization imperfection depolarizes it: with probability `polarization_error` the outcome is randomized. This is the intrinsic QBER source.
   **Basis mismatch** → measured bit is random (50/50).
3. Apply **detection efficiency**: detected with probability `efficiency`.
4. **Dark count**: if not detected, fire with probability `dark_count_rate · detection_window`, yielding a random bit. Dark counts are flagged.

`polarization_error` is derived from the channel's `polarization_fidelity` F as `1 − F`; on a matched basis it contributes an intrinsic QBER ≈ `(1 − F)/2` (depolarizing model). Both the sim path (`run_qfabric`) and the live `Bob` set it this way.

Config knobs: `efficiency`, `dark_count_rate` (Hz), `detection_window` (s; default 1 ns), `polarization_error`. `dead_time` and `timing_jitter` exist in config but are not yet modeled (see ROADMAP).

### 4.3 Bob — receiver (`qne/bob.py`)

Listens on a raw socket (30 s idle timeout), parses photon frames, applies the detector, and logs detections only (losses produce no record). Then runs the classical sifting exchange as TCP server.

---

## 5. BB84 Protocol Flow

Classical post-processing logic lives in `qne/bb84.py`; the live message exchange is split across `qne/alice.py` and `qne/bob.py` over `qne/channel.py`.

### 5.1 Classical channel transport (`qne/channel.py`)

Length-prefixed JSON over TCP: `[4-byte big-endian length][UTF-8 JSON]`. `ClassicalServer` (Bob) listens with IPv4/IPv6 dual-stack; `ClassicalClient` (Alice) connects with retries (default 30 attempts × 2 s) to tolerate Bob still draining photons.

### 5.2 Message sequence

| # | Direction | `type` | Payload |
|---|-----------|--------|---------|
| 1 | Alice → Bob | `alice_bases` | `{seq: basis}` for all sent photons |
| 2 | Bob → Alice | `sifting_result` | `matching_indices`, `detected_sequences` |
| 3 | Bob → Alice | `request_sample` | `matching_indices` to compare |
| 4 | Alice → Bob | `alice_sample_bits` | Alice's bit values at those indices |
| 5 | Bob → Alice | `qber_result` | `qber`, `confidence_interval`, `num_sampled`, `num_errors`, `raw_key_rate`, `secure_key_rate`, `final_key_bits` |

### 5.3 Sifting

Keep only positions where (a) Bob detected the photon and (b) Alice's and Bob's bases match. `BB84Protocol.sift` indexes Bob's detections by `sequence_num` and intersects with Alice's log.

### 5.4 QBER estimation

Sample a `sample_fraction` (default 0.1) of sifted positions, count mismatches, and report `qber = errors / num_sampled`. A 95% **Wilson score** confidence interval is computed. Sampled bits are considered consumed (removed from key material).

### 5.5 Secure key rate

Asymptotic Shor–Preskill bound per sifted bit:

```
r = max(0, 1 − 2·H₂(QBER))      for QBER < 0.11
r = 0                            for QBER ≥ 0.11   (BB84 security threshold)
```

where `H₂` is binary entropy. Then:

- `raw_key_rate    = sifted_bits / num_photons_sent`
- `final_key_bits  = floor((sifted_bits − num_sampled) · r)`
- `secure_key_rate = final_key_bits / num_photons_sent`

> **Implementation note**: full QBER/key-rate is computed in two places — `BB84Protocol` (used by simulation mode) and inline in `Bob._run_sifting` (used by the live socket path). These must be kept consistent; consolidating them is tracked in ROADMAP.

---

## 6. Configuration & Scenarios

### 6.1 `ScenarioConfig` (`qne/config.py`)

Nested YAML consumed by the live Alice/Bob path:

```yaml
name: baseline_1km
channel:
  distance_km: 1.0
  attenuation_db_per_km: 0.2
  polarization_fidelity: 1.0      # parsed; not yet modeled
detector:
  efficiency: 0.8
  dark_count_rate: 10.0           # Hz
  dead_time: 0.0                  # parsed; not yet modeled
  timing_jitter: 0.0              # parsed; not yet modeled
protocol:
  num_photons: 100000
  send_rate_hz: 1000000.0
  sample_fraction: 0.1
  wavelength: 0
seed: 42
```

Derived properties: `loss_probability` and `loss_threshold_u32` (the value installed in the switch's `quantum_channel_params` table by `deploy_fabric.py` / notebook `01_setup_slice`).

### 6.2 `ValidationScenario` (`validation/scenario.py`)

A platform-neutral, flat scenario used by the cross-validation framework. `from_yaml` accepts **both** the nested `ScenarioConfig` layout and a flat layout, so one YAML file can drive every backend. `load_sweep` expands a `sweep:` block:

```yaml
sweep:
  parameter: distance_km
  values: [1, 5, 10, 20, 50, 100]
base:
  attenuation_db_per_km: 0.2
  detector_efficiency: 0.8
  num_photons: 100000
  sample_fraction: 0.1
  seed: 42
```

Provided scenarios: `baseline_1km`, `fabric_1km`, `quick_test`, `sweep_distance`, `sweep_attenuation`.

---

## 7. Cross-Validation Framework

The cross-validation compares BB84 QBER and secure key rate across **four backends for the same scenario, all executed on the FABRIC slice** (notebook `03_cross_validation` → `deploy_fabric.run_cross_validation_on_fabric`):

| Backend | Runs on | Source of QBER |
|---------|---------|----------------|
| `qfabric` | BMv2 data plane (the `02_run_experiment` run) | measured emulation, loaded from `results/fabric_bob_results.json` |
| `qfabric_sim` | switch node (`.venv-qsim`) | `run_qfabric.py` — same `Detector`+`BB84Protocol` code as the live path, no traffic |
| `sequence` | alice node (`.venv-seq`, Python 3.12) | `run_sequence.py` — SeQUeNCe 1.0 native QKD stack (`pair_bb84_protocols`, real QuantumChannels with `polarization_fidelity`) |
| `netsquid` | bob node (`.venv-nsq`) | `run_netsquid.py` — NetSquid real qubits + `DepolarNoiseModel` |

- **Execution model**: each adapter runs standalone as `python -m validation.run_<backend> scenario.yml --json -`, emitting a sentinel-wrapped `ValidationResult`. `compare.run_backend_on_node` invokes it on the node via `fablib` and parses the result back; `compare.run_backend_subprocess` does the same locally via a per-backend interpreter (`QFABRIC_SEQUENCE_PYTHON` / `QFABRIC_NETSQUID_PYTHON`). SeQUeNCe 1.0 (Python ≥3.12) and NetSquid (3.10/3.11) can't share an interpreter, hence different nodes/venvs.
- **Honest reporting**: `backend_status()` classifies each result as `ok` / `unavailable` / `no_data`; only `ok` backends are compared. Missing libraries or API drift surface as **SKIPPED** with the error — never a fake pass. With fewer than two `ok` backends the run reports **INCONCLUSIVE**.
- **QBER for comparison** is taken over the **full sifted key** (not the 10% protocol sample) for `qfabric_sim`/`sequence`/`netsquid`, so the comparison reflects the physics model rather than estimator noise. The measured `qfabric` point keeps its protocol-sample QBER; each result records `qber_sample_bits` (the N its QBER was estimated from).
- **Agreement test**: two backends agree when `|QBER_a − QBER_b| < num_sigma · √(p̄(1−p̄)(1/N_a + 1/N_b))` — the standard error of the *difference* of two independent estimates, using each backend's `qber_sample_bits` (default `num_sigma = 2`). This correctly widens the bound for the sample-based FABRIC point. A simulator-vs-simulator DIFFER reflects a genuine model difference, not an error.
- **Output**: per-pair AGREE/DIFFER with ΔQBER and tolerance; notebook 03 saves all results to `results/cross_validation.json`, which `04_analysis` loads to plot QBER and key rate across all backends. Parameter sweeps additionally produce QBER/key-rate-vs-parameter plots via matplotlib (`--plot out.png`).
- **Test-suite baseline**: `validation/reference_bb84.py` is an independent *analytic* BB84 model used only by the tests to sanity-check the QFabric emulator — it is **not** a simulator backend.

---

## 8. Metrics

`qne/metrics.py` collects `ExperimentMetrics`: photons sent/received/lost, dark counts, sifted bits, QBER (+ CI), raw/secure key rate, final key bits, elapsed time, and the full scenario config. Derived `loss_rate` and `detection_rate`. Serializable to/from JSON; FABRIC runs write `results/fabric_alice_results.json` and `results/fabric_bob_results.json`.

---

## 9. Invariants & Assumptions

- Photons are modeled at the **bit/basis level**, not as full quantum states — sufficient for prepare-and-measure BB84, not for entanglement or multi-qubit protocols.
- Optics are **ideal** on a basis match (no polarization/phase error injection yet); QBER in the baseline therefore approaches 0 except for dark-count and finite-sampling noise.
- The loss model is **memoryless and per-packet**; it captures average attenuation, not burst loss or correlated fading.
- One **wavelength / one link** per run. Multi-hop and WDM are designed-for (header fields exist) but not implemented.
- The P4 `random` extern and the Python `numpy` RNG are independent; reproducibility holds **within** a backend (fixed `seed`), not bit-for-bit **across** the P4 and Python paths.
