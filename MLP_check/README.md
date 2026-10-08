# Machine-learning potential scoring

AutoCata has two independent scoring paths:

| Entry point | Models | Output meaning |
| --- | --- | --- |
| `OC20_MLP/mlp_scores.py` | Existing fairchem OC20 checkpoints | Existing `E_pred` and workflow filtering behavior |
| `score_structures.py` | MACE, CHGNet | Single-point total energy and raw force statistics |

The new entry point scores existing XYZ structures. It does not generate,
relax, train, run MD, or change input files. The production workflow runners
continue to use OC20. MACE/CHGNet results are not fed into their filters.

## Installation

Run from the repository root. Use separate environments: the existing AutoCata
environment pins Python 3.9 and older scientific libraries, while CHGNet 0.4.1
requires Python >=3.10 and newer dependencies. Do not upgrade the OC20
environment to install these backends. The scorer runs directly from the
checkout; do not run `pip install -e .` in these Python 3.11 environments,
because the core package currently requires Python 3.9.

```bash
conda env create -f envs/mlp_mace.yml
conda activate autocata-mace
python MLP_check/score_structures.py --config config/mlp_mace.yml --dry-run
```

For CHGNet:

```bash
conda env create -f envs/mlp_chgnet.yml
conda activate autocata-chgnet
python MLP_check/score_structures.py --config config/mlp_chgnet.yml --dry-run
```

These environment specifications are installation recipes, not fully resolved
lockfiles. Actual model installations and real inference have not been tested
as part of this integration. After validating an installation, record it with
`python -m pip freeze`; each scoring report also records key package versions.
CHGNet includes pretrained weights in its package. MACE may fetch the selected
pretrained checkpoint on the first real run. Environment installation and a
real run therefore differ from the scorer's dry-run, which never imports a
model package, loads weights, downloads anything, or creates output files.

## First calculation on existing results

The example configs select the first five structures from the restored
`outputs/workflows/ch3_ni_20_v2/success_summary.csv`. After checking the dry-run,
run the same command without `--dry-run` in the appropriate environment:

```bash
# In autocata-mace:
python MLP_check/score_structures.py --config config/mlp_mace.yml

# In autocata-chgnet:
python MLP_check/score_structures.py --config config/mlp_chgnet.yml
```

The respective new output directories are:

```text
outputs/workflows/ch3_ni_20_v2/mlp_comparison/mace_run_001/
outputs/workflows/ch3_ni_20_v2/mlp_comparison/chgnet_run_001/
```

Each contains `scores.csv` and `scoring_report.json`. Use a fresh output
directory for each subsequent run; nonempty directories are refused.
CPU is the default. `--device cuda` explicitly requests GPU and fails if CUDA
is unavailable instead of silently falling back to CPU.

To score a reaction-network success table:

```bash
python MLP_check/score_structures.py --config config/mlp_mace.yml \
  --success-summary outputs/reaction_workflows/co2rr_methanol/all_success_structures.csv \
  --output-dir outputs/reaction_workflows/co2rr_methanol/mlp_comparison/mace_run_001 \
  --max-files 5 --dry-run
```

Alternatively, supply a directory; nested adsorbate folders are included:

```bash
python MLP_check/score_structures.py --backend mace --model medium-mpa-0 \
  --xyz-dir outputs/workflows/ch3_ni_20_v2/success_structures \
  --output-dir outputs/workflows/ch3_ni_20_v2/mlp_comparison/mace_run_002 \
  --max-files 5 --dry-run
```

CLI values override config values. Paths in configs and CSVs are resolved
relative to the repository root, matching existing workflow outputs. Absolute
paths are also accepted. CSV order is retained; directory inputs use sorted
relative paths. `--all-files` explicitly removes the default five-file limit.
The success-table reader retains material and adsorbate labels when available;
older single-adsorbate tables may not contain the adsorbate column.

## Models and local checkpoints

MACE uses the ASE `mace_mp` factory with the explicitly named
`medium-mpa-0` default. Use `--model` to choose another compatible MACE-MP
factory model, or a local standard energy/force MACE checkpoint:

```bash
python MLP_check/score_structures.py --config config/mlp_mace.yml \
  --checkpoint /absolute/path/to/custom.model \
  --output-dir outputs/workflows/ch3_ni_20_v2/mlp_comparison/mace_custom_001 \
  --dry-run
```

CHGNet defaults to pretrained model version `0.3.0` (this is the weight
version, separate from package version `0.4.1`). `--checkpoint` accepts a local
checkpoint loadable by `CHGNet.from_file`. It takes precedence over `--model`
and bypasses pretrained loading. Only load trusted model files. Dry-runs check
checkpoint existence, not its compatibility or contents.

MACE precision is configurable with `--dtype float32` or `float64` (default).
CHGNet uses its model's precision; this option does not cast CHGNet weights.
MACE dispersion corrections are disabled in this initial adapter.

## Energy, forces, and boundary conditions

- `E_total_eV`: total energy of the entire input structure, in eV.
- `E_per_atom_eV`: total energy divided by atom count, in eV/atom.
- `F_rms_eV_A`: square root of the mean squared Cartesian force components.
- `F_max_eV_A`: largest per-atom force-vector norm.
- `energy_kind`, units, backend and model are explicitly recorded.
- `pbc_used` records the actual periodic boundary flags used for calculation.

CHGNet's direct prediction API returns energy per atom, but its ASE calculator
already returns total energy. This adapter uses the latter and does not
multiply by atom count a second time. Force statistics use unconstrained
model forces, even when the XYZ contains ASE constraints.

There is intentionally no `E_pred` column or automatic cross-material energy
ranking. Total energies across different compositions, atom counts or model
reference conventions must not be compared as adsorption energies. In
particular, applying the existing `E_pred < 0` criterion to these totals is
not meaningful. Adsorption energies require a consistent reference scheme,
typically `E(slab+adsorbate) - E(slab) - E(reference)`, with clearly defined
reference species and geometry/relaxation conditions. Such reference-energy
calculations and workflow filter integration are not implemented here.

`--pbc preserve` keeps saved cell/PBC metadata and is the default. MACE also
supports `--pbc slab` (T,T,F, assuming the third cell direction is the vacuum
direction) or `--pbc periodic` (T,T,T). The input file is never changed.
CHGNet's crystal-graph adapter requires a full 3D-periodic cell. For slab
calculations this requires a suitable vacuum cell and physical validation of
vacuum convergence. Partial/nonperiodic inputs are rejected; only explicitly
request `--pbc periodic` when that change is appropriate. Plain XYZ without a
cell cannot be converted into a periodic slab merely by changing PBC flags.

These general materials potentials are not automatically validated for every
adsorbate, alloy, surface, or molecular reference state. Validate the specific
checkpoint on representative structures before using it for screening.

## Errors and tests

Per-structure errors are written to `scores.csv` and scoring continues. Exit
code 1 means at least one structure failed; exit code 2 means configuration,
input selection or initialization failed; 0 means success or a valid dry-run.
The CSV is flushed after each structure, so partial results remain if a long
run is interrupted. Completed runs include a report with success/failure
counts, resolved options, source paths and package versions.

Run lightweight tests from the repository root with NumPy, ASE and PyYAML:

```bash
python -m unittest discover -s tests -p 'test_mlp_scoring.py' -v
```

Tests use a deterministic fake ASE calculator and mocked backend constructors.
They cover energy units, raw forces, PBC validation, input preservation,
metadata, partial failures, output protection and dry-runs without model
loading. They do not establish model accuracy or replace a real inference
smoke test after installing a backend.

## Official references

- [MACE foundation models and ASE usage](https://mace-docs.readthedocs.io/en/latest/guide/foundation_models.html)
- [CHGNet installation and prediction conventions](https://chgnet.lbl.gov/)
- [CHGNet ASE calculator implementation](https://github.com/CederGroupHub/chgnet/blob/main/chgnet/model/dynamics.py)
- [CHGNet dependency requirements](https://github.com/CederGroupHub/chgnet/blob/main/pyproject.toml)
- [FAIRChem adsorption-energy references](https://fair-chem.github.io/adsorption-energies/)
