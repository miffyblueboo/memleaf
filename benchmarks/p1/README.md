# P1 baseline evaluation assets

These files implement the **measurement preparation** part of P1. They do not
change memleaf's product pipeline and they do not claim real-model results.

## Baseline

Correctness baseline B0 is fixed at:

`e2106bc9d55b8109ebd8df54eff7c1bad0c11180`

This is the P0 branch state after the Gate protocol candidate plus the isolated
F01/F03/F04/F06 follow-up fixes passed the repository's Linux, Windows, macOS,
wheel/sdist, and Codex-native CI matrix.

`cases-v1.json.gz` stores the exact compressed JSON fixture and preserves the ten
synthetic semantic cases from the audit material. The exploration default is 3
repetitions per logical case (30 process runs per arm). Repetitions are repeated
samples, not additional independent semantic cases. A later acceptance set must
be expanded separately.

## Safety and control rules

`run_baseline.py` is dry-run by default. It performs **zero model calls** unless
`--execute` is explicitly supplied. Real execution also requires an explicit
config template that pins `llm.provider` and exact `llm.model`, an output path,
and a positive `--max-model-calls` hard cap. Every run uses a fresh temporary
Vault. Seed memories and event text are synthetic. The runner does not copy a
production Vault.

The temporary config keeps the selected provider/model/timeout/concurrency from
the supplied template, disables diagnostic logging, and fixes Gate/Summary/
Compact thinking to `low` for the experiment. It never serializes API keys,
provider URLs, prompts, raw model responses, or the source config into benchmark
output.

The model-call budget is enforced around the pinned API route, including
concurrent model stages. A rejected call is never delegated after the cap has
been reached. Each case writes a structural result even if processing fails, and
the output JSON is replaced after every completed case so a later failure or
budget stop does not discard earlier evidence.

The runner records product output needed for manual semantic grading (active and
history Markdown projections), structural model metrics, process wall time, and
a fresh-instance visibility check. It deliberately leaves `price_usd` unknown;
pricing and a monetary cap must only be computed from verified provider pricing
at run time.

`cost_report.py` prices only provider-reported token usage. It never infers token
counts from prompt/output characters. An exact bill requires a complete cache
hit/miss input split plus completion tokens for every retained call. If that is
unavailable, it can still report a conservative **observed** cost when prompt and
completion token totals exist, charging every input token at the more expensive
input tier. That observed value is not represented as a hard bound on future
calls.

## Current external blocker

A zero-call GitHub Actions preflight on 2026-09-10 at commit
`ed63b24c7a90148a735e8ec1ea9b3d304ae2841a` found the repository live-model
route unconfigured: `MEMLEAF_LIVE_MODEL_TOKEN`, `MEMLEAF_LIVE_BASE_URL`, and
`MEMLEAF_LIVE_MODEL` were all absent. The preflight made 0 model calls. This is a
historical observation, not a claim that repository settings can never change.

Do not add a credential merely to make CI green. The preferred next step is a
small local probe using the already configured local memleaf Model Route, if one
exists, without uploading its key to GitHub.

## Commands

The companion `cases-v1-manifest.json` lists the ten case IDs/categories and the
SHA-256 of the decompressed JSON so the compressed fixture is independently
checkable.

Inspect the default plan without calling a model:

```bash
python benchmarks/p1/run_baseline.py
```

Inspect the local route identity without printing its URL or credential and
without calling a model:

```bash
python benchmarks/p1/run_baseline.py \
  --config-template ~/.memleaf/config.yaml
```

Run the small asset tests (still no model call):

```bash
python -m unittest benchmarks.p1.test_runner benchmarks.p1.test_cost_report -v
```

### Stage 1: three-call probe

Before opening a larger pilot budget, run exactly one simple case with a hard
three-call cap. The normal single-fact path is expected to exercise Gate,
Summary, and Semantic Review. If a retry is needed, the cap may stop the process
part-way; the retained call graph is still useful for measuring real token and
latency behavior.

```bash
python benchmarks/p1/run_baseline.py \
  --execute \
  --arm-label B0-probe \
  --case AB01_fact \
  --repetitions 1 \
  --config-template ~/.memleaf/config.yaml \
  --output benchmarks/results/p1-b0-probe.json \
  --max-process-runs 1 \
  --max-model-calls 3
```

Price the observed calls only after verifying the current provider tariff:

```bash
python benchmarks/p1/cost_report.py \
  --input benchmarks/results/p1-b0-probe.json \
  --input-cache-hit-per-million <verified-rate> \
  --input-cache-miss-per-million <verified-rate> \
  --output-per-million <verified-rate> \
  --currency <currency> \
  --project-calls 12
```

### Stage 2: bounded pilot

Only after the three-call probe has established actual usage and the provider
price has been verified should the same `AB01_fact` case be allowed a larger
call cap. Register that cap and the corresponding monetary rationale in the run
notes before execution; do not silently increase it after a budget stop.

```bash
python benchmarks/p1/run_baseline.py \
  --execute \
  --arm-label B0-pilot \
  --case AB01_fact \
  --repetitions 1 \
  --config-template ~/.memleaf/config.yaml \
  --output benchmarks/results/p1-b0-pilot.json \
  --max-process-runs 1 \
  --max-model-calls <registered-pilot-call-cap>
```

### Stage 3: 30-run B0 exploration

After the bounded pilot establishes a defensible call/token and pricing range,
the full B0 exploration remains explicit:

```bash
python benchmarks/p1/run_baseline.py \
  --execute \
  --arm-label B0 \
  --config-template ~/.memleaf/config.yaml \
  --output benchmarks/results/p1-b0.json \
  --max-process-runs 30 \
  --max-model-calls <registered-call-cap>
```

Do not put a credential-bearing config file into the repository. Do not compare
a future architecture arm against the old crashing release; compare it against
this corrected B0 under the same provider, exact model, thinking, timeout,
concurrency, event data, and seed semantics.
