# Figures

Regenerate with:

```bash
python paper/make_figures.py
```

| File | What it shows |
|------|---------------|
| `sweep_distance.png` | QBER and secure key rate vs fiber distance (QFabric simulation). QBER stays at the intrinsic ≈ (1−F)/2 floor; key rate falls as loss grows with distance. |
| `sweep_attenuation.png` | QBER and secure key rate vs fiber attenuation at fixed distance. |

These come from the QFabric pure-Python simulation over the `sweep_distance` /
`sweep_attenuation` scenarios (no FABRIC slice required), so they're reproducible
in CI/locally. For the measured FABRIC vs SeQUeNCe vs NetSquid sweep, run
`05_run_all_scenarios` and plot `results/all_scenarios.json`.
