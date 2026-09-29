# Select a version-specific catalytic configuration

Apply to every model in both named-model and Matbench selection BEFORE Step 0.
This is a mandatory source investigation, not a keyword heuristic. Read the
official GitHub README, version docs, examples, configuration registry and relevant
calculator implementation to determine supported task selectors, heads, weights
and fine-tuned releases. Inspect the actual dataset read-only first when its
chemistry or reference definition is needed to choose. Do not create an ambiguous
run just to defer this decision until environment setup.

## Source authority and comparison

Verify repository ownership from an official project/provider link. Pin the
review to a full commit SHA and identify the exact model release/checkpoint it
describes. An official model card or primary paper may supplement GitHub; neither
a search snippet, unofficial fork nor a generic installation guide suffices as
the primary usage evidence. Save the reviewed text, URL, commit, retrieval time
and SHA-256. If an official repository cannot be established, keep the assessment
`insufficient_evidence` and request identifying evidence; do not infer absence of
a catalytic configuration.

Compare candidates against the actual surface/adsorbate chemistry, elements,
requested SP/Relax capabilities, boundary/reference states and DFT target. Check
the meaning of returned energy: total energy versus pre-referenced adsorption
energy or another target. Also check units, DFT level and whether slab+adsorbate,
clean slab and isolated adsorbate components can be consistently combined. A
catalysis label does not prove compatibility or accuracy on this benchmark.

For example, investigate an available UMA `task_name` at the confirmed version;
do not assume `oc20` exists in every checkpoint or is always the best domain
match. Task-conditioned prediction need not mean a separate physical output head.
Represent provider terminology accurately. Never silently switch the isolated
molecule to a molecular task while the surface uses a catalytic task: reference
energies from different tasks/theory levels are not automatically comparable.

Present the original request, investigated alternatives, recommendation, energy
semantics, limitations and direct official GitHub links together with the existing
model-identity confirmation. One user confirmation covers the model and execution
configuration. An explicit nonrecommended choice is allowed if it remains
scientifically compatible; preserve the reason and limitations. Do not increase
the requested model count or silently replace an explicitly requested checkpoint.

For Matbench, preserve the exact original leaderboard name, release, rank and
configuration in `leaderboard_identity`; record the actually tested configuration
separately. Explain any switch and confirm it, including checkpoint replacement.
The original rank is a selection proxy, never a claim about the substituted
configuration's benchmark performance. If the original configuration is unknown,
resolve it from the original official submission evidence instead of guessing.

## Selection interface

Use `assets/catalysis_selection.schema.json` alongside the existing selection
payload. Every model needs these three fields:

```json
{
  "execution_variant": {
    "checkpoint_id": "exact-checkpoint-and-release",
    "parameters": {"task_name": "confirmed-provider-value"},
    "head": null
  },
  "leaderboard_identity": null,
  "catalysis_assessment": {}
}
```

`parameters` is a generic JSON object, not limited to `task_name`. Use an empty
object only when there is no task selector, and null head only when there is no
separately selectable head. `checkpoint_id` must identify the actual selected
checkpoint/version, not just a model family. For Matbench, `leaderboard_identity`
is an object with original `model_name`, `version_or_variant`, `source_url`,
`snapshot_rank` and original `execution_variant`.

Populate `catalysis_assessment` with:

- `official_repository`, `repository_verification` (authority URL, reason,
  verified_by), full `commit_sha`, and `checked_version` matching the actual
  `version_or_variant` in the selection.
- `search_scope`: examined README/docs/examples/configuration/code locations.
- `scope`: system description, exact adsorbate, elements, requested_tasks, and
  DFT reference description. Step 0 checks adsorbate and requested task coverage;
  the agent must assess the scientific meaning and supported elements.
- `sources`: unique id, kind, URL, retrieved_at, saved `content` and its UTF-8
  SHA-256. Primary sources use kind `official_github`, the same commit_sha, and
  a `https://github.com/<owner>/<repo>/blob/<full-commit>/<file>` permalink.
  Supplemental kinds are official_model_card, primary_paper and official_docs.
- `candidates`: unique id, execution_variant, source_ids, applicability,
  limitations, compatibility (compatible/incompatible/unresolved), and
  energy_semantics (kind, unit, components_compatible, dft_level,
  reference_state, rationale).
- `finding`: specialized_found, no_specialized_found_in_checked_sources,
  insufficient_evidence or unresolved. The last two remain pending and cannot
  pass freeze. The second is a bounded search finding, not universal absence.
- `recommended_candidate_id`, `unresolved_conflicts`, and `decision` containing
  selected_candidate_id, user_requested_variant (null if unspecified), reason,
  displayed_differences, confirmed=true, confirmed_by=user and ISO confirmed_at.

Every candidate must cite saved sources. Retaining the general configuration
still requires a candidate assessment. The chosen candidate must match
execution_variant and be compatible with the current three-total-energy formula:
kind=total_energy, unit=eV, components_compatible=true. Non-total-energy outputs
need a separately agreed scientific workflow; this release blocks them rather
than subtracting already-referenced energies or silently changing the formula.
Unsupported cases and unresolved official code/documentation conflicts block
freeze even if a recommendation has been proposed.

Validate before initialization:

```text
python scripts/catalysis_selection.py <selection-file.json>
```

Step 0 also enforces validation, preserves these fields in the model identity
hash, and archives source text under `selection_evidence/catalysis/<sha256>.txt`.
The source text is already embedded in the frozen selection, so source paths or
external downloads are not required to resume. Never put credentials in it.
Updating the reviewed version, source content or actual configuration changes
the frozen identity and requires renewed confirmation and a new run.

## Bind selection to script generation and real execution

Before freezing a usage plan, copy the confirmed `execution_variant` and set
`catalysis_assessment_sha256` to the canonical JSON SHA-256 of the frozen
assessment. `checkpoint_loading.variant` must equal the selected checkpoint_id.
Use official usage evidence describing this exact release and configuration.
The plan validator rejects missing/changed selection evidence, so old plans
cannot be reused for a different task selector.

The model-owned script must pass the selected parameters/head to the actual
provider factory and load the selected checkpoint. After calculator construction,
inspect provider attributes or its effective initialized configuration. Record
observations from that live object, not copied launcher intent. For each record:

```json
{
  "execution_variant": {"checkpoint_id": "exact-checkpoint-and-release", "parameters": {"task_name": "confirmed-provider-value"}, "head": null},
  "calculator_initializations": [
    {
      "component": "slab_plus_adsorbate",
      "execution_variant": {"checkpoint_id": "exact-checkpoint-and-release", "parameters": {"task_name": "confirmed-provider-value"}, "head": null},
      "calculator_class": "provider.Calculator",
      "object_id": "observed-object-identity",
      "inspection_method": "Describe actual provider attributes inspected after construction",
      "inspection": {"checkpoint_id": "exact-checkpoint-and-release", "parameters": {"task_name": "confirmed-provider-value"}, "head": null},
      "checkpoint_path": "models/<model_key>/checkpoints/<file>",
      "checkpoint_sha256": "actual-file-sha256"
    }
  ]
}
```

Add these fields to the official smoke-test record (only constructed components
required), each task_runs entry in runtime_provenance.json (all three components
required), and each completed frame_manifest.json (all three components required).
For a reusable isolated-adsorbate reference, retain its original verified
construction/checkpoint observation; do not pretend a new calculator was built.
Record multiple actual constructions if needed. All components must use the
same confirmed configuration in this workflow. The selected variant also belongs
in model_config.json; script_preparation.json stores the binding automatically.

Use `observation_errors` to validate observations immediately after construction
and abort before inference on mismatch. Frame commit, runtime validation and
completion audit recheck them. The verifier compares parameters, checkpoint,
head, provider inspection and model-local checkpoint bytes; it cannot independently
establish whether an agent honestly inspected the live object. Existing runtime
provenance and calculator-isolation evidence remain required.

## Reporting, compatibility and tests

The English report displays actual checkpoint, task parameters/head, selection
reason and original leaderboard identity. Each model assessment must explain its
configuration choice and separate official intended applicability from measured
performance; official catalytic specialization is not proof of superiority.

Older releases remain intact. The new release rejects selection/usage/restore
records missing these fields, even when they use the v4 container format. Resume
old work with its original skill, or confirm a new configuration and start a new
run. Do not backfill an old completed run to make it appear newly verified.

Run `scripts/catalysis_selection_self_test.py`, then existing run control, model
usage, frame, report and package regression checks. Fixtures are synthetic and
never count as official evidence for real model choices.
