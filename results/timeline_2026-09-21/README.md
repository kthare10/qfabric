# Global timeline on the slice — 2026-09-21

Same slice (single-site TACC, `qfabric-bb84-2`), same scenario (`fabric_1km`: 1 km,
0.2 dB/km, F=0.98, η=0.8), same transports (quantum `0x7101` raw through BMv2,
classical `0x7102` raw L2), 20 000 pulses, modeled one-way delay 4.9 µs (= 1 km).
The only difference is which timeline drives the run.

| run | side | frames checked | late | max lateness | certified | QBER | secure key bits |
|---|---|---|---|---|---|---|---|
| authority | alice | 374 | 0 | 0 | **true** | 0.92 % | 4757 |
| authority | bob | 376 | 0 | 0 | **true** | 0.92 % | 4757 |
| wall-clock | alice | 406 | 406 | 4.45 ms | false | 0.99 % | 4643 |
| wall-clock | bob | 408 | 1 | 1.70 ms | false | 0.99 % | 4643 |

**E91 / BBM92** (entanglement-based QKD over the same slice, classical channel raw L2,
20 000 pairs, `--channel-delay auto`). Note the delay itself is new: `run_sequence_e91`
never passed one before 2026-09-21, so earlier E91 slice runs had delay 0 and a
certificate that passed vacuously.

| run | side | frames checked | late | max lateness | certified | QBER | CHSH S |
|---|---|---|---|---|---|---|---|
| logical | alice | 220 | 0 | 0 | **true** | 1.205 % | 2.742 |
| logical | bob | 221 | 0 | 0 | **true** | 1.205 % | 2.742 |
| wall-clock | alice | 220 | 220 | 74.08 ms | false | 1.205 % | 2.742 |
| wall-clock | bob | 221 | 2 | 68.36 ms | false | 1.205 % | 2.742 |

Identical physics (same seeds): same QBER, same CHSH violation, same 2581 secure key
bits, keys identical bit-for-bit. Only the schedule differs. E91 uses no coordinator
process — it never free-runs, so the shared per-node logical clock is the whole
mechanism (`sim_elapsed_ps` 2.156 ms for the session).

"Frames checked" is `on_time_events + late_events` — how much of the run the
certificate actually covers. Both modes now check the whole run: the protocol
timeline phase *and* the Cascade/privacy-amplification phase. Under the authority
nothing can be late; under the wall clock the 4.9 µs deadline is unmeetable for
Alice, who is emitting 20 000 photon frames while the deadlines run.

Both runs are physically sound (QBER at the (1−F)/2 ≈ 1 % floor, sift ≈ 7.6 k of
20 k, measured loss within 2 % of the model, keys identical bit-for-bit after
Cascade + PA). What differs is whether the run was an *exact* distributed
execution of the simulator's event schedule. Under the authority it is.

**Simulated post-processing cost.** The authority run reports
`postprocess_sim_ps = 3.646 ms`: 372 Cascade round trips × 2 × 4.9 µs, identical
on both sides. That is the reconciliation latency in *modeled* terms — deterministic,
a function of distance and block size rather than of the wire. The wall-clock mode
reports 0 (it spends real time instead).

Artifacts: `authority_{alice,bob}.json`, `wallclock_{alice,bob}.json`.
Reproduce: `notebooks/sequence/07_sequence_emulator.ipynb` with `TIMELINE = 'authority'`
or `'wall-clock'`.

**Repeater chain** (3 processes: Alice hosts the register, one station on the switch node
performs the entanglement swaps and heralds Bob, 4000 attempts, `--channel-delay auto`).
The station serves **two** links off one node clock.

| run | node | frames checked | late | certified | sim elapsed |
|---|---|---|---|---|---|
| logical | alice | 90 | 0 | **true** | 0.887 ms |
| logical | bob | 91 | 0 | **true** | 0.892 ms |
| logical | station1 | 2 | 0 | **true** | 0.015 ms |
| wall-clock | alice | 90 | 90 | false | 0.000 ms |
| wall-clock | bob | 91 | 19 | false | 0.000 ms |
| wall-clock | station1 | 2 | 2 | false | 0.000 ms |

Identical physics across both runs (deterministic seeds): 3653 swaps delivered, QBER 8.59 %,
CHSH S = 2.616 — and identical to the 2026-07-13 artifact, so the chain reproduces exactly.
(That QBER sits above the 4.88 % chain law on a 163-bit sample; it is the known thin-sample
outlier, unchanged by the timeline.) No key is extracted in either run: the 51-bit
key-verification tag now exceeds what a key this short can pay, which is the honest answer
for a 4000-attempt chain.

Note both modes check the same frames; only the verdict differs. The station is checked on
just 2 frames because it only exchanges a swap plan and its BSM results.
