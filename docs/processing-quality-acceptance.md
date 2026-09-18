# Incremental processing acceptance (G5a tooling; not production activation)

Acceptance separates semantic quality, deterministic maintenance and coverage.
The online candidate path uses one incremental planner and at most two durable
request reservations per source/authorization work. It does not retain the old
Gate -> summary -> semantic-review tail as an acceptance requirement. Legacy
routes remain available for compatibility and deliberately stay the default
until separately authorized migration. This document replaces the obsolete
statement that deterministic tests are local-only: `tests_public/` is committed,
in the sdist, and exercised by CI. A green CI is not live-model acceptance.

## What runs, and what does not

`python -m memleaf.acceptance` is an offline acceptance utility, not a new online
planner, writer, plugin framework, daemon or MCP tool. It uses the real capture,
incremental runner/compiler, two-request Core budget and shared commit/recovery.
It creates fresh isolated Vaults for each case/repetition. No production Vault
argument, automatic installation, process termination, restore or pipeline
activation is provided. The fixed main prompt is unchanged.

The bundled `examples/incremental_acceptance.json` contains twelve **synthetic
public regression** trajectories, eighteen turns in total. They cover same-ID
completion/repetition, transient instructions, unsupported assistant preferences,
responsibility transfer, deadline preservation/cancellation/late observation,
independent projects, unknown attribution, observation dates, relative deadlines,
fact retraction, an independent new cycle and an independent late task. They are
not private incident reconstructions or a hidden holdout set. A private holdout
can use the same schema; labelling a case `holdout` does not prove it was never
used to tune prompts. Preserve that provenance outside the candidate's input.

Each JSON case has:

- `id`, `split` (`regression|holdout`) and a nonempty `semantic_checks` checklist;
- optional `initial_memories` with fixed IDs and ordinary Core memory fields;
- `turns` containing an `id`, original user/assistant `messages`, optional exact
  write `scope`, and optional deterministic `expected` assertions.

Messages retain their roles, optional source time/sequence/ID, and explicit
assistant `final:true`. This fixture format requires one complete visible pair;
it does not fabricate an assistant or turn arbitrary tool logs into user input.
It deliberately does not implement a general source revision/fault-injection DSL.
Those deterministic paths, native sharing, selected/text remember, Forget,
background processes and migration remain covered by their existing public tests
and final native-host acceptance, not implicitly by these twelve fixtures.

Assertions support count, required/forbidden structural field subsets,
`same_ids_as` (zero-based earlier turn), maximum calls, execution status and
coverage status. They do not perform title/body keyword grading or demand exact
natural-language answers. Titles and bodies are retained for independent review.
Two tasks can share a reasonable title; only the actual identity/state constraints
matter. Checklists and expected assertions are **never included in a model input**.

## Plan first: no models, credentials or Vaults

```sh
python -m memleaf.acceptance \
  --suite examples/incremental_acceptance.json --repeat 5
```

This reads/validates the suite and reports its SHA-256, prompt SHA-256/byte count,
case count, repeat count and request upper bounds. It does not initialize a Vault,
read a backend configuration or create output files. Twelve cases repeated five
times mean sixty independent case runs and ninety normal turn requests; the
absolute bound including a possible second dispatch is 180. The bound is not a
price quote or prediction that every turn will need recovery. A smaller explicit
cap is allowed; results then disclose unexecuted cases/steps instead of reporting
full completion. One final case stopped halfway is incomplete too.

## Explicit live execution, same configured route

```sh
python -m memleaf.acceptance \
  --suite /private/approved-suite.json --repeat 5 \
  --execute --authorize-model \
  --backend-config /private/model-config.yaml \
  --max-requests 180 --output /private/new-acceptance-run
```

All execution options are required. The backend configuration uses the existing
Memleaf format; only its `llm` settings are given to the existing ModelRouter.
The configured Vault, native note paths and host installations are never opened
by this utility. Choose the actually deployed DeepSeek Flash route and its
existing endpoint; the utility does not invent a model alias, change models to
make a test pass, or fall back to a host callback. CLI execution requires
`llm.thinking.single_pass: disabled` and a fixed single-dispatch API backend.
Requiring that setting does not prove the service honors it: applied controls
and observed reasoning statistics are reported separately when available.

The target directory must be new, with an existing parent. This is a fresh test
authorization, not a retry of a former acceptance run. Existing output is rejected
rather than clearing its request counter. The runner has no automatic resume or
transport-retry loop: a saved, interrupted trial remains a trial needing review;
starting a new output directory is a new expressly authorized test expense.
The Core's bounded invalid-response retry still counts toward both limits.
Partial semantic replan is not silently added to the regression run. Authentication,
configuration and transport failures stop the suite with a bounded reason; later
cases do not repeatedly consume the same broken route. Invalid model content is
retained as a failed trial rather than hidden by selecting a successful repeat.

Before each backend dispatch the runner persists one reservation in
`requests.json`; it cannot exceed `max_requests` even when the Core requests its
second attempt. A process dying after reservation may waste a slot. A network
failure may have reached the provider. Reservation counts are not exact charges.
Repeated trials use independent Vaults and empty control state, not a cached
successful response or an earlier repetition's memories.

Limits: 2 MiB suite, 64 cases, 128 total turns, 32 turns per case, 20 repetitions,
2048 dispatch reservations, and the existing Core input/output size bounds.
Exceeding a limit fails visibly or stops at the requested cap. Necessary input is
not truncated to manufacture passing results. Cases sharing a source sequence or
message identity without a distinct supported revision are invalid fixtures,
not evidence of a runtime regression.

## Output, privacy and measurements

The root and case directories are created private (0700, where supported), with
JSON files written 0600. Windows ACL behavior needs native verification. These
modes are not encryption. Output contains source text and generated memories;
do not attach a real run to a public issue or commit it. Tests in this batch use
only synthetic fixtures and local backends.

- `suite.json` freezes the input and independent review checklist.
- `report.json` contains safe per-step structural checks, runtime implementation
  fingerprint, requested model identity, case split, prompt/suite hashes and
  timings. It always returns `switch_authorized:false`.
- `requests.json` records reservation/outcome, input/output byte lengths and
  hashes, available token/cache counts and known thinking-control observations.
  The configured OpenAI-compatible adapter exposes the returned model label when
  the response supplies a valid bounded label. Missing values stay unknown.
- Private `traces/` retain the exact prepared prompt and visible completion text,
  not HTTP headers, credentials or hidden reasoning fields. Oversized completions
  are represented by length/hash and an explicit omission, not a truncated valid
  response. Failed-response payloads unavailable through the normal adapter stay
  unavailable; the harness does not bypass it to capture reasoning or raw errors.
- Each case's `observations.json` retains current heads, original execution/commit
  results and the independent semantic checklist. This can be compared with its
  sources and raw visible responses without another evaluation-model call.

Ordinary runtime metrics are not broadened into plaintext logging. Trace retention
exists only within this expressly requested private acceptance output. Missing
or broken metric collection does not turn a valid completion into a failed model
request. A trace/journal write failure can leave partial data and a reserved or
returned attempt; it must not be interpreted as zero cost or zero writes.

`backend_reservations` is confirmed entry into the metered backend boundary;
`confirmed_responses` counts returned calls. Actual provider execution is unknown
for an interrupted/error call. Requested and returned model labels are distinct.
Endpoint identity is a SHA-256 fingerprint, never a URL containing credentials.
`duration_seconds` is wall time for the observed backend/step, not provider-only
inference time. Detailed stage timing/performance remains a separate measurement.

## Separate acceptance gates

The [installed artifact verification](installed-artifact-verification.md) gate
runs the complete inventory from wheel and sdist in isolated native environments.
Its OS matrix and explicit import/skip checks strengthen gate 1, not gates 2 or 4.
A workflow definition alone is not a passed native run; retain the per-commit
reports and keep actual Hermes installation and live semantics separate.

1. **Deterministic contracts and artifact consistency.** Run `tests_public/` on
   source, sdist and an actually installed wheel, verifying import paths. Fixed
   oracle responses test the harness/runtime, not the model. Source and artifact
   implementation fingerprints must match before comparing results.
2. **Live semantic judgement.** A structurally passing live run still records
   `semantic_status:not_reviewed`. Review each repetition against its own source
   and checklist: future value, non-invention, correct subject/scope, independent
   topics, responsibility, deadline meaning, same-item maintenance and omissions.
   Good paraphrases are accepted. Evaluate correctness, not similarity to an
   example sentence. No second online or offline grading model is invoked.
3. **Coverage convergence.** Report complete/partial, committed operations,
   NO_MEMORY and unresolved reasons separately. Complete coverage is not evidence
   that every meaningful subtopic was extracted. An expected blocked result can
   pass its structural check without counting as successful business extraction.
4. **Host, platform and migration.** Real Hermes capture/installed Provider,
   Windows/macOS process behavior, existing Codex compatibility, stopped writers,
   verified private backup, latest Forget evidence and authorized activation
   remain external gates. No stub replay grants a production switch.

Retain all attempted repetitions, including errors and budget stops; never choose
only one successful seed. Five repeats are a starting point for observing
variation, not a reliability percentage. The public twelve-case set does not
replace the private A-I incidents, E01-E20 applicable paths, reverse cases, or a
held-out set. Compare the actual production baseline and the candidate under the
same source and route; do not rerun all historic prompts by default.

The fixed-oracle programmatic test flag is for trusted local injected backends;
it is not a network sandbox or proof that an arbitrary callback is offline.
The CLI intentionally provides no way to call a live service while relabelling
it a fixture test. Neither a fixture pass nor a live structural pass is a model
quality approval or release authorization.
