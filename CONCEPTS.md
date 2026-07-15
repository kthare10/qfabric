# QFabric Concepts — the physics, and exactly where it lives in the code

This document walks through every quantum-networking concept QFabric implements —
from "what is a qubit" to entanglement swapping over a real WAN — and, for each one,
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

**Concept.** A classical bit is 0 or 1. A qubit is a quantum state that can be |0⟩,
|1⟩, or a superposition α|0⟩ + β|1⟩. Two facts drive everything in this repo:

- **Measurement is basis-relative.** You must choose a *basis* (an orientation of
  your measuring device) to read a qubit. QKD uses two *conjugate* bases: **Z**
  (rectilinear: |0⟩, |1⟩) and **X** (diagonal: |+⟩ = (|0⟩+|1⟩)/√2, |−⟩ = (|0⟩−|1⟩)/√2).
- **Wrong basis = coin flip.** A qubit prepared in a Z state and measured in X gives
  a uniformly random answer (the Born rule: outcome probability = |amplitude|²), and
  the qubit *collapses* to what was measured — the original state is gone.

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

**Concept.** The **no-cloning theorem**: an unknown quantum state cannot be copied.
There is no passive wiretap for qubits. An eavesdropper who wants the information
*must measure*, must guess a basis to do so, and a wrong guess both gives her a random
answer and **re-prepares the qubit in the wrong basis** — a disturbance the honest
parties can detect as an elevated error rate. Security is not computational; it's
physical.

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
(distributed, over real sockets), notebook `10_eavesdropper`.

---

### 3 · Entanglement and Bell states

**Concept.** Two qubits can be in a *joint* state that cannot be factored into
"qubit A's state" and "qubit B's state." The four maximally entangled **Bell states**:

```
|Φ⁺⟩ = (|00⟩ + |11⟩)/√2      |Φ⁻⟩ = (|00⟩ − |11⟩)/√2
|Ψ⁺⟩ = (|01⟩ + |10⟩)/√2      |Ψ⁻⟩ = (|01⟩ − |10⟩)/√2
```

Measure both halves of |Φ⁺⟩ in Z: outcomes are random individually but always
**equal**. Measure both in X: still always equal. That basis-independent correlation
has no classical analogue and is the raw material for entanglement-based QKD (§19)
and repeaters (§6).

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

**Concept.** Real channels degrade entanglement. A noisy Bell pair is described by a
**density matrix** — a probabilistic mixture of pure states. QFabric uses the
**Werner state**, the standard isotropic-noise model:

```
ρ = f·|Φ⁺⟩⟨Φ⁺| + (1 − f)·I/4
```

with `f ∈ [0,1]` the noise knob (`fidelity` in every API): probability *f* the pair
is a perfect |Φ⁺⟩, probability 1−f it is complete garbage (I/4 = the maximally mixed
state = a uniform mixture of all four Bell states).

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

**Concept.** Could the Bell correlations be faked by classical shared randomness
("the two photons secretly agreed on their answers in advance")? Bell's theorem says
no — and gives a measurable criterion. In the **CHSH** form: each party measures at
one of two angles; from the coincidence statistics build

```
S = E(a₀,b₀) − E(a₀,b₁) + E(a₁,b₀) + E(a₁,b₁)
```

where E is the correlation (+1 = always equal, −1 = always opposite). Any local
hidden-variable model obeys **S ≤ 2**. Quantum mechanics reaches **2√2 ≈ 2.83**
(Tsirelson's bound) at the optimal angles. Measuring S > 2 *certifies* genuine
entanglement — and since an intercept-resend attacker breaks the entanglement,
S > 2 doubles as an eavesdropping test that cannot be spoofed classically.

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
produced CHSH > 2 across a real WAN (results in `results/fabric_e91_*.json`), and
the three-node repeater run reproduced a violation across a *swapped* chain (§6).

**See it run:** `test_e91.py` (S ≈ 2√2 at f=1; S = 2√2·f under noise), notebook
`09_entanglement_e91`, notebook `12_repeater` §3.

---

### 6 · Entanglement swapping — teleportation, heralds, and repeater chains

**Concept.** Entanglement dies exponentially with channel length, so long-distance
entanglement is built from short links. Say A–R share a Bell pair and R–B share
another. The middle station R performs a **Bell-state measurement (BSM)** — a joint
measurement asking "which Bell state are *my two* qubits in?" — on its two halves.
This projects the two *outer* qubits (at A and B, which never interacted) into a
Bell state. Which one is identified by the BSM's 2-bit outcome, the **herald**; R
announces it classically, and B applies a corresponding Pauli correction
(X<sup>m₂</sup>Z<sup>m₁</sup>) to standardize the pair back to |Φ⁺⟩. (This is the same
mechanics as quantum teleportation — swapping is teleporting one half of a pair.)

Two consequences matter for networking:

- **The herald channel is load-bearing.** Without the classical herald, B's qubit
  is an even mixture over the four Bell states — pure noise (QBER = ½). The
  entanglement literally does not exist for you until the classical bits arrive.
  Herald latency is therefore a first-class network cost.
- **Quality composes multiplicatively.** Swapping two Werner pairs of parameter f
  yields a Werner pair of parameter f². For an L-link chain:

  ```
  w_chain = f^L   →   QBER = (1 − f^L)/2,   Fidelity = (1 + 3·f^L)/4,   S = 2√2·f^L
  ```

  (Why: index Bell states by two bits (x,z); swapping XORs the indices, and each
  noisy link randomizes its index with bias f, so L links compose to bias f^L.)

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

**Concept (BB84, Bennett–Brassard 1984).** Alice sends each bit encoded in a
randomly chosen basis (Z or X); Bob measures in his own random basis. After the
photons are gone, Alice announces *bases only* (never bits). Where bases matched,
Bob's bit equals Alice's; where they differed, his result was a coin flip and is
discarded. An eavesdropper cannot know the basis in flight (§2), so she leaves a
QBER fingerprint.

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

**Concept.** Alice announces her basis list (public, harmless — the bits stay
secret); both keep only positions where bases matched *and* Bob detected the photon.
With unbiased random bases the sift ratio is ~50%.

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

**Concept.** Alice and Bob publicly compare a random sample of their sifted bits.
The disagreement fraction estimates the **QBER** — the single statistic that carries
the security signal (channel noise + any eavesdropping). Two subtleties:

- The compared bits are now public → they must be **discarded** from the key.
- A finite sample has statistical error → report a confidence interval, and (§17)
  charge for the uncertainty when claiming security.

**In the code.** `BB84Protocol.qber_from_disclosed` counts disagreements and attaches
a **Wilson score interval** (better behaved than the normal approximation at QBER≈0,
`wilson_interval`). Sampling policy is shared across paths via `sample_size`. In the
distributed paths, Bob picks the sample positions and disclosure happens over the
wire; the key is then formed from the *unsampled* positions only (`key_only` /
`_bob_key_order`) — disclosed sample bits never leak into key material.

### 12 · How much of the key is secret? Entropy and the 11% threshold

**Concept.** The binary entropy function h(p) = −p·log₂p − (1−p)·log₂(1−p) measures
information in bits. From QBER Q, an eavesdropper's possible knowledge is bounded;
the **Shor–Preskill** result gives the asymptotic secure fraction of the sifted key:

```
r = 1 − 2·h(Q)      (one h(Q) pays for error correction, one for privacy amplification)
```

r hits zero at **Q ≈ 11%** — the famous BB84 threshold. Above it, no secret key is
possible and the only correct action is to abort.

**In the code.** `BB84Protocol.binary_entropy` and `secure_key_fraction` (returns 0
at Q ≥ 0.11) are the single source of truth used by every path — sim, raw-socket,
distributed, E91, repeater. The abort behavior is real: reconciliation is gated on
`secure_fraction > 0` everywhere (`node_runner.py`, `distributed_e91.py`,
`qne/bob.py`), so a run above threshold (e.g. under a full Eve) refuses to produce a
key. The biased-basis variant uses `efficient_secure_fraction(e_z, e_x) =
1 − h(e_z) − h(e_x)`, which reduces to Shor–Preskill when e_z = e_x.

### 13 · Reconciliation — Cascade

**Concept.** After sifting, Alice's and Bob's strings still differ on ~QBER of
positions. **Cascade** fixes Bob's string using only public *parity* exchanges:
split the key into blocks (size k₁ ≈ 0.73/QBER); for any block whose parity differs
from Alice's, an odd number of errors is inside — binary search (log₂k parities)
isolates and flips one. Later passes use fresh shared permutations; and the trick
that gives Cascade its name: correcting a bit flips the parity of every *earlier*
block containing it, so previously "even" blocks (hiding an error *pair*) become odd
and get fixed too. Every parity revealed is information leaked to an eavesdropper —
it must be counted and paid back in §14.

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

**Concept.** After Cascade the keys are identical but *partially known* to an
eavesdropper (channel leakage + all those parities). **Privacy amplification**
compresses the n-bit reconciled key to a shorter m-bit key with a randomly chosen
**2-universal hash**; the leftover-hash lemma guarantees the output is
near-uniform *given everything the eavesdropper knows*, provided

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
collapse to 0 past threshold — notebook `10_eavesdropper`. Open items: a
beam-splitting/PNS Eve (motivates §16) and an Eve on the E91 path (would be caught
by CHSH).

### 16 · Realistic sources — the PNS attack and decoy states

**Concept.** Ideal BB84 assumes exactly one photon per pulse. A real attenuated
laser emits **Poisson(μ)** photons; with probability ~μ²/2 a pulse carries ≥2
identical photons. The **photon-number-splitting (PNS) attack**: Eve siphons one
photon from every multi-photon pulse, stores it, and measures it *after* the basis
announcement — perfect information, zero disturbance. Plain BB84's proof collapses
for practical sources.

The **decoy-state method** (Lo–Ma–Chen 2005) defeats PNS with statistics: send
pulses at several intensities (signal μₛ, weak decoy μ_d, near-vacuum μᵥ) chosen
randomly and announced only afterward. Eve cannot tell them apart in flight, so she
attacks all intensities identically — but the honest parties can compare the
per-intensity **gains** Q_μ (detection rates) and error rates E_μ, which
over-constrain any PNS strategy. From them, *lower-bound* the single-photon yield Y₁
and *upper-bound* the single-photon error e₁, then keep only the provably-single-
photon fraction (GLLP rate):

```
R ≥ q·[ Q₁(1 − h(e₁)) − Q_μ·f_EC·h(E_μ) ]     with  Q₁ = μ·e^(−μ)·Y₁
```

**In the code.**

- `qne/decoy.py::decoy_state_key_rate` — the Lo–Ma–Chen bounds, kept in their full
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

**Concept.** The Shor–Preskill rate is asymptotic: it treats the sampled QBER as the
true error rate. For a run of n key bits with k sampled bits, two corrections are
mandatory:

1. **Parameter-estimation penalty.** The sample may have *missed* errors. The
   Serfling bound (sampling without replacement) gives a fluctuation term

   ```
   μ = √( (n+k)(k+1)/(2nk²) · ln(1/ε_PE) )
   ```

   such that the key's true error rate ≤ Q_observed + μ except with probability ε_PE.
2. **Failure budgets.** Error verification and privacy amplification each carry a
   small failure probability, paid as explicit log-terms.

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

**Concept.** BB84's security proof *assumes* the classical channel is
**authenticated** (not secret — authenticated). Otherwise Eve doesn't attack the
photons at all: she man-in-the-middles the sifting conversation and runs the whole
protocol with each party separately. Production QKD authenticates with
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
notebook `13_qkd_security` §3.

### 19 · E91 / BBM92 — QKD from entanglement

**Concept (Ekert 1991 / BBM92).** Instead of Alice preparing states, a source
distributes Bell pairs; both parties measure their halves in random bases. Matched
bases give correlated bits → key material, exactly like BB84 after sifting. E91's
addition: also measure the CHSH combinations (§5) and *certify entanglement* as the
security test. BBM92 is the Z/X-only, key-efficient variant.

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

Be precise about the claim. **Real:** every classical byte (sifting, parities,
heralds, RPCs) crosses real sockets and, on FABRIC, real WAN links; the P4 switch
really drops each photon frame with an independent hardware-pipeline RNG draw.
**Emulated:** the quantum states themselves. The `0x7101` photon frame carries the
`(basis, bit)` descriptor in cleartext, and the entangled register lives in one
process's memory.

The consequence: QFabric's security results are statements about the **protocol
under the modeled adversary** (`InterceptResendEve`, tampering on the classical
channel), not about wire-level secrecy of the emulated photons — a packet sniffer on
the quantum plane would read the descriptors, which no real photon permits. That is
the standard and honest trade of QKD *network* emulation: the physics layer is a
validated statistical model, so that the *networking* layer — where the research
questions live — can be completely real.

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
| Decoy states (Lo–Ma–Chen / GLLP) | `qne/decoy.py`; live source in `distributed_qkd.py::make_pulses`/`detect_pulse` |
| Finite-key (Serfling + TLGR) | `qne/finite_key.py` |
| Authenticated classical channel | `qne/auth.py` (+ `qne/channel.py`, `qne-sequence` `Link`) |
| E91/BBM92 protocol | `e91.py`, `distributed_e91.py` |
| Shared quantum-state authority + RPC | `quantum_state_service.py`, `remote_qm.py` |
| Distributed-runtime honesty gate | `guarded_stub.py`, `rt_timeline.py` |
| Cross-validation | `validation/` (`run_qfabric`, `run_sequence`, `run_netsquid`, `compare`) |
| FABRIC deployment | `scripts/deploy_fabric.py` |

*Companion reading:* `README.md` (workflow + notebook tracks), `qne-sequence/DESIGN.md`
(distributed-runtime architecture), `ROADMAP.md` (status), notebooks 09–13 (live demos
of Parts I–II).
