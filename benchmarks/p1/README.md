# P1 baseline evaluation assets

These files implement the **measurement preparation** part of P1. They do not
change memleaf's product pipeline and they do not claim real-model results.

## Baseline

Correctness baseline B0 is fixed at:

`e2106bc9d55b8109ebd8df54eff7c1bad0c11180`

This is the P0 branch state after the Gate protocol candidate plus the isolated
F01/F03/F04/F06 follow-up fixes passed the repository's Linux, Windows, macOS,
wheel/sdist, and Codex-native CI matrix.

`cases-v1.json.gz` stores the exact compressed JSON fixture and preserves the ten synthetic semantic cases from the audit
material. The exploration default is 3 repetitions per logical case (30 process
runs per arm). Repetitions are repeated samples, not additional independent
semantic cases. A later acceptance set must be expanded separately.

## Safety and control rules

`run_baseline.py` is dry-run by default. It performs **zero model calls** unless
`--execute` is explicitly supplied. Real execution also requires an explicit
config template that pins `llm.provider` and exact `llm.model`, plus an output
path. Every run uses a fresh temporary Vault. Seed memories and event text are
synthetic. The runner does not copy a production Vault.

The temporary config keeps the selected provider/model/timeout/concurrency from
the supplied template, disables diagnostic logging, and fixes Gate/Summary/
Compact thinking to `low` for the experiment. It never serializes API keys,
prompts, raw model responses, or the source config into benchmark output.

The runner records product output needed for manual semantic grading (active and
history Markdown projections), structural model metrics, process wall time, and
a fresh-instance visibility check. It deliberately leaves `price_usd` unknown;
pricing must only be computed from verified provider pricing at run time.

## Commands

The companion `cases-v1-manifest.json` lists the ten case IDs/categories and the SHA-256 of the decompressed JSON so the compressed fixture is independently checkable.

Inspect the plan without calling a model:

```bash
python benchmarks/p1/run_baseline.py
```

Run the small asset tests (still no model call):

```bash
python -m unittest benchmarks.p1.test_runner -v
```

A real B0 exploration is intentionally explicit:

```bash
python benchmarks/p1/run_baseline.py \
  --execute \
  --arm-label B0 \
  --config-template /path/to/isolated-eval-config.yaml \
  --output benchmarks/results/p1-b0.json \
  --max-process-runs 30
```

Do not put a credential-bearing config file into the repository. Do not compare
a future architecture arm against the old crashing release; compare it against
this corrected B0 under the same provider, exact model, thinking, timeout,
concurrency, event data, and seed semantics.
