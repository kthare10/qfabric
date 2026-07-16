# QFabric — modeling assumptions (what's emulated, what's real, what's assumed)

This document states, explicitly, the physical assumptions behind QFabric's numbers —
so results are interpreted correctly and cross-validation is done under *matched*
conditions. It was sharpened by the SeQUeNCe-team feedback of 2026-07-14; where the
model changed as a result, that is called out.

## The two channels

QKD uses two channels, and QFabric treats them very differently:

| | Quantum channel | Classical channel |
|---|---|---|
| Carries | qubits (photons) | control messages (sifting, QBER, heralds, Cascade parities) |
| In QFabric | **emulated** — raw `0x7101` L2 through the P4/BMv2 switch | **emulated** — raw `0x7102` L2 through the same switch (TCP available for dev / control plane) |
| What's modeled | loss + noise (statistical), in the switch | propagation delay (timeline lookahead, or netem on switch egress); ~0 loss |

The design bet: both channels ride the same programmable switch — **the switch *is* the
fiber**, carrying the quantum wavelength (`0x7101`) and the classical wavelength (`0x7102`).
The quantum channel is a validated statistical loss+noise model; the classical channel is
engineered near-lossless with propagation delay only (see below). Running QKD as a *real
distributed system* over emulated-but-faithful channels is what lets QFabric execute a pure
simulator's exact event schedule on real hardware — and, as explicit stress runs, measure how
real network impairments (jitter, congestion) would affect QKD, the part SeQUeNCe/NetSquid
idealize. The control plane (shared quantum state, timeline RPC) stays TCP: it carries
emulator bookkeeping, not emulated physics, so dropping a message corrupts state rather than
modeling a channel.

## Quantum channel — assumptions

QFabric does **not** simulate electromagnetic fields, real photons, or true quantum
states on the wire. Two representations stand in:

- **Prepare-and-measure (BB84):** each "photon" is a `0x7101` Ethernet frame carrying an
  abstract `(basis, bit)` descriptor. There is no polarization field — "polarization" is
  only the interpretation of the two conjugate bases (Z = rectilinear, X = diagonal;
  see `qne/photon.py`).
- **Entanglement (E91/BBM92, repeaters):** a shared multi-qubit state register
  (`qstate_core.QStateRegister`, numpy) holds the joint state; measurement is projective
  at an X–Z-plane angle. Entanglement is emulated as a sampled **Werner state**, not a
  physical Bell pair.

Assumptions:

1. **Fiber loss = memoryless per-photon Beer–Lambert drop**, `P(loss) = 1 − 10^(−αL/10)`,
   independent per photon. No burst loss, no correlated fading. Applied in the P4 switch
   (data plane), in software channels, or as pair-loss for entanglement — the *same*
   probability, but the mechanism differs (see "Consistency" below).
2. **Noise = depolarizing.** Optics/channel imperfection is a single `polarization_fidelity`
   F giving intrinsic matching-basis QBER = (1−F)/2; for entanglement the Werner parameter
   f ties QBER = (1−f)/2 and CHSH S = 2√2·f together.
3. **Detector:** efficiency η, dark counts (each a 50/50 error), optional dead time and
   timing jitter. At high loss, dark counts dominate the (few) detections and drive QBER
   up — real detector physics, but it means low-signal regimes are noisy (see below).
4. **Single wavelength, single link per run** (the P4 loss table is wavelength-keyed, so
   WDM is a future extension, not a current claim).

## Classical channel — assumptions (revised 2026-07-14)

**Real QKD does not drop packets on the classical channel.** The classical channel is
engineered to be reliable — a separate fiber strand, or a different wavelength on the
same fiber, chosen so it does not interfere with the quantum channel. SeQUeNCe reflects
this: it assumes **no classical packet loss** and emulates only **propagation delay**.

Consequences for QFabric:

- The scientifically meaningful classical-channel effects to emulate are **latency,
  jitter, and congestion** — not loss. These change *time-to-key* and repeater
  *herald latency*, which is the real cost QKD pays over distance/at scale.
- QFabric *can* inject classical loss with `apply_classical_netem`, but treat that as a
  **fault/stress test**, not a realistic operating condition — do not present classical
  packet loss as a normal QKD condition.
- Quantum-channel loss (fiber attenuation) is the physical loss that matters, and it is
  modeled (above). Keep the two clearly separate: **loss belongs to the quantum channel;
  the classical channel gets delay.**
- **One distance, one channel model (2026-07-15):** the classical channel rides the same
  fiber route as the quantum channel, so its realistic delay is ~4.9 µs/km of the *same*
  L that drives quantum loss. `--channel-delay auto` derives it that way (the unified
  distance knob); the deploy helpers default to it. Correspondingly, slices default to
  **single-site** (`create_slice`): a photon cannot cross a WAN span, so distance is
  emulated, and the sub-ms site-local network *is* the near-zero classical channel real
  QKD assumes. Cross-site Bob is an explicit stress mode, not the baseline.
- **Both channels are raw L2 through the switch (2026-07-15).** The classical channel
  migrates off TCP onto a second EtherType (`0x7102`) carried through the same P4/BMv2
  switch as the `0x7101` photons — *the switch is the fiber, carrying both wavelengths.*
  The honest division of labor, since **BMv2 has no primitive to hold a packet** (no
  propagation-delay model in the data plane): the P4 pipeline owns classification,
  forwarding, per-wavelength loss (quantum only), and counters; **propagation delay is
  applied by netem on the switch's egress ports** — per link, per direction, both
  ethertypes — or, for fidelity runs, modeled in the timeline (lookahead delivery, above).
  Because a raw-L2 path can drop/reorder/duplicate under burst (socket-buffer overrun,
  BMv2 backpressure), a thin **reliable-datagram shim** (per-message seq + per-fragment
  ack + timeout-resend + dedup, plus fragmentation for basis lists that exceed the MTU)
  sits under the classical channel; the physics is engineered-lossless, so a lossless
  emulated path is *faithful*, and `qne/auth.py` seals the payload bytes unchanged on top.
  TCP remains available (`--classical-transport tcp`) for local/dev and is the transport of
  the control plane. `AF_PACKET` (raw L2) is Linux/slice-only.

## Timing (revised 2026-07-15 — lookahead delivery, no PTP)

- `RealTimeTimeline` maps simulation time to wall-clock via a **shared epoch + the OS
  clock** (`time_ns()`), so events fire at `epoch + T·time_scale` and channel delays line
  up with real socket latency.
- **The epoch is negotiated, not synchronized:** Bob (the serving side) is the time
  master; Alice adopts his epoch via a one-shot Cristian handshake (`timesync.py`,
  residual error ~RTT/2). **PTP was considered and rejected** — nothing in the protocol
  compares wall clocks across nodes, so full clock synchronization solves a problem the
  design does not have.
- **Toward a central timeline (interim, 2026-07-15):** rather than PTP, the design goal
  is one *authoritative* timeline the orchestrator/switch owns (the natural completion of
  the centralized `QuantumStateService` — centralize time the way state is already
  centralized). The interim step is in place: the shared epoch can be **seeded by the
  run-plan** (`--epoch-ns`) instead of picked by the master, so a whole multi-node run
  stamps against one orchestrator-owned origin (metric alignment). Default `0` keeps the
  master picking it locally. Hosting the full timeline centrally (remote nodes as thin I/O
  adapters) is deferred — it is only feasible once nodes are co-located (single-site
  slice, above), since otherwise every event would pay a cross-site RTT to the timeline;
  high-rate photon traffic stays in the P4 data plane and never touches it regardless.
- **Lookahead delivery** (`listener.Listener`): with `channel_delay > 0`, a frame is
  delivered at exactly `t_send + delay` in shared sim time — the event time a pure
  simulator would use — rather than at real-arrival + delay. The modeled channel delay
  acts as the *lookahead* of a conservative distributed discrete-event simulation: as
  long as real wire latency stays below `delay × time_scale`, the emulation executes the
  simulator's exact event schedule. Frames that miss their deadline fire immediately and
  are **counted** (`lookahead.late_events` / `max_lateness_ps` in every run's results) —
  fidelity is verified per run, not assumed. `late_events == 0` is the certificate that
  the run was, event-for-event, a distributed execution of the simulation.
- **Causality floor:** the modeled delay must exceed the stack's real per-frame latency
  (Python decode + thread wakeup ≈ tens of µs on loopback, plus the wire across hosts).
  Below that — e.g. a 2 km fiber's 9.8 µs — deadlines are honestly reported as late;
  emulate short distances with `time_scale > 1` (slow motion) if a clean certificate is
  needed at small L.
- Consequence: for *fidelity* runs, model the classical propagation delay in the
  timeline (`--channel-delay ≈ 4.9e6 ps per km`) rather than with netem — the timeline
  delivers at exact sim times, while netem adds real kernel-scheduled delay with jitter.
  netem remains the tool for *stress* runs (jitter, congestion, adversarial conditions).
- P4 and Python RNGs are independent: reproducibility holds *within* a backend, not
  bit-for-bit across the P4 and Python paths. Bit-exact emulation==simulation claims
  therefore require software loss (`--loss model`, seeded); the P4 path is validated
  statistically.

## Consistency across implementations (for cross-validation)

Cross-validation only means something if every backend runs under the **same physical
assumptions** — this was a specific point from the 2026-07-14 review. In particular:

- **Fiber-loss must be applied equivalently** across qfabric-sim, SeQUeNCe-native,
  NetSquid, the distributed path, and the P4 path. They use different mechanisms
  (software Bernoulli drop vs the P4 threshold vs each simulator's own loss model), so
  verify they produce matched detection/sift counts, not just matched QBER.
- **Detector assumptions (η, dark counts) must match** — otherwise the low-signal
  (high-loss / long-distance) QBER diverges, which is exactly what was observed (the P4
  path running hot at high loss). When comparing, either match dark-count rates or
  compare only in regimes where dark counts are negligible.
- QBER agreement should be judged with sample-size-aware tolerances (the agreement test
  in `validation/compare.py` already does this) — small live/P4 samples have wide
  intervals and should not be over-read.

## One-line summary

Emulated quantum channel (loss + Werner/depolarizing noise, no real photons) + real
classical channel (latency/jitter/congestion, **not** loss) + honest cross-validation
under matched assumptions. See `CONCEPTS.md` for the physics→code mapping and
`ROADMAP.md` for status.
