# QFabric Technical Specification — wire formats, tables, protocol messages

- **Status**: 2026-09-21 — reflects `qne/`, `qne-sequence/`, `p4/`, `validation/` as implemented
- **Audience**: contributors, reviewers, anyone reproducing or extending the platform
- **Companion docs**: [`README.md`](README.md) (usage), [`ASSUMPTIONS.md`](ASSUMPTIONS.md) (what is modeled), [`CONCEPTS.md`](CONCEPTS.md) (physics → code), [`ROADMAP.md`](ROADMAP.md) (status)

This document is deliberately limited to the things a second implementation would need to
interoperate: frame layouts, P4 tables, the classical message sequence, and the key-rate
formulas. Narrative and modeling assumptions live in the companion docs.

---

## 1. Roles and channels

```
 Alice ──0x7101 photon frames──►  BMv2 P4 switch  ──►  Bob
        ◄──0x7102 classical────►  ("the fiber")   ◄──►
   control plane (state-service RPC, timeline handshake): TCP — emulator bookkeeping, not an emulated channel
```

| Channel | Frames | Switch behaviour | Transport options |
|---|---|---|---|
| Quantum | `0x7101` photon descriptors | per-wavelength probabilistic drop (fiber loss), MAC rewrite, counters; **table miss = drop** | raw L2 (slice); TCP descriptor batches (dev, `qne-sequence --quantum-transport tcp`) |
| Classical | `0x7102` reliable datagrams | forward + count, **never loss** (`classical_channel_params`) | raw L2 (`--classical-transport l2`); TCP (`qne/` path, dev) |

## 2. Photon frame (EtherType `0x7101`) — `qne/photon.py`, `p4/bmv2/includes/headers.p4`

Ethernet header (14 B: dst MAC, src MAC, `0x7101`) followed by the photon header, `struct` format `!4B3IB` (17 B):

| Field | Type | Meaning |
|---|---|---|
| `version` | u8 | `0x01` |
| `basis` | u8 | `0` = Z (rectilinear), `1` = X (diagonal) |
| `state` | u8 | `0` = \|0⟩/\|+⟩, `1` = \|1⟩/\|−⟩ — the classical bit |
| `wavelength` | u8 | keys the loss table (WDM hook; single wavelength today) |
| `sequence_num` | u32 | monotonic photon id; the sifting key on both sides |
| `timestamp_hi/lo` | 2×u32 | TX time, ps (informational — Bob never compares clocks) |
| `padding` | u8 | reserved |

Frames are padded to the 60-byte Ethernet minimum. **There is no photon-count field**: decoy-state runs (Poisson photon numbers) therefore use the TCP descriptor transport with thinning at the source and do not traverse the P4 loss table (open item).

## 3. Classical frame (EtherType `0x7102`) — `qne-sequence/qne_sequence/l2_link.py`

`ReliableLink` header `!2sBBQHHI` (20 B) after the Ethernet header:

| Field | Type | Meaning |
|---|---|---|
| `magic` | 2 s | `b"QL"` |
| `version` | u8 | `2` (v2 added `payload_len`; any other version is dropped) |
| `type` | u8 | 1 DATA, 2 ACK, 3 HELLO, 4 HELLO_ACK |
| `seq` | u64 | message sequence |
| `frag_index`, `frag_count` | u16, u16 | fragmentation (default fragment 1400 B) |
| `payload_len` | u32 | bytes of payload actually carried — strips Ethernet min-frame padding; a frame declaring more than it carries is **dropped, not ACKed** (`short_frames` counter) |

Semantics: every DATA fragment is ACKed independently and retransmitted on timeout (20 ms, 50 retries); complete messages are delivered strictly in order, duplicates ACKed but never delivered twice. `qne/auth.py` (HMAC-SHA256 tag + strictly sequential anti-replay counter under a pre-shared key) seals the *payload bytes* and is transport-agnostic. The `qne/` TCP path uses `[u32 length][UTF-8 JSON]` framing with `MAX_FRAME_BYTES = 64 MiB`.

## 4. P4 program — `p4/bmv2/quantum_channel.p4`

| Table | Key | Action | Notes |
|---|---|---|---|
| `quantum_channel_params` | `wavelength` | `set_channel_params(threshold, port, src_mac, dst_mac)`; default `drop_photon` | per photon: `random(0, 2³²−1) < threshold` → drop (+`photon_drop_counter`); else rewrite MACs, forward (+`photon_tx_counter`) |
| `classical_channel_params` | `ingress_port` | `classical_forward(port, src_mac, dst_mac)` (+`classical_fwd_counter`) | `0x7102`; **no loss path** |
| `port_forwarding` | `ingress_port` | `port_forward(...)` | all other EtherTypes (control plane, ARP, IP) |

`threshold = floor(P(loss)·2³²)` clamped to `2³²−1`, with `P(loss) = 1 − 10^(−α·L/10)` (`qne.config.ScenarioConfig.loss_threshold_u32`). Distance sweeps change the entry **in place** (`deploy_fabric.set_channel_loss` → `table_modify`); restarting the switch per point is the historical "loss freeze" bug. Known gaps: the loss table is direction-blind (single entry, egress port hard-coded) and the counters are not read back by any tool.

## 5. Classical message sequence

### 5.1 `qne/` raw-socket BB84 (`qne/alice.py`, `qne/bob.py`, `qne/reconcile.py`)

| # | Dir | `type` | Payload |
|---|---|---|---|
| 1 | A→B | `alice_bases` | `{seq: basis}` for every sent photon |
| 2 | B→A | `sifting_result` | `matching_indices` (sorted, basis-matched detections), `detected_sequences` |
| 3 | B→A | `request_sample` | positions to disclose: a `sample_fraction` random subset of matches; with `basis_bias ≠ 0.5`, **all X–X matches plus a Z–Z sample** |
| 4 | A→B | `alice_sample_bits` | Alice's bits at those positions |
| 5 | B→A | `qber_result` | `qber`, `confidence_interval` (Wilson 95 %), `num_sampled`, `num_errors`, `raw_key_rate`, `secure_key_rate`, `final_key_bits`, `reconcile` (bool) |
| 6…| B⇄A | `PARITY_REQ` / `PARITY_RESP` | Cascade: Bob sends index blocks, Alice returns parities (each parity counted once in `bits_leaked`) |
| n | B→A | `RECONCILE_DONE` | `corrections`, `bits_leaked`, `out_len`, `pa_seed`, `verify_seed`, `verify_tag`, `eps_cor` |
| n+1 | A→B | `RECONCILE_VERIFY` | `ok` — Alice's tag over her key equals Bob's. On `false` both sides raise `KeyVerificationError` and output **no key** |

Key material = matched positions minus everything disclosed in step 3. After `RECONCILE_VERIFY ok`, both sides apply the same Toeplitz hash (`qne/privacy.py`, seed `pa_seed`, length `out_len`) and hold the identical secret. Any unexpected `type` raises `ProtocolError`.

### 5.2 `qne-sequence` (`WireCodec` JSON envelopes over `Link`/`ReliableLink`)

BB84: `QUBITS` (descriptor batches, `bulk` or `per_event`), `QUBITS_DONE`, `BASES`, `SIFTED` (matching indices, sample indices + Bob's sample bits, decoy indices/bits when `--decoy`), `QBER`/`SUMMARY`; then the same `PARITY_REQ`/`PARITY_RESP`/`RECONCILE_DONE`/`RECONCILE_VERIFY` exchange over `RpcChannel`. Every frame carries `t_send` so the receiver can fire it at `t_send + delay` (lookahead) and account late arrivals. E91/repeater add `PLAN`, `MEASURE_REQ/RESP` (the state-service RPC, *not* part of the public classical transcript), heralds (`X^m2 Z^m1` corrections from each station) and the CHSH quartet.

## 5.3 Time authority (control plane, newline-delimited JSON over TCP)

| Dir | Message | Meaning |
|---|---|---|
| node→authority | `{"kind":"hello","node":N,"lookahead_ps":L}` | register; `L` = modeled one-way delay of the node's links |
| node→authority | `{"kind":"report","node":N,"round":r,"next_ps":T\|null,"sent":S,"recv":R}` | earliest unexecuted event (null = idle) + cumulative classical messages sent/received |
| authority→node | `{"kind":"grant","round":r+1,"lbts":X\|null}` | events with time ≤ X may run; `null` = every node idle and nothing in flight → run over |
| authority→node | `{"kind":"rereport"}` | Σsent ≠ Σrecv: transients in flight, report again |
| node→authority | `{"kind":"bye","node":N}` | node finished; its last counts stay in the totals |

`LBTS = min_i(next_ps_i + lookahead_i)` over live nodes. Nodes snapshot counters *before* their queue, and links count a message only after queuing it, so "idle with balanced counts" cannot hide a pending message.

## 6. Key-rate accounting (`qne/bb84.py`, `qne/finite_key.py`, `qne/decoy.py`)

| Mode | Formula | Where |
|---|---|---|
| Asymptotic (default) | `r = 1 − 2h(Q)` for `Q < 0.11`, else 0; PA length `n·(1−h(Q)) − leak_EC − t` | `secure_key_fraction`, `reconcile.secure_key_bits` |
| Efficient BB84 (`basis_bias ≠ 0.5`) | `1 − h(e_z) − h(e_x)`; key from Z–Z only, all X–X disclosed | `efficient_secure_fraction` |
| Finite key (`--finite-key`) | `ℓ = n(1 − h(Q + μ)) − leak_EC − log2(2/(ε_sec²·ε_cor))`, `μ = sqrt((n+k)/(nk)·(k+1)/k·ln(4/ε_sec))` (TLGR Eq. 2) | `finite_key_length` |
| Decoy (GLLP) | `R = ½[ Q₁(1 − h(e₁)) − f_EC·Q_μ·h(E_μ) ]` with Ma–Qi–Zhao–Lo Eq. 34/37 bounds `Y₁ᴸ`, `e₁ᵁ`; near-vacuum Y₀ straddled conservatively | `decoy_state_key_rate` |
| Entanglement (Werner weight w) | `QBER = (1−w)/2`, `S = 2√2·w`, chain of L links: `wᴸ`; Bell fidelity `(3w+1)/4` | `qstate_core`, `repeater.py` |

`t = ceil(log2(2/ε_cor))` is the key-verification tag length (51 bits at `ε_cor = 1e-15`). `h` is binary entropy. `Q` is the sampled point estimate in asymptotic mode by definition; the finite-key mode is the defensible number for real block sizes.

## 7. Scenario configuration

`ScenarioConfig` (`qne/config.py`, nested YAML) — `channel{distance_km, attenuation_db_per_km, polarization_fidelity}`, `detector{efficiency, dark_count_rate (Hz), detection_window (s, default 1e-9), dead_time (ns), timing_jitter (ns)}`, `protocol{num_photons, send_rate_hz, sample_fraction, wavelength, basis_bias}`, `seed` (**`null` by default** — OS entropy; an integer makes the run reproducible and its key derivable). `ValidationScenario` (`validation/scenario.py`) is the flat, platform-neutral form accepted by every simulator adapter and supports `sweep:` files. Result schemas: `qne.metrics.ExperimentMetrics` (raw-socket path) and the `node_runner` JSON line (`qne-sequence/README.md`).
