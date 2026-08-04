# Reference: Rusca et al. (2018) — 1-decoy finite-key analysis

Transcription of **Appendix A ("Calculation of the SKR")**, Eq. (A1)–(A25), from:

> D. Rusca, A. Boaron, F. Grünenfelder, A. Martin, H. Zbinden,
> *"Finite-key analysis for the 1-decoy state QKD protocol"*,
> **Appl. Phys. Lett. 112, 171104 (2018)**, DOI [10.1063/1.5023340](https://doi.org/10.1063/1.5023340).
> arXiv preprint (title: "…*on* the 1-decoy state QKD protocol"): [arXiv:1801.03443v2](https://arxiv.org/abs/1801.03443).
> Local copy: `../quantum-network-pdfs/rusca_1decoy_finite_key_1801.03443.pdf`

The analysis follows the 2-decoy finite-key proof of **Lim, Curty, Walenta, Xu, Zbinden**,
*"Concise security bounds for practical decoy-state QKD"*, Phys. Rev. A **89**, 022307 (2014)
([arXiv:1311.7129](https://arxiv.org/abs/1311.7129)) — cited as [14] below — and the asymptotic
analysis of **Ma, Qi, Zhao, Lo**, Phys. Rev. A **72**, 012326 (2005) — cited as [12].

Only **two** intensity levels are used: κ = {μ₁, μ₂} with μ₁ > μ₂ (a "1-decoy" protocol —
signal + one decoy, **no vacuum state**). Everything is written for the Z basis; the X basis
is identical with Z → X.

---

## Notation

| Symbol | Meaning |
|---|---|
| κ = {μ₁, μ₂} | intensity set, μ₁ > μ₂ |
| p_k | probability Alice sends intensity k ∈ κ |
| n_Z, m_Z | total detections / total errors in the Z basis (observed) |
| n_{Z,k}, m_{Z,k} | detections / errors in the Z basis for intensity k (observed) |
| s_{Z,n} | detections at Bob given Alice sent an n-photon state (n_Z = Σ_n s_{Z,n}) |
| v_{Z,n} | errors at Bob given Alice sent an n-photon state (m_Z = Σ_n v_{Z,n}) |
| ⋆* (e.g. n*_{Z,k}) | asymptotic (infinite-statistics) value of ⋆ |
| ⋆^l / ⋆^u | lower / upper bound |
| τ_n | total probability of emitting an n-photon state |
| φ_Z | phase error rate in the Z basis |
| λ_EC | bits leaked by error correction |
| ε_sec, ε_cor | secrecy and correctness parameters |
| ε₁, ε₂ | failure probabilities of the two Hoeffding bounds (detections / errors) |
| h(·) | binary Shannon entropy |
| ℓ | secret key length |

Total n-photon emission probability and the Bayes-rule conditional (Eq. A5):

```
τ_n = Σ_{k∈κ} p_k e^{−k} k^n / n!

p_{k|n} = p_k e^{−k} k^n / (τ_n · n!)                                     (A5)
```

---

## Finite-statistics (Hoeffding) corrections

```
n*_{Z,k} = Σ_{n=0}^{∞} p_{k|n} s_{Z,n},        ∀k ∈ κ                     (A1)

|n*_{Z,k} − n_{Z,k}| ≤ δ(n_Z, ε₁)      with probability 1 − 2ε₁           (A2)
        where   δ(n, ε) := sqrt( n · log(1/ε) / 2 )

m*_{Z,k} = Σ_{n=0}^{∞} p_{k|n} v_{Z,n},        ∀k ∈ κ                     (A3)

|m*_{Z,k} − m_{Z,k}| ≤ δ(m_Z, ε₂)      with probability 1 − 2ε₂           (A4)
```

Shorthand for the finite-key-corrected counts (Eq. A18):

```
n^±_{Z,k} := ( n_{Z,k} ± δ(n_Z, ε₁) ),         ∀k ∈ κ                     (A18)
```

> ⚠️ **As printed, (A18) omits the `e^k / p_k` prefactor.** Substituting (A18) verbatim into
> (A17)/(A19) does not reproduce (A10); consistency with (A10) and with Lim et al. [14]
> requires `n^±_{Z,k} := (e^k / p_k)(n_{Z,k} ± δ(n_Z, ε₁))`. Use the Lim form when implementing.
> Similarly (A16) writes `δ(m_Z, ε₁)` where (A4) defines the error-count deviation with **ε₂**.

---

## Single-photon detection lower bound

Starting from (A1) with the two intensities:

```
e^{μ₂} n_{Z,μ₂}/p_{μ₂} − e^{μ₁} n_{Z,μ₁}/p_{μ₁}
    = (μ₂ − μ₁) s_{Z,1}/τ₁ + Σ_{n≥2} (μ₂^n − μ₁^n) s_{Z,n} / (n! τ_n)
    ≤ (μ₂ − μ₁) s_{Z,1}/τ₁ + ((μ₂² − μ₁²)/μ₁²) Σ_{n≥2} μ₁^n s_{Z,n} / (n! τ_n)   (A6)
```

where the inequality uses, for n ≥ 2 and μ₁ > μ₂:

```
μ₂^n − μ₁^n = μ₂² μ₂^{n−2} − μ₁² μ₁^{n−2} ≤ (μ₂² − μ₁²) μ₁^{n−2}          (A7)
```

and the multi-photon sum is eliminated with:

```
Σ_{n≥2} μ₁^n s_{Z,n} / (n! τ_n) = e^{μ₁} n_{Z,μ₁}/p_{μ₁} − s_{Z,0}/τ₀ − μ₁ s_{Z,1}/τ₁   (A8)
```

Substituting (A8) into (A6):

```
e^{μ₂} n_{Z,μ₂}/p_{μ₂} − e^{μ₁} n_{Z,μ₁}/p_{μ₁}
    ≤ (μ₂ − μ₁) s_{Z,1}/τ₁
      + ((μ₂² − μ₁²)/μ₁²) [ e^{μ₁} n_{Z,μ₁}/p_{μ₁} − s_{Z,0}/τ₀ − μ₁ s_{Z,1}/τ₁ ]   (A9)
```

Isolating the single-photon contribution:

```
s_{Z,1} ≥ (τ₁ μ₁) / (μ₂ (μ₁ − μ₂))
          × [ e^{μ₂} n_{Z,μ₂}/p_{μ₂}
              − (μ₂²/μ₁²) e^{μ₁} n_{Z,μ₁}/p_{μ₁}
              − ((μ₁² − μ₂²)/μ₁²) · s_{Z,0}/τ₀ ]                          (A10)
```

**Key structural point of the 1-decoy protocol:** (A10) contains `s_{Z,0}` with a *negative*
sign, so a *lower* bound on s_{Z,1} needs an **upper** bound on the vacuum events. With only
two intensities that bound cannot be made tight — this is the whole cost of dropping the
vacuum decoy.

---

## Upper bound on the vacuum events (two routes)

**Route 1 (loose, all errors).** From

```
m_Z = Σ_{k=μ₁,μ₂} p_{k|n} Σ_{n=0}^{∞} v_{Z,n} ≥ v_{Z,0}                   (A11)
```

and the fact that vacuum detections carry no information for either Bob or Eve, so errors on
vacuum events occur with probability ½ [12]:

```
⟨v_{Z,0}⟩ / s_{Z,0} = 1/2                                                  (A12)

⟨v_{Z,0}⟩ ≤ v_{Z,0} + δ(s_{Z,0}, ε₁) ≤ m_Z + δ(n_Z, ε₁)                    (A13)
```

(the second inequality because m_Z ≥ v_{Z,0} and n_Z ≥ s_{Z,0}; needed because v_{Z,0} and
s_{Z,0} are not directly observable, while m_Z and n_Z are). Hence

```
s_{Z,0} ≤ s^u_{Z,0} := 2 ( m_Z + δ(n_Z, ε₁) )                              (A14)
```

**Route 2 (used in the paper — gives the better SKR).** Use only the errors of one intensity:

```
m*_{Z,k} = Σ_n p_{k|n} v_{Z,n} = Σ_n (p_k e^{−k} k^n / (τ_n n!)) v_{Z,n}
         ≥ (p_k / τ₀) e^{−k} v_{Z,0}                                       (A15)
```

which, with the finite-key correction, gives

```
s_{Z,0} ≤ s^u_{Z,0} := 2 [ τ₀ (e^k / p_k)( m_{Z,k} + δ(m_Z, ε₁) ) + δ(n_Z, ε₁) ]   (A16)
```

---

## Final single-photon and vacuum bounds

Inserting (A16) into (A10) and applying the finite-key corrections:

```
s_{Z,1} ≥ s^l_{Z,1} := (τ₁ μ₁) / (μ₂ (μ₁ − μ₂))
          × [ n^-_{Z,μ₂} − (μ₂²/μ₁²) n^+_{Z,μ₁} − ((μ₁² − μ₂²)/μ₁²) · s^u_{Z,0}/τ₀ ]   (A17)
```

Vacuum lower bound, taken from Lim et al. [14]:

```
s_{Z,0} ≥ s^l_{Z,0} := (τ₀ / (μ₁ − μ₂)) [ μ₁ n^-_{Z,μ₂} − μ₂ n^+_{Z,μ₁} ]  (A19)
```

---

## Phase error rate

Random-sampling-without-replacement bound (ref. [21] in the paper):

```
φ_Z := c_{Z,1}/s_{Z,1} ≤ v_{X,1}/s_{X,1} + γ( ε_sec, v_{X,1}/s_{X,1}, s_{Z,1}, s_{X,1} )   (A20)

γ(a, b, c, d) = sqrt(  ((c + d)(1 − b) b) / (c d log 2)
                     · log₂[ ((c + d) / (c d (1 − b) b)) · (2^21 / a²) ]  )  (A21)
```

Single-photon X-basis bit errors, upper bounded as in [14]:

```
v_{X,1} ≤ v^u_{X,1} = (τ₁ / (μ₁ − μ₂)) ( m^+_{X,μ₁} − m^-_{X,μ₂} )         (A22)
```

giving the phase error rate bound

```
φ_Z ≤ φ^u_X := v^u_{X,1}/s^l_{X,1} + γ( ε_sec, v^u_{X,1}/s^l_{X,1}, s^l_{Z,1}, s^l_{X,1} )   (A23)
```

---

## Security parameter and the secret key length

The security analysis is that of Lim et al. [14], which yields the coefficients **a = 6, b = 21**
in the `−a log₂(b/ε_sec)` term. The only difference for 1-decoy is the composition of ε_sec:

```
ε_sec = 2 [ α₁ + 2α₂ + α₃ ] + ν + 6ε₁ + 4ε₂                                (A24)
```

where α₁, α₂, α₃ and ν are the error terms of the security analysis [14]. The coefficients of
ε₁ and ε₂ count how many times the concentration inequalities (A2) and (A4) are applied in the
key-length formula. Setting every error term to a common value ε gives **ε_sec = 19ε**, hence:

```
ℓ ≤ s^l_{Z,0} + s^l_{Z,1} ( 1 − h(φ^u_Z) ) − λ_EC
      − 6 log₂(19 / ε_sec) − log₂(2 / ε_cor)                               (A25)
```

**Reading (A25):** every vacuum detection is fully secret (Eve learns nothing from an empty
pulse), every single-photon detection contributes `1 − h(φ^u_Z)` after privacy amplification,
multi-photon detections contribute nothing (they are conceded to PNS), then subtract the
error-correction leakage and the finite-key security/correctness penalties. Compare the
2-decoy version in Lim et al. [14], Eq. (1), which has the same shape with `6 log₂(19/ε_sec)`
replaced by `a log₂(b/ε_sec)` for their parameter values.

---

## Appendix B — channel/detector simulation model (for generating the inputs)

Used to synthesize `n_{Z,k}` and `m_{Z,k}` at fixed total n_Z, so (A25) can be evaluated
without an experiment:

```
n_{Z,μi} = n_Z · P_{Z,det,μi} / P_{Z,det,tot}                              (B1)

P_{Z,det,μi} = c_DT · P_Z · P_μi ( 1 − e^{−μi η} + P_DC )                  (B2)

c_dt = 1 / ( 1 + R · P_{Z,det,tot} · t_DT )                                (B3)

P_{Z,err,μi} = c_dt · P_Z · P_μi [ (1 − e^{−μi η}) P_Err + P_DC/2 ]        (B4)

QBER = m_Z / n_Z = P_{Z,err,tot} / P_{Z,det,tot}                           (B5)

m_{Z,μi} = n_Z · P_{Z,err,μi} / P_{Z,det,tot}                              (B6-equivalent, "similarly to B1")
```

with η the global transmission, P_Z the probability both parties pick Z, P_μi the intensity
probability, P_Err the misalignment error probability, P_DC the dark-count probability,
R the source repetition rate and t_DT the detector dead time.

Appendices C and D of the paper cover the remaining details (optimization of the free
parameters p_Z, p_μ1, μ₁, μ₂ vs. attenuation is shown in Figs. 3–4).

---

## Relation to the current qfabric implementation

`qne/decoy.py` currently implements the **asymptotic, 3-intensity Lo–Ma–Chen + GLLP** rate
(signal / decoy / vacuum, `decoy_state_key_rate`) — no finite-key terms. The Rusca analysis
differs on three axes, which is what makes it worth porting:

1. **Two intensities, not three.** No vacuum state; the price is the loose `s^u_{Z,0}` bound
   in (A14)/(A16) instead of a direct measurement of Y₀.
2. **Counts, not rates.** All bounds are on *integer event counts* (`n_{Z,k}`, `m_{Z,k}`) with
   explicit Hoeffding deviations `δ(n, ε)`, rather than on asymptotic gains Q_μ / QBERs E_μ.
   Our `simulate_intensity` / `run_decoy_experiment` already produce per-basis counts, so the
   inputs exist.
3. **Composable finite-key output.** (A25) returns a key *length* ℓ for a given block size with
   stated (ε_sec, ε_cor), not a rate per pulse — which is the quantity that actually matters for
   the block sizes (≤ 10⁸) we run on the FABRIC slice, and where the paper's headline result
   lives (1-decoy beats 2-decoy below ~10⁸).

Implementation notes if/when this is ported:
- Use the Lim-et-al. form of (A18) (`n^± := (e^k/p_k)(n ± δ)`), not the printed one — see the
  warning above.
- Clamp `s^l_{Z,0}`, `s^l_{Z,1}` at 0 and return ℓ = 0 when the bound goes negative; at high
  attenuation (A17) goes negative long before the asymptotic rate does.
- `ε_sec = 19ε` only holds under the "all error terms equal" choice; keep ε as the single knob.
