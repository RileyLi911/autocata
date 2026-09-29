# Skill Evaluation Cases

Use these cases for forward tests. Give a fresh agent only the skill and the raw
fixture/request. Do not reveal the expected diagnosis beyond deterministic
assertions.

## Trigger Cases

1. Chinese Matbench top-N + NO3 path + SP and Relax: must trigger and parse all
   fields.
2. English Matbench Discovery top-N + CO path + Relax only: must trigger without
   silently adding SP.
3. Named MLPs + adsorption benchmark: must trigger `user_specified`, search each
   identity, show authoritative candidate links, and wait for confirmation.
4. Three named local checkpoints + adsorption benchmark: must trigger and
   confirm the model/checkpoint identities without inventing ranks.
5. Completed-results plotting request: must not implicitly start a new benchmark.
6. General installation of a named MLP without adsorption evaluation: must not
   implicitly trigger.

## Workflow Cases

1. Two plausible DFT columns: pause for the target definition before Step 2.
2. Relax requested but DFT target is single-point at another geometry: identify
   the semantic mismatch.
3. A v2/v3 run: reject it explicitly as incompatible and require a fresh v4 run;
   do not migrate or modify it.
4. Gated checkpoint: request external authentication state, never the raw token.
5. AlphaNet isolated molecule returns NaN under nonperiodic stress: block the
   model, record a provider-specific scientific exception, and re-smoke-test.
6. Existing nonterminal Slurm job is absent from `squeue` but present in `sacct`:
   reconcile the terminal state rather than resubmitting.
7. Retry CSV contains `ok`, `success`, and `unconverged`: normalize without
   counting unconverged as valid.
8. One Relax frame dissociates: keep it in all-valid metrics and the dissociation
   table, while excluding it only from the clearly labeled intact-only table.
9. A model requires FIRE instead of the baseline optimizer: preserve the baseline
   and record a scoped exception.
10. A schema-valid worklist has N models but duplicated ranks: deterministic run
    validation must fail.
11. A row says `success` but has NaN/Inf, missing component energies, or an
    adsorption-energy identity mismatch: preserve its source status, mark its
    effective status failed, record the reason, and exclude it from metrics.
12. A Relax row says `success` with `converged=false`, missing/nonfinite fmax, or
    fmax above threshold: mark it failed. After the required 5000-step rerun,
    keep a still-unconverged row visible but exclude it from numerical metrics.
13. A frame is unconverged after the FIRE 1000-step run: require a 5000-step
    rerun and reject a final row that still records only the first attempt.
14. FIRE is unsupported: allow BFGS only with a nonempty fallback reason and
    scientific-exception ID; reject every other optimizer.
15. Relax metadata reports a variable cell or the wrong fixed-layer count:
    reject it. Treat exactly 5 slab layers as the one-fixed-layer branch.
16. Two model/task records resolve to one script path or have identical script
    hashes: reject the run even when both files exist.
17. A model-local script is a symlink or delegates to a shared executable
    runner: reject it.
18. Runtime provenance reports a different interpreter, package version, script
    path, or script hash than `model_config.json`: reject merge and completion.
19. Calculator isolation contains only `passed=true`: reject it until the full
    per-task and object-isolation evidence is recorded.
20. A new request omits `workspace_base`: ask for it and do not infer a prefix.
    Reject a manifest unless `run_base` is exactly
    `<workspace_base>/CatQC/benchmark_runs` and `benchmark_root` is its direct
    child.
21. User requests `MACE-MP-0` and `CHGNet`: search both independently, show a
    direct authoritative candidate link for each, and do not create Step 0 until
    the user confirms both identities.
22. A user-specified model name has no credible source: ask for an official URL,
    repository, paper/DOI, provider/package, checkpoint path, or exact variant;
    do not guess, drop it, or replace it.
23. Two credible variants share one requested family name: show both and require
    the user to select one; package installation success is not identity
    confirmation.
24. A `user_specified` models manifest has a non-null `snapshot_rank`, missing
    confirmation URL, or `confirmed=false`: deterministic run validation must
    fail.
25. Five confirmed models + NO3 + SP and Relax: create a
    `<timestamp>_5models_no3_sp-relax` run with all v4 control files.
26. One exact incomplete request resumes automatically; two exact incomplete
    runs return candidates and require a user choice; a completed run is ignored.
27. Two five-model requests with different identities never match.
28. Result written before checkpoint update is reconstructed; checkpoint-only
    completion returns to pending.
29. A valid foreign lease blocks submission. Expired-lease takeover keeps
    submission disabled until active Slurm/PID state is reconciled.
30. Corrupt JSON, truncated events, duplicate result frames, hash drift, active
    jobs without ID/PID, or completed tasks with pending frames fail validation.
31. A valid supplied DFT table is frozen and used by default; VASP is not run
    unless the table is absent or the user explicitly requests recomputation.
32. Generated DFT relaxes slab+adsorbate, derived clean slab, and isolated
    adsorbate with one confirmed profile and computes their energy difference.
33. VASPKIT and direct POTCAR backends preserve POSCAR element order, record
    TITEL/SHA provenance, never return POTCAR, and never silently fall back.
34. A credential-like execution-profile field is rejected; authentication is
    external to run artifacts.
35. DFT and MLP jobs may remain active concurrently, but merge blocks until DFT
    is complete or the user explicitly approves partial coverage.
36. A result written before the DFT checkpoint is reconstructed; a completed
    DFT checkpoint without valid output is rejected.
37. Official repository environment guides are saved verbatim, hashed, and
    linked to the environment preparation record before any mutation.
38. Existing CUDA/driver/Python/package-manager state is captured before
    environment creation, and the toolchain decision records user uv, agent uv,
    conda/mamba, or user-decision-pending in that order.
39. Unattested local guide content is rejected; guide redirects, HTTP status,
    content type, ETag, approved host, and final URL are recorded.
40. A model config cannot pass run validation without a valid environment
    preparation record and a hashed installation plan referencing saved guides.

## Acceptance Criteria

- No source input or legacy artifact is modified.
- No raw secret is requested or written.
- No job is duplicate-submitted.
- Every required decision is either validated or represented as a pending
  question; no scientific value is guessed.
- Deterministic scripts reject missing coverage, duplicated indices,
  `selection_index` gaps, Matbench rank gaps, unconfirmed user-specified
  identities, invalid statuses, metric drift, and absent dissociation evidence.

## Catalysis-aware selection

Run `scripts/catalysis_selection_self_test.py` for version-specific selectors,
checkpoint changes, original leaderboard identity, nonrecommended choices,
unknown/conflicting official evidence, incompatible energy semantics, source
hash drift, legacy rejection and component initialization mismatches.
