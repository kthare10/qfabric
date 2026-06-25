# Publishing QFabric to the FABRIC Artifact Manager

This file records the metadata and steps for sharing QFabric as an artifact on the
[FABRIC Artifact Manager](https://artifacts.fabric-testbed.net). Keep it in sync so
re-uploads and new versions stay consistent.

## 1. Build the artifact tarball

```bash
bash scripts/package_artifact.sh v0.1.0
# -> dist/qfabric-v0.1.0.tgz   (clean: no .venv, .git, caches, or personal files)
```

The tarball extracts to a top-level `qfabric/` directory and bundles sample results
(`results/fabric_*_results.json`) so the analysis notebook runs without a slice.

## 2. Artifact metadata

Enter these in the Artifact Manager UI (or via the REST API — fields below map to the
`ArtifactCreate` schema in `/api/schema/`).

| Field | Value |
|-------|-------|
| `title` (required) | `QFabric: Quantum Network Emulation on FABRIC (BB84 QKD over P4)` |
| `description_short` (≤255) | `Programmable quantum-channel emulation with P4/BMv2; BB84 QKD cross-validated against SeQUeNCe & NetSquid.` |
| `description_long` (required, ≤5000) | See below |
| `authors` (required) | Komal Thareja — RENCI, UNC Chapel Hill — kthare10@renci.org (+ FABRIC author UUID) |
| `tags` (required) | `quantum-networking`, `qkd`, `bb84`, `p4`, `bmv2`, `emulation`, `sequence`, `netsquid` |
| `visibility` | `project` first; switch to `public` when ready (`author` \| `project` \| `public`) |
| `project_uuid` | your FABRIC project UUID |

### description_long (paste into the form)

> QFabric is a programmable quantum network emulation platform built on the FABRIC
> testbed. It emulates fiber attenuation as probabilistic packet drop in a P4/BMv2
> data plane (custom EtherType 0x7101 photon frames) and runs BB84 QKD across real
> FABRIC links: Alice (photon source) → P4 switch (quantum channel) → Bob (detector
> model with efficiency, dark counts, and polarization-misalignment QBER), with
> classical sifting over the data-plane link so genuine WAN latency/jitter enters the
> protocol. A cross-validation framework runs the measured FABRIC result, a QFabric
> simulation, SeQUeNCe 1.0, and NetSquid — each on its own slice node — and checks
> statistical agreement on QBER and secure key rate. A linear notebook workflow
> (overview → set up slice → run experiment → cross-validate → analysis) drives the
> whole thing on FABRIC. See README.md, SPEC.md, and ROADMAP.md.

## 3. Upload

- **Web UI:** create the artifact with the metadata above, then upload `dist/qfabric-v0.1.0.tgz` as a version (set the version string, e.g. `0.1.0`).
- **REST API:** `POST /api/artifacts` (metadata) then `POST /api/contents` (multipart `file=@dist/qfabric-v0.1.0.tgz`, `data={artifact:<uuid>, storage_type:fabric, storage_repo:renci}`).

## 4. Pre-upload checklist

- [ ] `pytest tests/ -v` passes
- [ ] `python -m validation.compare validation/scenarios/baseline_1km.yml` reports honestly (uninstalled simulators show SKIPPED, not PASS; <2 backends → INCONCLUSIVE)
- [ ] Notebooks 0–4 run in order on a FABRIC slice (01 setup → 02 run → 03 cross-validate → 04 analysis)
- [ ] No personal paths/secrets (kiso `rc_file` uses `${FABRIC_RC}` / `${HOME}`; NetSquid creds come from `NETSQUID_USER`/`NETSQUID_PASS`, not hard-coded)
- [ ] Tarball excludes `.venv*`, `.git`, caches, `cc-usage-log.md`, generated `cross_validation.json`
- [ ] LICENSE (Apache-2.0) and CITATION.cff present
