# QFabric Concepts — the physics, and exactly where it lives in the code

This document walks through every quantum-networking concept QFabric implements —
from "what is a qubit" to entanglement swapping across real testbed nodes — and, for each one,
points at the **specific code** that realizes it, the **tests** that prove it, and the
**notebooks** that let you watch it run.

How to read it: Part I builds the quantum foundations (each section is
*concept → in the code → see it run*). Part II assembles them into the QKD pipeline.
Part III explains what is emulated vs real, and why the numbers can be trusted.
The appendix is a one-table concept → code index.

**The design bet, in one sentence:** the *quantum channel* (single photons, loss,
noise, entanglement) is emulated — in a P4 programmable switch and a numpy state
register — while the *classical channel* (every message Alice and Bob exchange) is
real TCP over real network links. QKD is a distributed protocol whose performance is
gated by its classical coordination; QFabric makes that coordination physically real
on the FABRIC testbed while keeping the physics honest and cross-validated.

---

## Part I — Quantum foundations

### 1 · Qubits, bases, and measurement

**Concept.** A qubit is |0⟩, |1⟩ or a superposition, and reading it means choosing a
*basis*: the wrong basis returns a coin flip and collapses the original away. QKD uses
the conjugate pair **Z** (rectilinear: |0⟩, |1⟩) and **X** (diagonal: |+⟩, |−⟩ =
(|0⟩ ± |1⟩)/√2). Worked through from zero in [`PRIMER.md`](PRIMER.md) §1.1–1.2.

**In the code — two levels of model, deliberately.**

*Descriptor level (BB84 path).* Prepare-and-measure BB84 never needs amplitudes: the
measurement statistics are fully reproduced by carrying a classical descriptor
`(basis, state)` per photon and applying the two rules above at the detector.

- `qne/photon.py` — a photon is `PhotonPacket(basis, state, sequence_num, …)`, with
  `Basis.Z = 0`, `Basis.X = 1`. It serializes to a 17-byte header inside a custom
  Ethernet frame (EtherType `0x7101`) so photons can traverse a real data plane.
- `qne/detector.py::Detector._detect` — Bob picks a measurement basis
  (`meas_basis = 0 if rng.random() < basis_bias else 1`); if it matches the photon's
  basis the bit is read deterministically (`bit_value = photon.state`), otherwise the
  outcome is random (`rng.integers(0, 2)`). That *is* conjugate-basis measurement,
  at the level of statistics.

*State-vector level (entanglement path).* Entanglement cannot be captured by
per-photon descriptors (§3), so `qne-sequence/qne_sequence/qstate_core.py` implements
real quantum states:

- `QStateRegister` holds joint states as complex amplitude vectors over groups of
  qubits (a 2-qubit group is a length-4 vector over |00⟩,|01⟩,|10⟩,|11⟩).
- `measure(qubit_id, angle, samp)` is a full projective measurement at any angle θ in
  the X–Z plane: it rotates the qubit by `Ry(−θ)` (`_rot_meas_unitary`) so the target
  basis becomes Z, computes P(outcome 0) as the summed |amplitude|² over the matching
  basis states (**the Born rule, literally**: `p0 = np.sum(np.abs(amp[mask0])**2)`),
  draws the outcome, **collapses** the state onto the outcome subspace, renormalizes,
  and traces the measured qubit out. θ=0 is Z, θ=π/2 is X, θ=π/4 and 3π/4 are the
  CHSH angles (§5).

**See it run:** `tests/test_detector.py` (matched basis deterministic, mismatched
random); `qne-sequence/tests/test_e91.py::test_matching_basis_perfectly_correlated_at_f1`.

---

### 2 · No-cloning and measurement disturbance — why QKD is possible at all

**Concept.** No-cloning means there is no passive wiretap for qubits: an eavesdropper
must measure, must guess a basis, and a wrong guess re-prepares the qubit wrongly — a
disturbance the honest parties see as elevated error rate. Security is physical, not
computational. Worked through in [`PRIMER.md`](PRIMER.md) §1.3.

**In the code.** `qne/eve.py::InterceptResendEve` implements the canonical
intercept-resend attack, and the *structure of the class enforces no-cloning*: Eve's
only handle on a photon is `intercept(basis, bit)`, which must commit to a random
measurement basis and forward what she measured — there is no "copy" operation to
call. The arithmetic of detectability falls straight out:

- Eve guesses Alice's basis correctly with probability ½ → she learns the bit and
  retransmits it faithfully: no error.
- Eve guesses wrong (½): she resends in the wrong basis, so on a *sifted* position
  (where Alice's and Bob's bases match) Bob's outcome is random: error with
  probability ½.

So tapping a fraction *f* of photons adds **QBER ≈ 0.25·f** to the sifted key
(`expected_sifted_qber`). Tap everything and QBER ≈ 25% — far beyond the 11%
security threshold (§12), so the run aborts and no key is issued. Eavesdropping
converts directly into a measurable, thresholdable statistic.

**See it run:** `tests/test_eve.py`, `qne-sequence/tests/test_two_node_eve.py`
(distributed, over real sockets), notebook `concepts/01_eavesdropper`.

---

### 3 · Entanglement and Bell states

**Concept.** Two qubits can share a *joint* state that does not factor into "A's state"
and "B's state". The four maximally entangled **Bell states**, which the code names
directly:

```
|Φ⁺⟩ = (|00⟩ + |11⟩)/√2      |Φ⁻⟩ = (|00⟩ − |11⟩)/√2
|Ψ⁺⟩ = (|01⟩ + |10⟩)/√2      |Ψ⁻⟩ = (|01⟩ − |10⟩)/√2
```

Both halves of |Φ⁺⟩ measured in the *same* basis always agree, in Z or in X — a
basis-independent correlation with no classical analogue, and the raw material for
entanglement-based QKD (§19) and repeaters (§6). Worked through in
[`PRIMER.md`](PRIMER.md) §3.1.

**In the code.** The four Bell vectors are literal constants in
`qstate_core.py::_BELL`. `create_bell_pair()` allocates two qubit ids sharing one
amplitude vector; when either party measures its half, `measure()` collapses the
*joint* state, so the partner's subsequent measurement is automatically consistent.

**Why a shared register (an architectural consequence of the physics).** BB84 could
serialize photons as descriptors because each is independent. A Bell pair's state is
*one* object spanning two nodes — it cannot be split into two messages. So one
process owns the `QStateRegister` (the "quantum state service",
`quantum_state_service.py`), and the remote party performs measurements via RPC
(`remote_qm.py::RemoteQuantumManager` sends `MEASURE_REQ` over the wire). One
authority owns every collapse; only *classical* data (qubit ids, angles, outcomes)
crosses the network — which is also physically honest: in a real network too, the
only thing that travels between entangled nodes is classical coordination.

**See it run:** `qne-sequence/tests/test_e91.py` — perfect correlation in both bases
at fidelity 1.0; `test_two_node_e91.py` for the two-process version.

---

### 4 · Mixed states, noise, and the Werner state

**Concept.** Real channels degrade entanglement into a mixture. QFabric models that
with the **Werner state**, the standard isotropic-noise form — and the platform's
single noise knob:

```
ρ = f·|Φ⁺⟩⟨Φ⁺| + (1 − f)·I/4
```

`f ∈ [0,1]` is the knob (`fidelity` in every API): with probability *f* a perfect
|Φ⁺⟩, otherwise the maximally mixed I/4 — a uniform mixture of all four Bell states.
Why one knob suffices: [`PRIMER.md`](PRIMER.md) §3.4.

**In the code.** `create_bell_pair(fidelity)` implements the mixture by *sampling*:
emit |Φ⁺⟩ with probability `f + (1−f)/4` and each other Bell state with probability
`(1−f)/4`. Averaged over many pairs this reproduces ρ exactly — a mixed state *is*
a classical distribution over pure states, so sampling the decomposition per shot is
not an approximation of the density matrix; it is the density matrix, realized.

**Why one knob is enough — the QBER law.** Group the Bell states by their
correlations:

- **Z basis:** Φ⁺, Φ⁻ give *equal* outcomes; Ψ⁺, Ψ⁻ give *opposite* ones.
  Error probability = P(Ψ⁺) + P(Ψ⁻) = **(1 − f)/2**.
- **X basis:** Φ⁺, Ψ⁺ give equal; Φ⁻, Ψ⁻ opposite. Error = P(Φ⁻) + P(Ψ⁻) =
  **(1 − f)/2** again.

So matching-basis **QBER = (1−f)/2** in every basis, and (§5) **CHSH S = 2√2·f**.
One parameter ties the key-error rate and the Bell-test violation together — exactly
the property that makes an entanglement-based security test meaningful, and it keeps
the `fidelity` knob directly comparable to the BB84 path's polarization fidelity.

**See it run:** `test_e91.py::test_qber_equals_one_minus_f_over_two` sweeps f and
checks the law to ±1.5%.

---

### 5 · Nonlocality — the CHSH Bell test

**Concept.** Bell's theorem rules out faking these correlations with prior classical
agreement, and gives a measurable criterion. In the **CHSH** form each party measures
at one of two angles, and the coincidence statistics build

```
S = E(a₀,b₀) − E(a₀,b₁) + E(a₁,b₀) + E(a₁,b₁)
```

where E is the correlation (+1 always equal, −1 always opposite). Local hidden-variable
models obey **S ≤ 2**; quantum mechanics reaches **2√2 ≈ 2.83** (Tsirelson) at the
optimal angles. So S > 2 certifies genuine entanglement, and because intercept-resend
destroys entanglement it doubles as an eavesdropping test that cannot be spoofed
classically. Worked through in [`PRIMER.md`](PRIMER.md) §3.2.

**In the code.** `qne-sequence/qne_sequence/e91.py`:

- The measurement angles are `_ANGLE = {0: 0, 1: π/4, 2: π/2, 3: 3π/4}`; in `e91`
  mode Alice draws from {0, π/4, π/2}, Bob from {π/4, π/2, 3π/4}.
- `chsh_value()` bins the *cross-basis* detected pairs into the four CHSH
  combinations `(a,b) ∈ {(0,1),(0,3),(2,1),(2,3)}`, computes each
  E = P(equal) − P(differ), and sums with signs `+,−,+,+`.
- With the Werner state the prediction is **S = 2√2·f** — the register reproduces it
  because the sampled Bell mixture and the projective measurement at arbitrary
  angles (§1) are both exact.

**Measured on real hardware:** the distributed E91 run on the FABRIC testbed
produced CHSH > 2 across a real WAN (re-record with notebook `concepts/03_repeater`;
run outputs are not tracked), and
the three-node repeater run reproduced a violation across a *swapped* chain (§6).

**See it run:** `test_e91.py` (S ≈ 2√2 at f=1; S = 2√2·f under noise), notebook
`sequence/03_entanglement_e91`, notebook `concepts/03_repeater` §3.

---

### 6 · Entanglement swapping — teleportation, heralds, and repeater chains

**Concept.** Long-distance entanglement is built from short links. A middle station
holding one half of each adjacent pair performs a **Bell-state measurement** on its two
qubits, projecting the two *outer* qubits — which never interacted — into a Bell state;
the BSM's 2-bit outcome is the **herald**, and the far end applies X<sup>m₂</sup>Z<sup>m₁</sup>
to standardize the result back to |Φ⁺⟩. Swapping is teleportation of half a pair. Worked
through in [`PRIMER.md`](PRIMER.md) §4.2–4.3.

Two consequences drive the networking, and both are asserted by tests here:

- **The herald channel is load-bearing.** Without the classical herald, B's qubit
  is an even mixture over the four Bell states — pure noise (QBER = ½). The
  entanglement literally does not exist for you until the classical bits arrive.
  Herald latency is therefore a first-class network cost.
- **Quality composes multiplicatively.** Swapping two Werner pairs of parameter f
  yields a Werner pair of parameter f². For an L-link chain:

  ```
  w_chain = f^L   →   QBER = (1 − f^L)/2,   Fidelity = (1 + 3·f^L)/4,   S = 2√2·f^L
  ```

  (Index Bell states by two bits (x,z): swapping XORs the indices, and each noisy link
  randomizes its own with bias f, so L links compose to f^L.)

**In the code.**

- `qstate_core.py::bell_measure(q1, q2)` implements the BSM as the standard analyzer
  circuit — CNOT(q1→q2), Hadamard(q1), then measure both in Z (`_CNOT`, `_HADAMARD`,
  `_apply_2q`). The heralds deterministically identify the Bell state:
  Φ⁺→(0,0), Φ⁻→(1,0), Ψ⁺→(0,1), Ψ⁻→(1,1). If the two qubits belonged to different
  entangled groups, `_merge` first joins them into one amplitude vector (tensor
  product) — that merge *is* the moment the two links become one chain.
- `apply_pauli(qubit, x, z)` is the heralded correction.
- `repeater.py::run_chain_session` runs an n-node chain in-process: create L Werner
  pairs, BSM at each intermediate node, XOR-accumulate the heralds, correct once at
  the end (valid because Paulis compose by XOR), then measure the end-to-end pair
  (BBM92 or CHSH mode). **The Werner-chain law is nowhere in the protocol code — it
  emerges from the circuit**, and the tests check it does.
- `distributed_repeater.py` distributes exactly this across **three OS processes and
  three TCP links**: alice hosts the register and creates both link pairs; the
  repeater station performs BSMs via RPC and sends the heralds to bob **over its own
  link** (the herald traffic is real network traffic); bob applies the corrections
  before measuring, then runs the standard QKD tail (§7). A `--no-correction` control
  run collapses QBER to ½ — demonstrating the herald channel's role empirically.
- On FABRIC, the station runs on the switch node between the endpoints
  (`deploy_fabric.setup_repeater_bridge` + `run_sequence_repeater`), validated live:
  identical keys extracted at both ends with heralds crossing a real 25 ms WAN link.

**Physics footnote (delayed choice).** In the distributed flow Alice measures her
end *before* the swaps happen. That's fine — this is "delayed-choice entanglement
swapping": the heralded correlations are independent of measurement ordering, and
the tests confirm the chain law holds under this ordering too.

**See it run:** `test_repeater.py` (herald mapping, chain law, no-correction
control), `test_three_node_repeater.py` (all of it across processes), notebooks
`12_repeater` and `12_repeater_fabric`.

#### 6.1 · From swapping to *computing* — teleportation and the non-local CNOT

**Concept.** The same machinery buys something other than a key. Two QPUs at
different sites share no qubit, so no gate between them is possible unless they spend
entanglement — either **teleporting** a state (BSM the data qubit against your half of
a pair, send 2 bits, the peer applies X<sup>m₂</sup>Z<sup>m₁</sup>; the data qubit is
consumed) or running a **telegate**, the non-local CNOT built from a cat-entangler and
cat-disentangler (1 bit each way, and the control never leaves its node — which is why
a distributed compiler emits telegates, not teleports). Teleporting is swapping with a
data qubit in place of a link half, which is why the validated repeater code already
contained the primitive. Worked through in [`PRIMER.md`](PRIMER.md) §4.4.

Two properties matter here, and both are asserted by tests:

- **One number prices a gate and a key.** A Werner-w pair is wrong with probability
  3(1−w)/4, and the three faulty Bell states land as Pauli errors — Z → phase error on
  the control, X → bit error on the target, XZ → both — so bit-error = phase-error =
  **(1−w)/2**, the same curve as the E91 QBER (§4).
- **Late is not lost.** Because the corrections are Paulis, a bit that misses its gate
  can be applied on arrival, carried as a tracked **Pauli frame** through later Clifford
  gates, or folded into an already-recorded outcome by XOR (`correct_outcome`) — the
  same move §6 makes when it XOR-composes L heralds and corrects once at the end. So
  herald latency is a memory-time cost, not an error source. An error appears only when
  a correction is permanently unavailable or arrives after the result was consumed, and
  *that* costs 50%, because each bit protects exactly one Pauli channel.

**In the code.**

- `qstate_qiskit.py::QiskitRegister` — a drop-in for `QStateRegister` (identical
  interface, so `RemoteQuantumManager` and everything above it are unchanged) backed
  by `qiskit.quantum_info.Statevector`, adding the one operation a QKD register has
  no reason to offer: `apply_circuit(ids, QuantumCircuit)`, i.e. "run this circuit on
  my local qubits". `qstate_core` gained `alloc` / `apply_gate` / `statevector` so the
  primitives run on both backends. Endianness — qfabric groups are MSB-first, Qiskit
  is little-endian — is pinned by a CNOT truth-table test on both.
- `dqc.py` — the primitives, each split into the steps a *single node* runs with the
  classical bits returned explicitly, so distributing them is only a matter of
  putting those bits on the link: `teleport_send`/`teleport_recv`,
  `telegate_cnot_send`/`_apply`/`_finish`.
- `distributed_dqc.py` (`node_runner --protocol dqc`) does exactly that across two
  processes: alice is the control node **and** the register authority, bob the target
  node whose half of each gate is an op over the wire (the same seam the repeater
  station uses for its BSMs). m1 travels A→B inside `PLAN`, m2 returns B→A inside
  `ACK`, and each side applies **the value that arrived** — so `--dqc-drop` really
  corrupts the computation instead of pretending to. `--dqc-late` withholds the bit
  for the gate and folds it in afterwards, which recovers the result exactly.

**See it run:** `test_two_node_dqc.py` — the whole thing across two processes over a
real link, including the global-timeline certificate at a 100 km modeled delay. And
`test_dqc.py` — exact transfer and the CNOT truth table at w=1;
telegate on |+⟩|0⟩ producing |Φ⁺⟩ correlated in Z *and* X (coherent, not a classical
copy of a measured bit); the (1−w)/2 law on both error channels; a
*permanently lost* correction bit costing 50% on the one channel it protects while a
*late* one is recovered exactly; and numpy vs Qiskit agreeing shot for shot.

---

## Part II — From quantum effects to a secret key

### 7 · The pipeline spine: transmit → sift → estimate → distill

Every protocol in the repo ends the same way. Learn this spine once:

1. **Transmit & measure** (quantum phase) — differs per protocol: BB84 photons
   (§8–9), Bell-pair halves (§19), a swapped chain (§6).
2. **Sift** (§10) — keep only the events where the bases matched.
3. **Estimate the error rate** (§11) — publicly sacrifice a random sample → QBER;
   abort above threshold (§12).
4. **Distill** — reconcile the remaining bits to identical strings (§13), then
   compress out the eavesdropper's information (§14). Both sides end with the same
   *secret* key, or provably nothing.

Steps 2–4 are pure classical protocol riding the real network — which is why their
cost (round trips, bytes, latency sensitivity) is measurable on FABRIC.

### 8 · BB84 — prepare and measure

**Concept (BB84, Bennett–Brassard 1984).** Alice encodes each bit in a random basis and
Bob measures in his own; afterwards Alice announces *bases only*, and the positions where
they matched carry the key. Because an eavesdropper cannot know the basis in flight (§2),
she leaves a QBER fingerprint. Step by step in [`PRIMER.md`](PRIMER.md) §2.2.

**In the code — the same protocol on two transports:**

- *Raw-socket path* (`qne/alice.py`, `qne/bob.py`): Alice's `_send_photons` draws
  `(basis, bit)` per photon and transmits real `0x7101` frames; Bob's
  `_receive_photons` parses frames and runs each through the `Detector`. Sifting,
  QBER, reconciliation then run over a TCP `ClassicalChannel` (`qne/channel.py`).
  This is the path that traverses the BMv2 P4 switch on FABRIC.
- *Distributed SeQUeNCe path* (`qne-sequence/qne_sequence/distributed_qkd.py`):
  real `sequence.qkd.BB84` protocol instances run in two separate processes, with
  every cross-node interaction converted to an explicit wire message
  (`BEGIN_PHOTON_PULSE → QUBITS → BASIS_LIST → SIFTED → QBER_RESULT`). A
  `GuardedRemoteStub` (§23) proves at runtime that no code cheats by reaching into
  the peer's memory.

Both paths call the *same* math in `qne/bb84.py::BB84Protocol` — one source of truth
for sift/QBER/key-rate, so the two transports are comparable by construction.

### 9 · The quantum channel: loss and imperfect detectors

**Fiber loss** follows the Beer–Lambert law: over L km at α dB/km,

```
P(survive) = 10^(−αL/10)        P(loss) = 1 − 10^(−αL/10)
```

Implemented identically in three places (deliberately — so the transports are
interchangeable):

- **In the P4 data plane** (`p4/bmv2/quantum_channel.p4`): the control plane
  precomputes `threshold = ⌊P(loss)·2³²⌋` per wavelength
  (`qne/config.py::loss_threshold_u32`) and installs it in the
  `quantum_channel_params` table. Per photon frame, the switch draws a random 32-bit
  number (`random(meta.random_value, …)`) and drops the frame if it is below the
  threshold — fiber attenuation as a match-action pipeline, with per-wavelength
  tx/drop counters. Classical traffic bypasses the loss table via `port_forwarding`.
- **In software channels** (`remote_channel.py::RemoteQuantumChannel`,
  `raw_photon.py::RawQuantumChannel`): the same per-photon Bernoulli drop, for
  switch-free runs.
- **As pair loss** for entanglement (`quantum_state_service.create_pairs`,
  `repeater.py` per-link survival): a lost photon means the pair was never heralded.

**Detector model** (`qne/detector.py`) — each effect is a real QKD impairment:

| Effect | Physics | Code |
|---|---|---|
| Efficiency η | photon arrives but detector doesn't click | `detected = rng.random() < efficiency` |
| Dark counts | thermal click with no photon → random bit (QBER ½ on those events) | `dark_count_prob = rate × window` |
| Polarization error p | optics misalignment depolarizes the qubit → matched-basis QBER = p/2; set from fidelity F as p = 1−F | depolarize branch in `_detect` |
| Dead time | detector blind for τ ns after each click | arrival-time gate vs `_blocked_until_ns` |
| Timing jitter σ | click lands outside the gate window w → lost; effective efficiency × erf(w/(2√2σ)) | gaussian draw vs w/2; `jitter_pass_probability()` |
| Multi-photon pulse (n photons) | P(click) = 1−(1−η)ⁿ — the decoy-state hook (§16) | `detect_pulse()` |

**See it run:** `tests/test_detector.py`, `tests/test_detector_realism.py` (the erf
law and dead-time slot arithmetic are asserted quantitatively), `p4/tests/`.

### 10 · Sifting

**Concept.** Alice announces her basis list — public and harmless, since the bits stay
secret — and both sides keep only the positions where bases matched *and* Bob detected
the photon. Unbiased random bases give a ~50% sift ratio ([`PRIMER.md`](PRIMER.md) §2.2).

**In the code.** `qne/bb84.py::BB84Protocol.sift` — an inner join of Alice's log and
Bob's detection log on sequence number, filtered on basis equality. The distributed
paths do the same join message-wise (Bob computes `matching` from `BASIS_LIST`).
Duplicate frames (switch flooding) are deduplicated by sequence number in
`qne/bob.py`.

**Efficient (biased-basis) BB84** (§ Lo–Chau–Ardehali): choose Z with probability
p > ½ on *both* sides and the sift ratio rises to **p² + (1−p)²** (0.82 at p = 0.9).
Security stays honest by splitting the roles of the bases: the key comes from Z–Z
matches; **all** X–X matches are disclosed to estimate the phase error, and the rate
becomes 1 − h(e_z) − h(e_x) (§12). Implemented via `basis_bias` in
`distributed_qkd.py` (Alice's draw + the disclose-all-X sampling policy),
`detector.py` (Bob's draw), and `bb84.py::efficient_secure_fraction`.

### 11 · Estimating the error rate — QBER and finite samples

**Concept.** Publicly comparing a random sample of the sifted bits estimates the
**QBER**, the one statistic carrying the security signal — channel noise and any
eavesdropping together ([`PRIMER.md`](PRIMER.md) §2.4). Two consequences: the compared
bits are now public and must be **discarded** from the key, and a finite sample carries
statistical error, so report a confidence interval and charge for the uncertainty when
claiming security (§17).

**In the code.** `BB84Protocol.qber_from_disclosed` counts disagreements and attaches
a **Wilson score interval** (better behaved than the normal approximation at QBER≈0,
`wilson_interval`). Sampling policy is shared across paths via `sample_size`. In the
distributed paths, Bob picks the sample positions and disclosure happens over the
wire; the key is then formed from the *unsampled* positions only (`key_only` /
`_bob_key_order`) — disclosed sample bits never leak into key material.

### 12 · How much of the key is secret? Entropy and the 11% threshold

**Concept.** QBER bounds how much an eavesdropper could know, and the **Shor–Preskill**
result turns that bound into the asymptotic secure fraction of the sifted key, in terms
of the binary entropy h(p) = −p·log₂p − (1−p)·log₂(1−p):

```
r = 1 − 2·h(Q)      (one h(Q) pays for error correction, one for privacy amplification)
```

r hits zero at **Q ≈ 11%**, the BB84 threshold: above it no secret key exists and the
only correct action is to abort. Derived in [`PRIMER.md`](PRIMER.md) §2.5.

**In the code.** `BB84Protocol.binary_entropy` and `secure_key_fraction` (returns 0
at Q ≥ 0.11) are the single source of truth used by every path — sim, raw-socket,
distributed, E91, repeater. The abort behavior is real: reconciliation is gated on
`secure_fraction > 0` everywhere (`node_runner.py`, `distributed_e91.py`,
`qne/bob.py`), so a run above threshold (e.g. under a full Eve) refuses to produce a
key. The biased-basis variant uses `efficient_secure_fraction(e_z, e_x) =
1 − h(e_z) − h(e_x)`, which reduces to Shor–Preskill when e_z = e_x.

### 13 · Reconciliation — Cascade

**Concept.** Sifted strings still differ on ~QBER of positions. **Cascade** repairs
Bob's using only public *parity* exchanges — blocks of size k₁ ≈ 0.73/QBER, binary
search into any block whose parity disagrees, and later passes over fresh permutations
that cascade backwards into blocks which were hiding error *pairs*
([`PRIMER.md`](PRIMER.md) §2.6). Every parity revealed is leakage: it must be counted
and paid back in §14.

**In the code.** `qne/cascade.py::reconcile`:

- Blocks, per-pass permutations, `_binary_correct` (the log-k bisection), and a
  work-queue that re-checks earlier passes after every flip — the cascade.
- **A deliberate departure from the textbook:** pass sizes *alternate* k₁, 2k₁,
  k₁, 2k₁… instead of doubling unboundedly. Doubling grows blocks toward the whole
  key, so a residual error *pair* stays co-located and is never separated; the
  alternating schedule keeps re-introducing small blocks. Measured effect: 200/200
  perfect reconciliations vs ~184/200 for doubling (see module docstring).
- `bits_leaked` counts every disclosed parity.
- The algorithm runs against a **parity oracle** — a callable returning Alice's
  parities for index sets. That one abstraction lets the identical code run
  in-process (tests) and across the network: `qne/reconcile.py::drive_cascade` (Bob)
  issues `PARITY_REQ` messages and `serve_parities` (Alice) answers them, over
  whichever transport — the qne-sequence `RpcChannel` or the raw path's
  `ChannelRpc`. On FABRIC, these parities are real round trips on the WAN — the
  measurable classical cost of error correction.

**See it run:** `tests/test_cascade.py`, `test_two_node_bb84_cascade.py`, notebook
`11_reconciliation`.

### 14 · Privacy amplification — the Toeplitz hash

**Concept.** After Cascade the keys are identical but *partially known* — channel
leakage plus every disclosed parity. **Privacy amplification** compresses n bits to m
with a randomly chosen **2-universal hash**, and the leftover-hash lemma makes the
output near-uniform given everything the eavesdropper knows, provided
([`PRIMER.md`](PRIMER.md) §2.7)

```
m ≈ n·(1 − h(Q)) − bits_leaked
```

(the `secure_key_bits` accounting — note it charges the *measured* Cascade leak, not
the asymptotic h(Q) twice, which would double-count error correction).

**In the code.** `qne/privacy.py::toeplitz_amplify` — the hash is a random m×n
**Toeplitz matrix** over GF(2), defined by only m+n−1 random bits (constant along
diagonals: `T[i,j] = diag[i−j+n−1]`), applied as `T·key mod 2`. The matrix seed is
*public*: both sides apply the same matrix to their identical reconciled keys and
extract the identical secret (its security comes from the hash family's randomness,
not secrecy). `drive_cascade` announces `(out_len, pa_seed)` in `RECONCILE_DONE`;
both ends produce the final key, verified bit-for-bit in every two-/three-process
test.

**See it run:** `tests/test_privacy.py`, and every `*_reconcile`/`auth_finite` test
asserts `alice_key == bob_key` on the amplified output.

### 15 · The adversary in the loop

§2 covered the physics; operationally, `--eve-fraction f` on the distributed BB84
path (`node_runner.py`) inserts `InterceptResendEve` into the photon stream
(`distributed_qkd.receive_qubits`), and the measured QBER then reflects channel
noise *plus* 0.25·f. The full-security demo: sweep f and watch `secure_fraction`
collapse to 0 past threshold — notebook `concepts/01_eavesdropper`. Open items: a
beam-splitting/PNS Eve (motivates §16) and an Eve on the E91 path (would be caught
by CHSH).

### 16 · Realistic sources — the PNS attack and decoy states

**Concept.** A real attenuated laser emits **Poisson(μ)** photons, so ~μ²/2 of pulses
carry two or more identical ones — and the **photon-number-splitting attack** lets Eve
siphon one, store it, and measure after the basis announcement: perfect information,
zero disturbance ([`PRIMER.md`](PRIMER.md) §2.8c).

The **decoy-state method** (Lo–Ma–Chen 2005) defeats PNS statistically: transmit at
several intensities (signal μₛ, weak decoy μ_d, near-vacuum μᵥ) chosen randomly and
announced only afterwards. Eve cannot tell them apart in flight, so she must attack all
intensities alike — while the honest parties compare per-intensity **gains** Q_μ and
error rates E_μ, which over-constrain any PNS strategy. The Ma–Qi–Zhao–Lo bounds then
lower-bound the single-photon yield Y₁ and upper-bound its error e₁, and only the
provably-single-photon fraction is kept (GLLP rate):

```
R ≥ q·[ Q₁(1 − h(e₁)) − Q_μ·f_EC·h(E_μ) ]     with  Q₁ = μ·e^(−μ)·Y₁
```

**In the code.**

- `qne/decoy.py::decoy_state_key_rate` — the Ma–Qi–Zhao–Lo bounds (PRA 72, 012326, Eqs. 34, 37), kept in their full
  form (the common truncation that drops the Q_s and Y₀ background terms
  *overestimates* Y₁; this implementation retains them — see docstring).
- **On the live transport** (`distributed_qkd.py`): Alice's `make_pulses` draws a
  per-pulse intensity class, samples a real photon number n ~ Poisson(μ), and thins
  it through fiber loss Binomial(n, 1−p_loss); the wire descriptor carries the
  *surviving* count. Bob's `detect_pulse` fires with 1−(1−η)ⁿ, and empty pulses can
  still dark-count (which is what makes the vacuum intensity measure the background
  Y₀). Intensity labels are announced only after detections are locked in; key
  material comes from signal pulses only; decoy-class matches are fully disclosed to
  measure E_decoy. The measured gains/QBERs — not analytic formulas — feed
  `decoy_state_key_rate`.

**See it run:** `tests/test_decoy.py` (bounds vs reference), `test_two_node_decoy.py`
(live run: measured gains match the weak-coherent model 1−e^(−ημ)), notebook
`13_qkd_security` §5, `scripts/decoy_sweep.py`.

### 17 · Finite-key security — honesty about sample sizes

**Concept.** Shor–Preskill is asymptotic — it treats the sampled QBER as the true one.
A real run of n key bits with k sampled needs two corrections ([`PRIMER.md`](PRIMER.md)
§2.8b): a **parameter-estimation penalty**, since the sample may have missed errors, via
the Serfling bound for sampling without replacement

```
μ = √( (n+k)/(nk) · (k+1)/k · ln(4/ε_sec) )        (TLGR Eq. 2)
```

so the true error rate ≤ Q_observed + μ except with probability ε_sec; and **failure
budgets** for error verification and privacy amplification, paid as explicit log-terms.
The extractable length (Tomamichel–Lim–Gisin–Renner form):

```
ℓ = n·(1 − h(Q + μ)) − leak_EC − log₂(2/ε_cor) − 2·log₂(1/(2·ε_PA))
```

with `leak_EC` the *measured* Cascade leakage. The striking practical lesson: short
noisy runs finite-key to **zero**. A ~6,000-bit block at the repeater chain's ~4.9%
QBER yields no secret at all — not a bug, the bound doing its job; noisy multi-hop
links need long blocks or better fidelity.

**In the code.** `qne/finite_key.py` (`serfling_mu`, `finite_key_length`); the
`--finite-key` flag makes `drive_cascade` size the Toeplitz output with the finite
bound instead of the asymptotic one, on BB84, E91, and the repeater chain alike.

**See it run:** `tests/test_finite_key.py` (monotonicity, convergence to the
asymptote, zero-at-small-n), `test_two_node_auth_finite.py`, notebook
`13_qkd_security` §2.

### 18 · Authentication — the assumption everyone forgets

**Concept.** BB84's proof *assumes* an **authenticated** classical channel — not
secret, authenticated. Without it Eve ignores the photons entirely and
man-in-the-middles the sifting conversation, running the protocol separately with each
party ([`PRIMER.md`](PRIMER.md) §2.8a). Production QKD authenticates with
information-theoretic Wegman–Carter MACs keyed from previously distilled secret;
QFabric models the same wire discipline with HMAC-SHA256 under a pre-shared key —
computationally rather than information-theoretically secure, but byte-for-byte the
same framing overhead and failure semantics, which is what a *network* emulator
needs to measure.

**In the code.** `qne/auth.py::FrameAuthenticator` — per-frame layout
`[8-byte seq][16-byte truncated HMAC tag][payload]`, with **strictly sequential**
sequence numbers: TCP already delivers in order, so any gap/repeat/reorder can only
mean tampering, and raises `AuthError`. Wired into both transports
(`qne/channel.py`, qne-sequence `Link`); a frame that fails verification tears the
connection down (like TLS) — the run then aborts rather than proceeding on
attacker-controlled sifting traffic. Enabled with `--auth-key` everywhere, including
all three repeater links.

**See it run:** `tests/test_auth.py` (tamper/replay/splice), `test_link_auth.py`,
notebook `concepts/04_qkd_security` §3.

### 19 · E91 / BBM92 — QKD from entanglement

**Concept (Ekert 1991 / BBM92).** A source distributes Bell pairs instead of Alice
preparing states; both parties measure in random bases, and matched bases give
correlated bits — BB84's sifted key by another route. E91 adds the CHSH combinations
(§5) and certifies *entanglement* as the security test; BBM92 is the Z/X-only,
key-efficient variant. Worked through in [`PRIMER.md`](PRIMER.md) §3.3.

**In the code.** `e91.py::run_session` is transport-independent protocol logic;
`distributed_e91.py` runs it across two processes (Alice hosts the state service,
Bob measures via RPC; basis announcement, QBER sample, CHSH-bit disclosure, then the
standard Cascade+PA tail all cross the real link). Sift/QBER/key math is *reused
from* `BB84Protocol`, so E91 and BB84 report directly comparable metrics — one of
the repo's cross-cutting design rules.

### 20 · The repeater as a network protocol

§6 covered the physics; the networking view is what makes it a FABRIC experiment.
The three-process chain (`distributed_repeater.py`) has three links with distinct
traffic classes — swap-plan + BSM RPCs (A↔R), **heralds** (R→B), and the QKD tail
(A↔B) — so each classical cost is separately measurable (e.g. netem delay on the
herald link isolates herald latency). On the slice, the station occupies the switch
node (`setup_repeater_bridge` swaps BMv2 for a Linux bridge carrying the station
IP), so all three links traverse real WAN segments. Validated live: identical
extracted keys with 2,000 swaps heralded over a 25 ms link.

---

## Part III — Why you can trust an emulator

### 21 · What is emulated, what is real, and what that means

In one line: the classical plane is real (every byte crosses real sockets, and on
FABRIC real links), the quantum plane is a statistical model (the `0x7101` frame carries
a cleartext `(basis, bit)` descriptor; the entangled register lives in one process's
memory). So results are statements about the **protocol under the modeled adversary**,
not about wire-level photon secrecy — the honest trade that lets the *networking* layer,
where the research questions live, be completely real.

**[`ASSUMPTIONS.md`](ASSUMPTIONS.md) is the authoritative ledger** — per-channel
assumptions, timing, detector and security accounting, distributed computing, and
cross-validation consistency. Read it before quoting any number; it is maintained, this
paragraph is only the summary.

### 22 · Cross-validation — not marking its own homework

The same scenario (distance, attenuation, fidelity, detector) runs on up to four
backends: QFabric measured (BMv2 + sockets), QFabric simulated
(`validation/run_qfabric.py`, same models minus the network), **SeQUeNCe**
(`run_sequence.py` — its own discrete-event QKD stack), and **NetSquid**
(`run_netsquid.py` — its own qubit formalism and `DepolarNoiseModel`). The adapters
drive each simulator's *native* machinery, so agreement is evidence about physics,
not code reuse. `validation/compare.py` applies a statistical agreement test with
combined variance and sample-size-aware tolerances, and unavailable backends are
reported **SKIPPED — never silently passed**.

### 23 · Runtime guarantees for the distributed claims

- `guarded_stub.py::GuardedRemoteStub` — SeQUeNCe's stock BB84 mutates its peer's
  memory directly (`self.another.…`), which would be *cheating* in a distributed
  emulator. The stub replaces `another` and raises on any access beyond the two
  legitimate addressing reads; a clean run with **zero** `RemoteAccessError`s is a
  runtime proof that every cross-node interaction really crossed the wire.
- `rt_timeline.py::RealTimeTimeline` — SeQUeNCe's timeline jumps virtual time;
  distributed processes must instead agree with the wall clock so simulated delays
  coexist with real socket latency. Events fire at `epoch + T·time_scale`, and
  listener threads inject inbound frames as events thread-safely.
- Determinism: every stochastic component takes a seed (register, channels,
  detectors, Eve, Cascade permutations), so runs are reproducible and the physics
  tests can assert tight tolerances.

---

### 23.5 · SeQUeNCe's QuantumManager, and how qfabric distributes around it

This section is worth its own space because it's the crux of the qfabric ×
SeQUeNCe relationship — where their design and ours meet.

**How SeQUeNCe stores quantum state.** In SeQUeNCe a qubit's state does *not*
live inside the photon object. It lives in one central store, the
**`QuantumManager`**, owned by the `Timeline` (`timeline.quantum_manager`) —
effectively a single in-process "quantum memory" for the whole simulation.

  * You allocate a qubit with `qm.new()`, which returns an integer **key**. A
    `Photon.quantum_state` is just that key — an index into the manager, not the
    amplitudes.
  * **Entanglement is a shared entry.** Two entangled qubits' keys point at the
    *same* multi-qubit state in the manager. A Bell pair is not "A's state + B's
    state"; it is one entry keyed by both. Measuring one key
    (`qm.run_circuit(circuit, keys)`) updates that shared entry, so the other
    key's later measurement is automatically consistent — the collapse is correct
    *because one authority holds the joint state*.
  * The same interface backs multiple formalisms (ket-vector, density-matrix,
    Fock), chosen per Timeline.

The design assumes **one process, one memory, pass-by-reference**. Efficient for a
simulator — and exactly what breaks across two processes: two managers, two
key-spaces, and a Bell pair's single shared entry cannot straddle them (DESIGN §2,
§6.2).

**qfabric's two answers.**

*BB84 (prepare-and-measure) — sidestep it.* BB84 qubits are independent, so
nothing needs a shared cross-process manager. `RemoteQuantumChannel` serializes
the classical **descriptor** `[seq, basis, bit]` per photon; Bob's `Detector`
measures it. Each process keeps its own local SeQUeNCe manager, but they never
have to share — the problem disappears. This is why BB84 was the first path
distributed.

*Entanglement (E91, repeaters) — rebuild "one authority" as a service.* We can't
share SeQUeNCe's manager across processes, so we built a minimal stand-in behind
an RPC (the same shape as `QuantumManager`, just the ops we need):

| SeQUeNCe (in-process) | qfabric (distributed) |
|---|---|
| `QuantumManager`, Timeline-owned | `QStateRegister` + `QuantumStateService`, owned by one process (Alice) |
| qubit key → shared state entry | qubit id → shared amplitude group (`qstate_core.py`) |
| entanglement = two keys, one entry | entanglement = two ids, one group |
| `run_circuit(gates, keys)` in memory | `measure(id,θ)` / `bell_measure(q1,q2)` / `apply_pauli` |
| pass-by-reference across the sim | `RemoteQuantumManager` → `MEASURE_REQ`/`RESP` on the socket (`remote_qm.py`) |

The remote party (Bob) holds a `RemoteQuantumManager`: his `measure_batch(...)`
touches no local state — it sends a request frame to Alice's service and blocks
for the reply. **One process owns every collapse; only classical RPC crosses the
wire** — which is also physically honest (in a real entanglement network, only
classical coordination travels between the nodes).

**Why we rolled our own instead of exposing SeQUeNCe's manager over RPC** — and
this is an open design question for the SeQUeNCe project, not a closed decision:
`QuantumManager` is tightly bound to the Timeline and its single-process
assumptions, and its general `run_circuit` interface is awkward to serialize.
`QStateRegister` is a *minimal, serialization-friendly* subset (measure at an
angle, Bell-measure, Pauli-correct). If SeQUeNCe ever exposes a distributable /
remote QuantumManager, `RemoteQuantumManager` is essentially a prototype of the
hook it would plug into.

### 24 · The test map

| Concept | Tests |
|---|---|
| Measurement rules, detector model | `tests/test_detector.py`, `tests/test_detector_realism.py` |
| Bell correlations, Werner QBER law, CHSH | `qne-sequence/tests/test_e91.py` |
| Swap heralds, chain law, no-herald control | `qne-sequence/tests/test_repeater.py` |
| Repeater across 3 processes | `qne-sequence/tests/test_three_node_repeater.py` |
| BB84 sift / QBER / rates | `tests/test_bb84.py` |
| Distributed BB84 (zero remote accesses, key match) | `qne-sequence/tests/test_two_node_bb84*.py` |
| Eve detectability | `tests/test_eve.py`, `…/test_two_node_eve.py` |
| Cascade | `tests/test_cascade.py`, `…/test_two_node_bb84_cascade.py` |
| Privacy amplification | `tests/test_privacy.py` (+ key-equality asserts everywhere) |
| Decoy bounds + live decoy | `tests/test_decoy.py`, `…/test_two_node_decoy.py` |
| Finite-key | `tests/test_finite_key.py`, `…/test_finite_key_link.py`, `…/test_two_node_auth_finite.py` |
| Authentication | `tests/test_auth.py`, `…/test_link_auth.py` |
| Biased-basis BB84 | `…/test_two_node_biased.py` |
| Raw-socket reconcile path | `tests/test_raw_path_reconcile.py` |
| Cross-validation harness | `tests/test_validation.py` |
| Teleport / non-local CNOT, both register backends | `qne-sequence/tests/test_dqc.py` |
| Distributed gate over a link, drop vs late controls | `qne-sequence/tests/test_two_node_dqc.py` |
| Corrections must come from the wire, not local state | `qne-sequence/tests/test_dqc_wire_integrity.py` |
| Global timeline: LBTS grants, no late frames | `qne-sequence/tests/test_time_authority.py`, `…/test_lookahead.py` |

---

## Appendix — concept → code index

| Concept | Where it lives |
|---|---|
| Qubit as (basis, state) descriptor | `qne/photon.py` |
| Projective measurement, Born rule, collapse | `qne-sequence/qne_sequence/qstate_core.py::measure` |
| Conjugate-basis rule (wrong basis = random) | `qne/detector.py::_detect` |
| No-cloning → intercept-resend Eve, QBER 0.25·f | `qne/eve.py` |
| Bell states | `qstate_core.py::_BELL`, `create_bell_pair` |
| Werner noise, QBER = (1−f)/2 | `create_bell_pair(fidelity)` sampling |
| CHSH S = 2√2·f, angles | `qne-sequence/qne_sequence/e91.py::chsh_value`, `_ANGLE` |
| Bell-state measurement (swap) + heralds | `qstate_core.py::bell_measure` (CNOT+H+ZZ) |
| Heralded Pauli correction X^m2·Z^m1 | `qstate_core.py::apply_pauli`, used in `repeater.py`/`distributed_repeater.py` |
| Quantum teleportation (state transfer) | `dqc.py::teleport_send` / `teleport_recv` |
| Non-local CNOT (cat-entangler/disentangler) | `dqc.py::telegate_cnot_send` / `_apply` / `_finish` |
| Arbitrary local circuits on a node's qubits | `qstate_qiskit.py::QiskitRegister.apply_circuit` |
| Werner-chain law F=(1+3f^L)/4 | emerges; checked in `repeater.py` results vs `chain_*` helpers |
| Distributed repeater (3 processes, herald link) | `qne-sequence/qne_sequence/distributed_repeater.py` |
| Fiber loss 1−10^(−αL/10) in the data plane | `p4/bmv2/quantum_channel.p4` + `qne/config.py::loss_threshold_u32` |
| Detector efficiency/darks/dead-time/jitter | `qne/detector.py` |
| BB84 sift / QBER / Wilson CI | `qne/bb84.py::BB84Protocol` |
| Shor–Preskill 1−2h(Q), 11% abort | `bb84.py::secure_key_fraction` (+ gating in every runner) |
| Efficient BB84, 1−h(e_z)−h(e_x) | `bb84.py::efficient_secure_fraction`, `basis_bias` knobs |
| Cascade reconciliation | `qne/cascade.py`; network driver `qne/reconcile.py` |
| Toeplitz privacy amplification | `qne/privacy.py::toeplitz_amplify` |
| Secret-length accounting | `qne/reconcile.py::secure_key_bits` |
| Decoy states (Ma–Qi–Zhao–Lo / GLLP) | `qne/decoy.py`; live source in `distributed_qkd.py::make_pulses`/`detect_pulse` |
| Finite-key (Serfling + TLGR) | `qne/finite_key.py` |
| Authenticated classical channel | `qne/auth.py` (+ `qne/channel.py`, `qne-sequence` `Link`) |
| E91/BBM92 protocol | `e91.py`, `distributed_e91.py` |
| Shared quantum-state authority + RPC | `quantum_state_service.py`, `remote_qm.py` |
| Distributed-runtime honesty gate | `guarded_stub.py`, `rt_timeline.py` |
| Cross-validation | `validation/` (`run_qfabric`, `run_sequence`, `run_netsquid`, `compare`) |
| FABRIC deployment | `scripts/deploy_fabric.py` |

*Companion reading:* `README.md` (workflow + notebook tracks), `qne-sequence/DESIGN.md`
(distributed-runtime architecture), `ROADMAP.md` (status), the `concepts/` notebooks (live demos
of Parts I–II).
