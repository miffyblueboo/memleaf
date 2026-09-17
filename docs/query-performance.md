# Query verification performance (G5b)

This change implements the measured local-query portion of FS198, after the
G5a acceptance preparation. It does not change extraction, request allowances,
protocols, default pipelines, package version or production configuration.

## Remove duplicate parsing, retain physical verification

The public query path deliberately scans current Markdown and rechecks it before
returning. Previously both passes parsed all frontmatter, constructed Memory
objects and canonicalized their business fields. Profiling a synthetic 1,000-file
Vault identified that duplicate CPU work; it did not justify dropping the second
read or trusting modification times.

The initial `QueryScan` now retains request-local, immutable parse facts for each
fully validated ordinary file: its area/relative-path locator, exact raw SHA-256,
business fingerprint, validated identity and scope tuple. It does not retain a
second body copy. The facts are separate from mutable records handed to callers.

The final scan still enumerates the selected areas, validates path/file type and
bounds, opens and reads every file, checks opened-file identity and before/after
metadata, and hashes the current bytes. Only **after those checks**, an exact-byte
match can reuse the parse facts. Changed bytes follow the normal parser. Malformed
records are never cached as valid. Valid duplicate-ID claimants retain their
facts even though their records are quarantined, so ambiguity stays visible.

This is not a stat-only shortcut, persistent index, cross-request cache, new
storage service or permission token. Each new query starts with a fresh parse.
Counting/format-only writes are reparsed but retain the existing business-view
fingerprint semantics; changed body/status/scope/validity still invalidate it.
Public metadata and response limits are unchanged.

The extra metadata is O(number of validated files) for the duration of one
request. Disk reads and file checks remain O(number of selected files), normally
two passes. There is no guarantee of faster cold I/O, network filesystems or very
large full-pagination workloads. The existing final-check-to-return race against
uncooperative external editors is neither enlarged into a promised atomic
snapshot nor claimed to be eliminated.

## Reproduce an isolated local measurement

```sh
# Source checkout; for an installed wheel omit PYTHONPATH.
PYTHONPATH=src python examples/query_benchmark.py --sizes 100 1000 10000 --repeat 5
```

The example accepts sizes/repetitions only, not a production Vault, model config
or credentials. Each size uses a fresh TemporaryDirectory, 1..10,000 synthetic
independent Todo heads, and a fixed source clock. Fixture creation is outside the
timed section and does not rebuild an index for each inserted file. One warmup is
excluded. JSON output contains raw samples, empirical median/P95, runtime/scanner
hashes, fixture hash/bytes, result counts, generation and diagnostic parser counts.
Parser instrumentation is performed separately from the timing samples.

The benchmark verifies complete scans and nonempty expected public results.
It reports first pages, **not** a full enumeration of every page. A fast empty
result cannot count as successful measurement. The CLI fails rather than reporting
partial timings as a successful suite. There are zero model or network calls.

## Observed local comparison, 2026-09-17

Linux x86_64 / Python 3.13.5, identical G5a service code and synthetic fixture,
old versus new query scanner. Five measured repetitions after one warmup; variants
ran sequentially. OS cache and scheduling were not controlled. These are empirical
observations, not an SLA or a confidence interval. For five samples the nearest-rank
P95 is simply the largest observed sample. The raw report includes every sample.

| Memory heads | Operation | Baseline median (s) | Candidate median (s) | Observed reduction |
|---:|---|---:|---:|---:|
| 100 | `recheck` | 0.0203 | 0.0062 | 69.3% |
| 100 | `todo_first_page` | 0.0461 | 0.0304 | 33.9% |
| 100 | `search_first_page` | 0.0458 | 0.0315 | 31.2% |
| 100 | `read_first_page` | 0.0439 | 0.0285 | 35.1% |
| 1,000 | `recheck` | 0.2376 | 0.0620 | 73.9% |
| 1,000 | `todo_first_page` | 0.4997 | 0.2927 | 41.4% |
| 1,000 | `search_first_page` | 0.4873 | 0.3139 | 35.6% |
| 1,000 | `read_first_page` | 0.4665 | 0.2920 | 37.4% |
| 10,000 | `recheck` | 1.8580 | 0.5064 | 72.7% |
| 10,000 | `todo_first_page` | 3.9923 | 2.6869 | 32.7% |
| 10,000 | `search_first_page` | 4.2484 | 2.7542 | 35.2% |
| 10,000 | `read_first_page` | 3.7341 | 2.6473 | 29.1% |

All compared fixture hashes, returned counts, paging flags, generations and
complete-scan observations matched. Initial parsing remained N in both versions;
unchanged recheck parsing fell from N to zero. No business records were omitted
to improve latency. At 10,000 heads the fixture contains 7,550,000 bytes; it is not
a sample of private user conversations or an actual production Vault.

The harness is included in sdist through the existing examples manifest. Running
it against an installed wheel must use that wheel's imports, not an editable
checkout. The measurements do not cover provider latency, tokens, recall/semantic
quality, native Hermes installation or Windows/macOS performance. Those remain
separate G5 acceptance gates; successful timings never authorize switching.

## Regression boundary

Focused tests cover physical rereads, no cross-query reuse, mutation of caller
records, same-size/same-mtime body edits, format/hit changes, identity conflicts,
malformed claimants, invalid UTF-8, source additions/removals/renames, history,
file/total/path bounds, read errors and link substitution. Public metadata must
not expose the private receipts. Existing source, lifecycle, migration, retention
and acceptance tests are retained; no test thresholds require a particular speed.
