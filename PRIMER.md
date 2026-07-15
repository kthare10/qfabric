# Quantum Networking, From Zero
### A primer for people who know networks but not quantum

This document explains quantum networking from scratch. It assumes you are
comfortable with classical networking (packets, TCP, MITM attacks, checksums) and
basic probability — and **nothing else**. No physics background, no linear algebra.

It is deliberately slow and detailed. Every idea gets a "why", a worked example
with real numbers, and — where they exist — a warning about the standard
misconception. Read it in order; each part builds on the last.

- **Part 1** — the four quantum ground rules everything else follows from
- **Part 2** — BB84: turning those rules into an unbreakable shared secret
- **Part 3** — entanglement: the genuinely strange resource
- **Part 4** — repeaters: why distance is the enemy, and how to beat it
- **Part 5** — the big picture: what a quantum internet actually is

No code here. When you're ready to see how each concept is implemented in this
repo, `CONCEPTS.md` maps every section below to the source. Notebooks 09–13 let
you *watch* these concepts as live experiments.

---

# Part 1 — The quantum ground rules

Four facts about quantum physics power everything in this field. Each one will
feel strange. None of them requires math to use correctly.

## 1.1 · A qubit is not a bit

A classical bit is a 0 or a 1. You can read it whenever you want, copy it as many
times as you like, and reading it changes nothing. Every assumption you have as a
network engineer — store-and-forward, retransmission, packet capture, checksums —
quietly relies on those properties.

A **qubit** (quantum bit) is the state of a single quantum object — in networking
contexts, usually one **photon**, a single particle of light. A qubit can be in
state |0⟩, state |1⟩ (that notation just means "the quantum state labeled 0/1"), or
— and here is the first strange thing — a **superposition**: a state that is
genuinely *both at once*, in defined proportions.

A useful mental picture: a qubit is an **arrow pointing somewhere on a globe**.
North pole = |0⟩. South pole = |1⟩. But the arrow can point anywhere — the equator,
45° north, anywhere. The direction encodes the proportions: an arrow on the equator
is an equal blend of |0⟩ and |1⟩.

The proportions are called **amplitudes**, and the rule connecting them to reality
is the one piece of quantum math you need:

> **The Born rule:** when you measure, the probability of each outcome is the
> *square* of its amplitude.

A qubit that is "70% of the way toward |0⟩" in amplitude (amplitude 0.837 on |0⟩,
0.548 on |1⟩) gives outcome 0 with probability 0.837² = 70% and outcome 1 with
probability 30%. That's the whole rule.

**Misconception check:** superposition does *not* mean "it's secretly 0 or 1 and we
just don't know which." That would be an ordinary hidden value, like an unread
config flag. The Bell test in Part 3 *proves* — experimentally, not
philosophically — that no hidden pre-existing value can explain how qubits behave.
The qubit really is both, until measured.

## 1.2 · Measurement has a direction — and wrong directions give coin flips

Here is the fact that makes quantum cryptography work, so take it slowly.

To read a qubit you must **measure** it, and a measurement is always performed in a
chosen **basis** — think of it as the *orientation of your measuring apparatus*.
You don't ask a qubit "what are you?"; you ask "are you 0-or-1 **along this
direction**?" and it must answer with one of those two options.

The physical picture makes this concrete. Photons can carry information in their
**polarization** — the orientation in which the light wave oscillates. You have
handled polarization measurement hardware: polarized sunglasses. A polarizing
filter passes light oscillating one way and blocks the perpendicular way.

Quantum key distribution uses two measurement bases:

- The **Z basis** ("rectilinear"): is the photon polarized vertically (call it 0)
  or horizontally (call it 1)?
- The **X basis** ("diagonal"): is it polarized at +45° (call it 0) or −45°
  (call it 1)?

Now the crucial behavior, in three cases:

1. **Measure in the matching basis → deterministic.** A vertically polarized
   photon measured in the Z basis reads "0", every time. The preparation and the
   question are aligned; you get the encoded answer.
2. **Measure in the wrong basis → a fair coin flip.** A vertically polarized
   photon measured in the X basis (the ±45° question) has *no defined answer* to
   that question. Vertical is exactly halfway between +45° and −45°. The Born rule
   gives 50/50 — truly random, not "unknown."
3. **Measurement changes the qubit.** This is the part with no classical
   analogue. After that wrong-basis measurement returns (say) "+45°", the photon
   *is now* +45°-polarized. Its original vertical polarization is **gone** —
   destroyed, not hidden, unrecoverable by any future measurement.

You've seen case 2 and 3 without noticing: rotate two polarized sunglass lenses to
90° and they block all light; insert a *third* lens between them at 45° and light
gets through again. The middle lens *measured* the photons diagonally and
*re-prepared* them — changing what the last lens sees. Measurement is an
intervention, not an observation.

**Networking translation:** there is no such thing as reading a qubit "off the
wire" the way tcpdump reads a frame. Reading requires choosing an orientation; a
wrong choice returns noise *and corrupts the payload*. Hold onto that — it is the
entire security model of Part 2.

## 1.3 · No-cloning: qubits cannot be copied

The **no-cloning theorem** (proved in 1982, and it's a theorem — a mathematical
consequence of quantum mechanics, not an engineering limitation): *there is no
device that takes an unknown qubit and produces two copies of it.*

Why you can't just work around it: to copy something you must first learn what it
is, and (§1.2) learning requires measuring in some basis — but you don't know the
right basis, a wrong guess yields a random answer, and the act of asking destroys
the original. Copying without measuring is what the theorem rules out directly.

The consequences invert several networking instincts:

- **No passive taps.** A fiber splitter on a classical link gives the attacker a
  perfect copy of every frame and the endpoints see nothing. On a quantum link
  this is *physically impossible* — any information extraction is an active,
  disturbing operation (and disturbance is detectable, as Part 2 shows).
- **No retransmission of quantum data.** You can't hold a copy for ARQ. Lost
  qubit = gone. Quantum protocols are designed around loss, not around recovery.
- **No amplifiers.** A classical optical amplifier is a copying machine
  (1 photon in → many identical photons out). Illegal for qubits. This single
  fact creates the distance problem of Part 4 and is why repeaters have to be so
  clever.

## 1.4 · Quantum randomness is real randomness

When a qubit measured in the wrong basis answers 50/50, that is not a PRNG, not
thermal noise, not "we lack information." Per the Born rule, the outcome is
fundamentally undetermined until measured — the universe genuinely flips the coin.
(Part 3's Bell test is the experimental proof that no hidden predetermined answer
exists.) Two practical consequences: quantum measurements are a perfect entropy
source, and — more importantly for us — an eavesdropper's wrong-basis measurement
errors are *irreducibly* random. She cannot engineer them away.

---

# Part 2 — BB84: a secret key from physics

## 2.1 · The problem: key distribution

All practical encryption reduces to: **two parties need a shared secret key**.
Today you get one via public-key cryptography (Diffie–Hellman, RSA, ECDH — the
core of TLS). Those rest on *computational assumptions*: factoring and discrete
logs are slow for today's computers.

Two clouds hang over that:

1. **Shor's algorithm** (1994): a sufficiently large quantum computer breaks RSA,
   Diffie–Hellman, and elliptic curves outright. Not "weakens" — breaks.
2. **Harvest now, decrypt later:** an adversary recording your TLS traffic today
   can decrypt it retroactively the day they get that machine (or any
   mathematical breakthrough). Data with a 25-year secrecy requirement is
   *already* at risk.

**Quantum key distribution (QKD)** replaces the computational assumption with a
physical one: any eavesdropping on the key exchange is *detectable*, because of
§1.2 and §1.3. Not "we believe factoring is hard" but "measurement disturbs, and
we measured the disturbance."

**Misconception check:** QKD does not encrypt your data and does not send
messages. It manufactures a shared random key with a security guarantee. The key
is then used with ordinary symmetric encryption (AES, or a one-time pad for the
truly paranoid). Also note what QKD does *not* replace: signatures, certificates,
identity. It solves key distribution only.

## 2.2 · The protocol, step by step

BB84 (Bennett & Brassard, 1984 — the founding protocol of the field). Cast:
**Alice** (sender), **Bob** (receiver), **Eve** (eavesdropper). Two channels:

- a **quantum channel** (fiber carrying single photons), and
- a **classical channel** (ordinary network traffic — assume for now it's
  authenticated but public; Eve reads every byte).

**Transmission.** For each photon, Alice picks two random bits: a *data bit*
(0/1) and a *basis choice* (Z/X). She polarizes one photon accordingly (Z: 0 =
vertical, 1 = horizontal; X: 0 = +45°, 1 = −45°) and sends it. Bob, who cannot
know her basis, picks his *own* random basis per photon and measures.

Per §1.2: where their bases **match** (half the time), Bob reads Alice's bit
perfectly. Where they **differ**, Bob gets a coin flip — garbage, but *known
garbage*, and here's the elegant part: they can find and discard it afterward
**without revealing any data bits**.

**Sifting.** After the photons are done, Alice announces — publicly, over the
classical channel — the *sequence of bases she used* (never the bits!). Bob
replies with which photons he detected and his bases. They discard every position
where the bases differ or the photon was lost. What remains — the **sifted key**
— should be identical on both sides.

A worked example with 8 photons:

| photon # | Alice's bit | Alice's basis | Bob's basis | bases match? | Bob's result |
|---|---|---|---|---|---|
| 1 | 0 | Z | Z | ✓ | **0** |
| 2 | 1 | Z | X | ✗ | random → discard |
| 3 | 1 | X | X | ✓ | **1** |
| 4 | 0 | X | Z | ✗ | random → discard |
| 5 | 1 | Z | Z | ✓ | **1** |
| 6 | 0 | Z | Z | ✓ | **0** |
| 7 | 1 | X | Z | ✗ | random → discard |
| 8 | 0 | X | X | ✓ | **0** |

Sifted key: **0 1 1 0 0** — five shared secret bits from eight photons. Note what
Eve learned from the public conversation: the *bases*, announced only **after**
the photons were already measured — too late to help her, and the bits themselves
never crossed the classical channel at all.

Why this is secure at heart: while the photons were in flight, the basis
information existed **nowhere on the network** — only in Alice's head. Eve cannot
read a qubit correctly without the basis, cannot copy it to decide later
(no-cloning), and cannot measure it without risking disturbance. Her problem is
not computational. It is physical.

## 2.3 · Eve, quantified: the intercept-resend attack

Give Eve the strongest simple strategy: she catches each photon, measures it in a
random basis (best she can do — the basis secret doesn't exist yet), records her
result, and — because Bob must receive *something* or the photon counts as lost —
prepares and sends Bob a fresh photon in the state *she measured*.

Follow one sifted position (Alice's and Bob's bases matched) through Eve's hands:

- **Eve guessed the same basis as Alice — probability ½.** She reads the bit
  correctly and resends a perfect photon. Bob measures it correctly. *Eve learned
  the bit and left no trace.*
- **Eve guessed wrong — probability ½.** Her measurement returns garbage *and*
  (§1.2, case 3) re-prepares the photon in *her* wrong basis. Bob now measures a
  wrong-basis photon **even though his basis matches Alice's** — so his outcome
  is a coin flip: wrong with probability ½.

Error probability per tapped, sifted bit: ½ × ½ = **¼**. So:

> **Intercept-resend on a fraction *f* of photons adds ≈ 0.25·f to the QBER.**

Tap everything (f = 1) and 25% of the sifted key disagrees — a blaring alarm.
Tap only 20% hoping to hide? You add 5% QBER *and learn only ~10% of the bits*
(half your taps used the wrong basis). Information gained and disturbance caused
are physically chained together — Eve cannot have one without the other. That
trade-off, made rigorous, is the security proof of QKD.

## 2.4 · Measuring the damage: QBER

The **QBER** (Quantum Bit Error Rate) is the fraction of sifted positions where
Alice's and Bob's bits disagree. It is *the* security signal.

To estimate it, they must compare bits — but comparing reveals them. So they
**sacrifice a random sample**: publicly compare, say, 10% of the sifted key,
compute the disagreement rate, and **throw those bits away** (they're public
now). The remaining 90% is the key material, with the sample's QBER as its
estimated error rate.

Honest hardware also contributes errors — misaligned optics, detector "dark
counts" (thermal false clicks) — typically a baseline of 1–3%. Alice and Bob
cannot distinguish noise from eavesdropping, so the security analysis makes the
paranoid assumption: **attribute every error to Eve**. The QBER therefore doesn't
answer "is someone listening?" but the only question that matters: *"assuming the
worst, how much could anyone know about this key?"*

One more subtlety that matters more than it looks (§2.8 formalizes it): the
sample is finite. 100 compared bits estimate the true error rate only to within a
few percent — statistics 101, but with security consequences.

## 2.5 · How many secret bits is a noisy key worth?

Suppose QBER = 5%. The sifted key has errors *and* Eve may hold partial
information. How much true secret is in there?

The currency is **entropy**. The binary entropy function h(p) measures the
uncertainty (in bits) of a biased coin: h(0) = 0 (no uncertainty), h(½) = 1
(maximal). At small p it rises *steeply* — h(0.05) ≈ 0.29, h(0.11) ≈ 0.50 — a
little uncertainty costs a lot of entropy, which is why the numbers below bite so
early.

The classic (Shor–Preskill) result: from a sifted key with error rate Q, the
fraction extractable as perfect secret key is

> **r = 1 − 2·h(Q)**

Read it as a budget. Start with 1 (the raw bit). Pay **h(Q)** to fix the errors
(§2.6 — error correction publishes about that much information). Pay **h(Q)**
again to erase Eve's possible knowledge (§2.7 — her potential information is
bounded by the disturbance she'd have caused). What's left is yours.

| QBER | secret fraction 1 − 2h(Q) |
|---|---|
| 1% | 0.84 |
| 3% | 0.61 |
| 5% | 0.43 |
| 8% | 0.20 |
| 11% | **≈ 0.00** |
| >11% | 0 — **abort** |

That **11% threshold** is the most famous number in QKD. Above it the budget goes
negative — no secret key can be distilled, and the only correct response is to
abort and try again later. Note what an abort *is*: not a failure of security but
security *working*. Eve can always vandalize the channel into uselessness (she
can cut the fiber, too) — that's denial of service. What she can never do is
stay under 11% *and* know your key.

## 2.6 · Making the keys identical: error correction

After sifting, Alice's and Bob's strings differ in ~Q of the positions, and
neither knows *where*. They must reconcile — over the public channel, leaking as
little as possible.

The trick is **parities** (XOR of a block of bits — literally the checksum
instinct you already have). One parity bit reveals at most one bit of
information, but says something about a whole block:

1. Split the key into blocks sized so each holds about one error on average
   (higher QBER → smaller blocks).
2. Compare each block's parity publicly. A mismatch means an **odd** number of
   errors inside — at least one, findable.
3. Binary-search the bad block: halve it, compare half-parities, recurse into the
   mismatched half. In log₂(block) comparisons, one error is located and flipped.
4. Blocks with an *even* number of errors (two, typically) pass silently. So:
   reshuffle the key with a shared random permutation and repeat with different
   block boundaries. The pair splits across new blocks and gets caught. The
   polished protocol (called **Cascade**) also back-propagates: fixing a bit in
   pass 3 flips a parity from pass 1, exposing that block's hidden partner error
   — corrections *cascade* until everything is consistent.

Two properties to remember. First, **everything disclosed is counted**: every
parity is one bit of leakage, tallied and deducted in the next step. Second —
the networking angle — Cascade is *chatty*: hundreds of round trips of tiny
parity queries. On a real WAN, error correction's cost is dominated by RTT, not
bandwidth. (This is exactly the kind of effect this project exists to measure.)

## 2.7 · Erasing Eve: privacy amplification

Now the keys are identical, but Eve may hold partial knowledge — a few bits from
lucky same-basis taps, plus all the parities of §2.6. **Privacy amplification**
shrinks the key so that her partial knowledge becomes *no* knowledge.

The intuition, in one tiny example. Suppose the key is 2 bits and Eve knows the
first one. Replace the key with the single bit `key = bit₁ XOR bit₂`. Eve knows
bit₁ — but without bit₂ that tells her *nothing* about the XOR: from her view the
result is a perfect 50/50. One known bit + one unknown bit → one perfectly secret
bit. The key got shorter; her knowledge went to zero.

Scale it up: from an n-bit key of which Eve knows at most k bits' worth, compute
m ≈ n − k output bits where *every output bit is a XOR of a random ~half of all
input bits*. For an output bit to be predictable, Eve would need to know
essentially all its inputs — and every output mixes bits she has with bits she
hasn't. A theorem (the *leftover hash lemma*) makes this exact: pick the mixing
recipe from a suitable random family (a **universal hash** — in practice a random
binary Toeplitz matrix, chosen so it's cheap to share and apply), and Eve's
information about the output is negligible — not "hard to use," but
*information-theoretically absent*.

A lovely detail: the recipe itself is **public**. Alice picks the random matrix,
announces it in the clear, both apply it to their identical reconciled keys, and
both get the identical short key. Its security comes from the *randomness* of the
choice, not its secrecy — Eve, holding fragments, watches them get blended into
uniform noise and can do nothing about it.

**The pipeline is complete.** Every QKD system — and every protocol in this repo
— runs this same four-beat rhythm:

> **transmit & measure → sift → estimate QBER (abort > 11%) → reconcile + amplify**

Out the other end: two identical, provably-secret random strings, or a clean
abort. Nothing in between.

## 2.8 · The fine print (where real systems live)

Three gaps between the textbook story and a deployable system. Each is a concept
in its own right.

**(a) Authentication — the assumption everyone forgets.** We assumed Eve *reads*
the classical channel but can't *modify* it. Drop that and QKD falls to the
classic MITM you know from TLS: Eve runs a full BB84 session with Alice
(pretending to be Bob) and another with Bob (pretending to be Alice), ends up
with two keys, and transparently re-encrypts traffic between them. Nobody sees
any QBER anomaly. The defense is the classical one — **authenticate every
message** (MAC tags with anti-replay counters) so Eve can read but not forge.
Notice the beautiful circularity, resolved: authentication needs a small
pre-shared key, and each QKD round banks part of its output to authenticate the
next. QKD is honestly a key-*growing* protocol: seed secret in, unbounded secret
out. And every tag is bytes-per-message overhead on a real network — measurable
cost.

**(b) Finite keys — the polling problem.** Sampling 100 bits and seeing 3 errors
does not mean QBER = 3%; it means QBER ≈ 3% ± a confidence interval, same as any
poll. Security must price in the possibility that the sample got lucky and the
true rate is higher: use Q + margin in every formula, where the margin shrinks
like 1/√(sample size), plus small explicit penalties for the failure
probabilities of each distillation step. The consequence surprises everyone the
first time: **short sessions yield zero key**. A few thousand noisy bits can be
entirely consumed by the safety margins — the formula outputs 0, correctly.
Real systems run millions of pulses per key for exactly this reason. Key length
is not a rate; it's a *batch* with a minimum viable size that grows with noise.

**(c) Real lasers and the decoy-state trick.** Ideal BB84 assumes exactly one
photon per pulse. Real transmitters are attenuated lasers emitting a **Poisson**-
distributed photon number — usually 0, sometimes 1, occasionally **2+ identical
copies**. Those multi-photon pulses break the no-cloning protection wholesale:
Eve can split one photon off, store it, and measure it *after* the bases are
announced — perfect information, **zero disturbance** (the photon-number-
splitting attack, PNS). No QBER alarm whatsoever.

The countermeasure is a statistical sting operation called **decoy states**.
Alice randomly intersperses pulses at different brightnesses — signal (μ≈0.6
photons average), weak decoy (μ≈0.1), and near-vacuum — *indistinguishable* to
Eve pulse-by-pulse; she attacks them all identically. Afterward Alice reveals
which was which, and both sides compute per-brightness detection rates and error
rates. Here's the trap: a PNS attack helps multi-photon pulses survive and
suppresses single-photon ones, which *bends the detection-vs-brightness curve*
in a way honest physics cannot. If the statistics are consistent, they yield a
certified lower bound on how much of the key came from true single-photon pulses
— and only *that* fraction enters the secret-key budget. Nothing about the
hardware changes; three brightness settings and arithmetic close the loophole.

---

# Part 3 — Entanglement: the strange resource

BB84 needs only single qubits. The rest of quantum networking — repeaters,
teleportation, distributed quantum computing — runs on something stronger.

## 3.1 · What entanglement is

Two qubits can be prepared in a **joint** state that simply is not "qubit A's
state" plus "qubit B's state." The workhorse example is the Bell state |Φ⁺⟩,
pronounced "phi-plus": *the two qubits will agree, but neither has a value yet.*

Concretely: give one half to Alice in Raleigh and the other to Bob in Chicago.
Each measures in the Z basis. Each sees a perfectly random bit. But compare notes:
**always equal**. Both measure in the X basis instead: individually random, again
always equal. The *correlation* is guaranteed; the *values* are born at
measurement time.

Your correct instinct is: "so what? I can do that classically." Write the same
random bit on two cards, mail them — open yours, you instantly know Bob's.
Random, perfectly correlated, boring. The cards had a *pre-existing hidden
value*. The next section is about why entanglement provably is **not** that —
and it matters, because a hidden value can be copied by an eavesdropper, while
entanglement cannot.

There are exactly four Bell states — the four ways two qubits can be maximally
entangled — and it's worth meeting them since Part 4 uses them by name:

| state | Z-basis outcomes | X-basis outcomes |
|---|---|---|
| Φ⁺ | equal | equal |
| Φ⁻ | equal | opposite |
| Ψ⁺ | opposite | equal |
| Ψ⁻ | opposite | opposite |

Note the pattern: each state is a *signature of correlations across two bases*.
No classical pair of cards can have a guaranteed relationship in two
incompatible bases at once — that's precisely where the classical story starts
to crack.

## 3.2 · The Bell test: proving it's not hidden cards

In 1964 John Bell found something remarkable: the card story and the quantum
story make **different, measurable predictions**. The practical form is the
**CHSH test**:

Alice and Bob share many entangled pairs. For each, Alice randomly measures at
one of two detector angles (a₁ or a₂) and Bob at one of two others (b₁ or b₂).
For each of the four angle combinations they compute the *correlation* E — +1
if outcomes always agree, −1 if they always disagree — and combine:

> **S = E(a₁,b₁) − E(a₁,b₂) + E(a₂,b₁) + E(a₂,b₂)**

Now the punchline. If the outcomes were determined by *any* pre-existing hidden
values whatsoever (any "cards", however cleverly constructed): the four terms
can be at most **S ≤ 2**. This is provable with nothing but counting — no
physics in it at all. But quantum mechanics, with entangled pairs and the right
angles (0° and 45° for Alice; 22.5° and 67.5° for Bob), predicts

> **S = 2√2 ≈ 2.83.**

Experiments (from the 1980s to the loophole-free versions in 2015; Nobel Prize
2022) say: **2.83**. Nature violates the classical bound. There are no cards.
The correlations are made at measurement time, and no local pre-existing
information explains them.

**Misconception check — no, this doesn't send information faster than light.**
Alice's outcomes, viewed alone, are perfect coin flips no matter what Bob does;
the correlation is only visible after they *compare notes over a classical
channel*. Entanglement coordinates randomness; it cannot carry a message. Every
entanglement-based protocol has this shape: quantum correlations **plus**
classical communication — which is why quantum networks always ride on
classical networks, never replace them.

Why an engineer should care about S: it is a **certificate**. S > 2 is
something *no classical system can fake* — not an eavesdropper simulating your
source, not a vendor's buggy device claiming to be quantum. Measured violation
= genuine entanglement, endpoint to endpoint. That makes S the natural
"link-quality + security" metric for entanglement networks, which the next two
sections cash in.

## 3.3 · QKD from entanglement: E91 and BBM92

Flip BB84 around (Ekert, 1991). Instead of Alice manufacturing states, a
**source** — anywhere, even Eve's own equipment! — distributes entangled pairs
to Alice and Bob, who each measure in randomly chosen bases.

- Where their bases match: perfectly correlated random bits → after the same
  sift/QBER/reconcile/amplify pipeline as §2, a key. (The variant that uses just
  Z/X this way is called BBM92 — "entanglement-based BB84.")
- On a random subset, they instead measure at the **CHSH angles** and compute S.

S is the security test, and it's *stronger* than BB84's: if Eve entangled
herself with the pairs, measured them, stored copies — anything — the three-way
correlations mathematically cannot keep S above 2 for Alice–Bob. **A measured
violation certifies that no third party holds correlated information — without
trusting the source at all.** The paranoid endpoint: you can buy your entangled
pairs from your adversary and still get a provably private key, because the
certificate is measured by you, at your endpoints, on physics.

## 3.4 · Noise, fidelity, and the one-knob model

Real entanglement is imperfect — fibers depolarize, sources misfire, detectors
add noise. The standard way to describe a degraded pair is the **Werner state**,
a one-parameter noise model with a beautifully simple reading:

> With probability **f**, the pair is a perfect |Φ⁺⟩.
> With probability **1 − f**, it's complete garbage (all correlation lost).

That parameter f (loosely, the pair's **fidelity** — its quality on a 0-to-1
scale) turns out to control everything at once:

- **Key errors:** matching-basis QBER = **(1 − f)/2**. (The garbage fraction
  agrees only by luck, i.e. half the time.)
- **Bell violation:** **S = 2√2 · f**. (Garbage contributes zero correlation,
  diluting S linearly.)

One dial, both consequences — and they move together, with an instructive
ordering. At f = 1: QBER 0, S = 2.83. At f = 0.95: QBER 2.5%, S = 2.69. At
f = 0.78 the QBER hits 11% — **the key dies first**. The Bell violation survives
down to f ≈ 0.71 (where S touches 2): there's a band of quality where the pair
is still *provably quantum* yet too noisy to distill a single secret bit.
"Entangled" and "useful" are different bars, and the key-rate bar is higher. The
"is my key clean?" metric and the "is this really quantum?" metric are two faces
of one quantity — both measure how much genuine entanglement survived the
channel. Keep the f-dial in mind; it is the star of Part 4's bad news.

---

# Part 4 — Distance: the enemy, and the repeater idea

## 4.1 · Why you can't just use longer fiber

Optical fiber absorbs photons at a blistering **exponential** rate — standard
telecom fiber loses about 0.2 dB/km, i.e. each kilometer transmits ~95.5% of
photons. Sounds fine until you compound it:

| fiber length | photon survival | 1 GHz source delivers… |
|---|---|---|
| 10 km | 63% | ~630 M/s — great |
| 50 km | 10% | ~100 M/s — fine |
| 100 km | 1% | ~10 M/s — fine |
| 300 km | 10⁻⁶ | ~1,000/s — painful |
| 500 km | 10⁻¹⁰ | ~0.1/s — barely usable |
| 1,000 km | 10⁻²⁰ | **one photon every ~3,000 years** |

Classical networking solved the identical problem a century ago: amplify or
regenerate every ~80 km. **Both are copying operations, and no-cloning (§1.3)
forbids them for qubits.** There is no quantum amplifier, period. Direct
transmission tops out around 300–500 km, and no better laser or fiber will
change an exponential. Going farther needs a different *idea*.

## 4.2 · Entanglement swapping: a chain of introductions

The idea (and it's genuinely clever): **stop trying to send a qubit the whole
way. Build long-distance entanglement out of short-distance entanglement.**

Set up a middle station R between Alice and Bob:

```
Alice ────── pair 1 ────── R ────── pair 2 ────── Bob
  qubit A         qubit R₁   qubit R₂        qubit B
```

Two *short* links, each easily covered by direct transmission: A↔R₁ entangled,
R₂↔B entangled. Note A and B share nothing yet — no photon has traveled more
than half the distance, and never will.

Now the station performs a **Bell-state measurement (BSM)** on its two qubits
R₁ and R₂ — a joint measurement that asks not "what is each qubit?" but *"which
of the four Bell states are you two in, relative to each other?"* The output is
2 bits naming one of Φ⁺/Φ⁻/Ψ⁺/Ψ⁻ (§3.1's table).

The consequence is the magic: the measurement consumes R's two qubits, and
**projects A and B — which never met — into an entangled pair**. The intuition:
before the BSM, A "agrees with" R₁, and R₂ "agrees with" B. The BSM measures
precisely the *relationship between R₁ and R₂* — and once that relationship is
fixed, A's relationship to B is fixed too, by transitivity of agreements. Two
introductions through a mutual friend, and the friend then leaves the room.

Two footnotes that matter enormously in practice:

- **The 2-bit outcome is random** — the pair A–B lands in one of the four Bell
  states, and only the station knows which. It must send those 2 bits (the
  **herald**) to an endpoint over the *classical* network, and the endpoint
  applies a simple corrective flip to standardize the pair to Φ⁺. Until the
  herald arrives, B's qubit is *useless noise* — averaged over the four unknown
  outcomes, all correlation washes out (QBER = exactly 50%). **The entanglement
  effectively travels over the classical channel.** Herald latency is a hard
  serialization delay in every repeater protocol — a first-class *networking*
  cost inside a quantum protocol. (Incidentally, "measure, send 2 classical
  bits, apply correction" is also exactly how **quantum teleportation** moves a
  qubit state without moving a particle — swapping *is* teleportation applied
  to half of a pair.)
- **It chains.** Five stations make a six-link chain: every station swaps, five
  heralds fly, and the two ends of a continent-scale path share a pair. This —
  plus quantum memories to hold qubits while heralds are in flight, and
  per-link retry (each short link can regenerate a lost pair *independently*,
  which is what finally beats the exponential) — is the **quantum repeater**
  architecture.

## 4.3 · The price: quality multiplies

There's no free lunch. Swapping *composes* imperfections: joining a pair of
quality f with another pair of quality f yields an end-to-end pair of quality
**f²**. Across L links:

> **quality dial after L swapped links = f^L → end-to-end QBER = (1 − f^L)/2**

Exponential again — but now in *hop count* with base f, rather than in
*kilometers* with base 0.955/km. That's the trade. Concretely, with excellent
f = 0.95 links:

| links L | end-to-end dial f^L | QBER | S (Bell) | verdict |
|---|---|---|---|---|
| 1 | 0.95 | 2.5% | 2.69 | comfortable |
| 2 | 0.90 | 4.9% | 2.55 | fine |
| 3 | 0.86 | 7.1% | 2.42 | getting warm |
| 4 | 0.81 | 9.3% | 2.30 | near the cliff |
| 5 | 0.77 | 11.3% | 2.19 | **QBER over 11% — no key** |

Five hops of *quite good* links and the key rate hits zero — while, notice, the
Bell violation is still alive (S > 2): entanglement outlives usefulness-for-QKD.
And recall §2.8(b): near the cliff, the finite-key margins get vicious, so
practical block sizes explode before the asymptotic limit even bites.

This table is the design space of quantum networking, compressed: *repeaters
trade the fiber exponential for a hop exponential, so everything hinges on
per-link fidelity — and on the classical machinery (heralds, retries, memory
lifetimes) that turns fragile links into reliable ones.* The full repeater
roadmap adds **entanglement distillation** (burn several mediocre pairs to
forge one good one — pushing f back up between hops, at the cost of yet more
classical round trips) — at which point arbitrary distances open up.

## 4.4 · Why this is a networking problem

Step back and look at what Part 4 actually contains, with your systems hat on:

- multi-hop **path building** through intermediate stations (routing),
- a hard dependency on **control-plane messages** — heralds — whose latency
  serializes the data plane (signaling),
- per-link **retry against loss** with buffering in quantum memories whose
  contents *decay in real time* (ARQ with a TTL measured in physics),
- **batching and rates** dictated by finite-key statistics (§2.8b),
- chatty **error correction** whose cost is RTT-bound (§2.6),
- and **authentication** overhead on every classical byte (§2.8a).

Every one of these is a classical networking concern, and the quantum layer's
performance is *gated* by them. A quantum network is not a replacement for the
classical internet — it is a new service that runs **on top of** it and is only
as fast, reliable, and secure as its classical substrate. Measuring exactly how
real-world network conditions (latency, jitter, loss, congestion) throttle
quantum protocols is an open research area — and it is the reason this
repository exists: the quantum channel is emulated faithfully, precisely so the
classical channel can be completely real.

---

# Part 5 — The big picture

## 5.1 · The quantum internet, in stages

The research community sketches the future as a capability ladder — each stage
subsuming the last:

1. **Trusted-node QKD networks** — QKD links chained through trusted relay
   sites. Deployed today (metro networks in several countries; China's
   2,000 km Beijing–Shanghai backbone; the 2017 Micius satellite doing QKD to
   ground stations). The relays must be trusted — the key exists in the clear
   inside them — which is exactly the weakness repeaters remove.
2. **Entanglement distribution networks** — end-to-end entanglement via
   swapping (Part 4); E91-style keys with *untrusted* middles, certified by
   Bell tests. Metro-scale demos exist (three-node networks in the lab since
   ~2021); long-distance is the active frontier.
3. **Quantum memory networks** — add storage and distillation; teleportation
   of arbitrary qubit states on demand; blind/delegated quantum computing
   (use a remote quantum computer without revealing your data or program).
4. **Distributed quantum computing** — networked quantum processors acting as
   one machine; entanglement-linked telescopes and clock networks; sensor
   arrays with precision beyond any classical limit.

Stage 1 is products; stage 2 is prototypes; stages 3–4 are laboratories and
theory. The classical-network substrate, though, is common to all of them —
lessons about it transfer up the whole ladder.

## 5.2 · The concept map — everything on one page

```
GROUND RULES (Part 1)
  superposition + Born rule ─── measurement disturbs ─── no-cloning ─── true randomness
        │                              │                      │
        ▼                              ▼                      ▼
BB84 (Part 2): encode in 2 bases → wrong-basis reads corrupt → taps are detectable
        │
        ▼   the universal pipeline
  sift → QBER sample → [ >11%? ABORT ] → error-correct (count leaks) → privacy-amplify
        │                                        plus the fine print:
        │                    authentication · finite-key margins · decoy states
        ▼
ENTANGLEMENT (Part 3): Bell pairs → CHSH > 2 certifies "no hidden values, no third party"
        │                               → E91: keys from an untrusted source
        ▼
DISTANCE (Part 4): fiber loss is exponential → no amplifiers allowed
        → entanglement swapping (BSM + classical herald) → repeater chains
        → cost: fidelity f^L → the hop cliff → memories, retries, distillation
        ▼
THE POINT: every arrow above leans on classical networking —
           heralds, parities, auth tags, batching — all riding real networks.
```

## 5.3 · Where to go from here

- **See the concepts live:** notebooks `10` (watch Eve raise the QBER and kill
  the key rate), `09` (entanglement + a CHSH violation), `12` (a repeater chain,
  the f^L law, and what happens when heralds don't arrive), `13` (finite keys,
  authentication, decoy states — the §2.8 fine print, running).
- **From concepts to code:** `CONCEPTS.md` — the same topics in the same order,
  each mapped to the source files, tests, and measured results in this repo.
- **Books, when you want depth:** *Quantum Computation and Quantum Information*
  (Nielsen & Chuang) — the standard text; Wehner, Elkouss & Hanson, *"Quantum
  internet: a vision for the road ahead"* (Science, 2018) — the stages of §5.1
  in the authors' own words.

*You now know the ground rules, the protocol they enable, the resource that
generalizes it, the obstacle, and the architecture that overcomes it. That is
quantum networking. The rest — including everything in this repository — is
making each of those five sentences real, measurable, and honest.*
