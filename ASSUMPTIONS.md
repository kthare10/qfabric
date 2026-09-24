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

## Timing (revised 2026-09-21 — central time authority; lookahead delivery kept as the wall-clock mode)

**The global timeline is now a real option, not a plan**, and it covers every protocol:
BB84, E91/BBM92 and the entanglement-swapping chain. `node_runner --time-authority`
(or `time_authority=True` on the `deploy_fabric.run_sequence_*` helpers) puts a run on
it. Simulation time is **decoupled from the wall clock**, and the contract is the same
everywhere: *every classical message is delivered at exactly `t_send + delay` in
simulation time, so nothing can be late whatever the wire does.*

Two mechanisms enforce that contract, and which one a protocol needs depends on a
single question — **can a node execute work without waiting for a message?**

| | needs | why |
|---|---|---|
| BB84 protocol phase | **LBTS grants** from the central authority (`time_authority.py`) | Alice free-runs on a local event queue (photon emission, detector events), so something must bound how far she may advance |
| BB84 post-processing, E91/BBM92, repeater chain | **shared logical clock only** (`LogicalClock` in `remote_qm.py`) — no coordinator process | every action waits on a receive, so the message order *is* the schedule; the receiver's clock jumps to the arrival time |

A node owns **one** clock, shared by all its links: a repeater station serving two links,
or Alice serving K+1, is a single sequential entity and cannot be at two simulation times
at once. Serving several inbound links therefore advances one monotonic clock — the node
is modelled as a sequential server with zero service time, and its fixed serve order
keeps the run deterministic regardless of which request physically arrived first.

The grant mechanism, in one paragraph: Each node keeps its local event queue but may only
execute events with timestamps up to the last *grant*. The authority computes the grant
as the lower bound on any timestamp that can still be produced,
`LBTS = min_i (next_event_i + lookahead_i)`, with `lookahead_i` = the modeled one-way
channel delay of node *i*'s links (a node whose next event is at *T* cannot make anything
arrive before *T + delay*), and only issues it when Σsent = Σrecv over all emulated
classical messages (nothing in flight; links count a message as received only after it
is queued). Consequently **no node can process an event past the arrival time of a
message it has not yet received**: `late_events` is zero *by construction*, at any
wall-clock speed, on any network — the run is an exact distributed execution of the
sequential simulator's schedule. `tests/test_time_authority.py` shows BB84, E91 and the
3-process repeater chain all certified at a modeled delay far below real message latency,
and the wall-clock timeline failing the identical runs with identical physics; the same
contrast holds on a live slice (`results/timeline_2026-09-21/`).
**Read the certificate with its coverage:** `on_time_events + late_events` is how many
frames were actually checked; on the slice run above that is 374 of 374, i.e. the whole
run. Two mechanisms enforce the same contract: the protocol timeline phase is paced by
LBTS grants, and the post-processing (Cascade + PA) phase — a strict request/response
ping-pong, where the message order *is* the schedule — is paced on a **logical clock**,
the receiver's clock jumping to `t_send + delay` on arrival. Local computation is
modelled as instantaneous in both, as the timeline already models its handlers, so a
Cascade of N round trips costs `N·2·delay` of simulation time, reported as
`postprocess_sim_ps`: reconciliation latency as a modeled quantity rather than a
property of the wire.
Cost: one authority round-trip per "how far may I advance" question (control plane,
TCP); wall-clock realism (delay pacing) is no longer implied — the emulation runs as
fast as the slowest node. The raw `0x7101` photon path is lossy and therefore not
message-counted; Bob's runner covers it with a wall-clock quiescence guard (no photon
frame for `--photon-drain-ms`) before executing events. Scope today: the BB84 path;
E91 and repeater runs still use the wall-clock timeline below.

### Wall-clock mode (the default; revised 2026-07-15 — lookahead delivery, no PTP)

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
  fidelity is verified per run, not assumed. The certificate is `lookahead.certified`:
  True only when a nonzero delay was modeled, at least one frame was checked against its
  deadline, and none missed. With `channel_delay = 0` nothing is checked and the field is
  `None` / `applicable: false` — a bare `late_events == 0` is *not* a pass. (The 2026-08-04
  BB84 L2 slice run was **not** certified: 22 of 82 frames were late.)
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

## Detector and security accounting (revised 2026-09-21)

- **Dark counts fire in every slot**, including slots whose photon was lost in the channel
  (`qne/bob.py` draws them for unseen sequence numbers; the NetSquid adapter and the
  distributed BB84 do the same). `dark_count_prob = dark_count_rate × detection_window`
  with `detection_window` a scenario knob (default 1 ns). At the default 10 Hz this is
  ~1e-8 per slot — effectively zero; raise the rate or the window to study the
  dark-count-dominated regime.
- **Randomness.** `ScenarioConfig.seed` defaults to `None`: bits, bases and QBER samples
  come from OS entropy. A seeded run is reproducible *and therefore not secret*; use seeds
  for cross-validation and tests only.
- **Key verification.** Every reconciled run ends with a `t = ceil(log2(2/ε_cor))`-bit
  2-universal (Toeplitz) tag of the corrected key; a mismatch aborts with no key on either
  side. In asymptotic mode the `t` public bits are subtracted from the PA output; in
  finite-key mode the `log2(2/ε_cor)` term already pays for them.
- **Finite-key constant.** `μ = sqrt((n+k)/(nk)·(k+1)/k·ln(4/ε_sec))`, TLGR Eq. (2)
  (arXiv:1103.4130). It is deliberately the published, conservative constant: a 6 k-pulse
  block yields zero finite-key bits, and that is the honest answer.
- **Efficient BB84.** With `basis_bias ≠ 0.5` the key is Z–Z matches only; every X–X match
  is disclosed for the phase-error estimate (both paths).

## Consistency across implementations (for cross-validation)

Cross-validation only means something if every backend runs under the **same physical
assumptions** — this was a specific point from the 2026-07-14 review. In particular:

- **Fiber-loss must be applied equivalently** across qfabric-sim, SeQUeNCe-native,
  NetSquid, the distributed path, and the P4 path. They use different mechanisms
  (software Bernoulli drop vs the P4 threshold vs each simulator's own loss model), so
  verify they produce matched detection/sift counts, not just matched QBER.
- **Detector assumptions (η, dark counts, detection window) must match** — today the SeQUeNCe
  adapter ignores `num_photons`/`sample_fraction` and uses a weak-coherent μ = 0.1 source,
  and `detection_window` is not a `ValidationScenario` field (open item). Otherwise the low-signal
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
