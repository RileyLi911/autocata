# Step 3 Model Scripts, Calculator Isolation, And Adsorbate Integrity

Read and follow this reference while creating and validating every model's SP
or Relax implementation.

## Require A Model-Owned Test Script

Create real files inside each model workspace:

```text
models/<model_key>/scripts/predict_single_point.py
models/<model_key>/scripts/predict_relax.py
```

Each requested task must have its own model-specific executable script. Do not
point multiple models to one shared runner, symlink, wrapper, or executable
entrypoint. Each script must own its model import, checkpoint/model creation,
calculator factory, frame loop, error handling, resume logic, and output
writing.

The script must be derived from the frozen official model-usage plan. Record
the guide paths and plan hash in `model_config.json` and runtime provenance.
The official minimal usage example is a prerequisite to the benchmark-specific
calculator-isolation probe; a failed or hash-drifting guide blocks execution.

Pure stateless helpers for CSV parsing, atomic selection, or validation may be
shared, but a shared helper must not load the model, create the calculator, run
inference/optimization, or own the frame loop. Record each script's path and
SHA-256 in `model_config.json`.

Set `model_specific=true`, `shared_executable_runner=false`,
`delegates_to_shared_runner=false`, and affirm ownership of the model import,
checkpoint loading, calculator factory, and frame loop. The validator rejects
paths outside the owning model's `scripts/` directory, symlinks, reused paths,
and duplicate script hashes across any model/task. Cosmetic copies and thin
wrappers do not satisfy model ownership.

Write `validation/runtime_provenance.json` from inside the actual model process.
Record configured and observed `sys.executable`, observed package versions, and
the executed path/hash for each requested task. These values must match
`model_config.json`; launcher intent alone is not execution evidence.

## Use A Fresh Calculator Per Structure

The frame directory is part of the scientific provenance, not an optional
logging convention. Use
`models/<model_key>/frames/<task>/<structure_index:08d>/` with separate
`input/`, `work/`, `outputs/`, and `errors/` subdirectories. Write one
`frame_manifest.json` and one `cleanup.json` for every terminal frame. The
aggregate CSV is a derived output and aggregate Relax XYZ is not required.

Treat calculator state as frame-local. For every input frame:

1. Copy the source `Atoms`; never mutate the shared input object.
2. Build a new calculator object through the model's calculator factory.
3. Attach it to only that copied structure.
4. Use a frame-specific working directory, log, temporary files, constraints,
   and optimizer.
5. Detach the calculator and release frame-local objects after success or
   failure.

For relaxation adsorption energy, create separate fresh calculator objects
for the slab-plus-adsorbate structure and the clean slab. Do not attach one
calculator object to two `Atoms` objects. Create a new calculator again for a
retry. Reusing the same checkpoint path and immutable loaded weights is allowed
only when the provider supports it and isolation validation passes. Reusing a
mutable calculator, neighbor list, results dictionary, or mutable model cache
across structures is not. If the official API shares mutable state between
calculator objects, reload, clone, or otherwise isolate that state.

Do not rely on `calc.results.clear()` as a substitute for a new calculator.
Do not carry an optimizer, trajectory, neighbor list, calculator cache, result
dictionary, or model work directory from one frame to another.

Use cleanup appropriate to the framework, such as detaching `atoms.calc`,
deleting frame-local objects, garbage collection, and clearing unused GPU cache.
Cleanup must run in `finally` paths so a failed frame cannot contaminate the
next frame.

The manifest must record the input hash, calculator freshness, calculator object
identities, release status, cleanup status, output hashes, attempt count, and
the terminal status. A successful Relax frame must also record and hash its own
`outputs/relaxed_structure.extxyz`; never use a combined XYZ as the only record.

## Validate Calculator Isolation

Before the full run, test at least two frames in both orders:

```text
A then B
B then A
```

Use fresh calculator objects for every evaluation and confirm each frame's
energy agrees across orders within the recorded numerical tolerance. For Relax,
use a short deterministic smoke relaxation or equivalent model-appropriate
probe. Record script paths/hashes, calculator-factory call counts, object
identity checks, immutable-weight reuse mode, order-invariance results,
tolerance, peak memory, timing, and pass/fail in
`validation/script_calculator_isolation.json`.

The isolation artifact must use the bundled schema and contain more than a bare
`passed=true`: include model identity, per-task script path/hash, fresh-object
and mutable-state flags, distinct object-identity evidence, calculator-factory
call count, separate Relax component evidence, and the A/B order-invariance
result.

Do not start a full job when the model reuses one calculator across frames or
fails the order-invariance probe.

## Check Adsorbate Dissociation After Relaxation

Define the dataset-specific adsorbate integrity rule in
`benchmark_config.json` before running Relax. Derive adsorbate atom indices from
the recorded selection rule and use the recorded periodic-boundary handling.
Evaluate monitored intramolecular bonds with minimum-image distances when PBC
applies.

For the current NO3 workflow, the validated rule is:

- Select the adsorbate atoms as N, O, O, O according to the configured indices.
- Compute all three N-O minimum-image distances.
- Mark `dissociated` when any N-O distance is greater than `1.8 Å`.

Do not apply this NO3 threshold to another adsorbate automatically. Define
explicit monitored bonds and thresholds, a connectivity-graph rule, or another
user-approved criterion for each dataset.

Record these fields for every Relax frame:

```text
dissociation_status,dissociation_reason,broken_bond_count,max_monitored_bond_distance_A,monitored_bond_distances_A_json,integrity_check_method
```

Use `intact`, `dissociated`, or `unknown`. Determine dissociation independently
from optimizer convergence. Use `unknown` only when the relaxed structure or
required atoms/distances cannot be validated.

Save all relaxed structures and additionally save dissociated structures with
their `model_key`, `structure_index`, integrity rule, and measured distances.
Write `validation/relaxation_integrity.csv` as the per-frame audit.

## Metric Policy

Do not silently discard dissociated frames. Keep them marked in the main long
table and all-valid metrics. Also create a clearly named intact-only Relax
metrics table so readers can compare results with dissociated frames excluded.
Report the exclusion count and exact structure indices per model.

## Catalytic configuration integrity

The actual checkpoint, task parameters and head must match the confirmed
execution_variant. Follow `references/catalysis-aware-selection.md` for live
calculator inspection and per-component observations. The script registration
record includes the selected variant and assessment hash; launch intent alone
is not runtime evidence.
