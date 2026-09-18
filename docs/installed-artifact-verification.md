# Installed artifact and native-platform verification (G5c)

This is a deterministic CI gate, not a new runtime subsystem or live-model
acceptance. It follows FS183, FS185-194 and the installation/process portions
of E10/E17/E19. Existing extraction defaults, prompts, budgets, public interfaces
and package version are unchanged. Running these checks does not install a real
Hermes host, read a production Vault, create a release or authorize migration.

## Build once, test the same distribution bytes

The build job runs the complete public source suite and records the discovered
case inventory. It builds one wheel and one sdist, verifies that **all** packaged
Memleaf files match (including the Provider resources), and creates a manifest
binding artifact sizes/hashes, test assets, inventory and the current commit.
Verification inputs (manifest and test-only dependencies) use a separate
artifact; `memleaf-distributions` continues to contain only the release wheel
and sdist, so downstream PyPI publishing cannot ingest test data. The build job
exposes the manifest hash separately as a job output. A verification
job must match that hash and commit before installing the downloaded artifacts.
This catches accidental cross-run/mixed artifacts; it is not a signed supply-chain
attestation against replacement of the workflow and its own evidence together.

`verify-artifacts` covers five cells:

| Native runner | Python |
|---|---|
| Ubuntu | 3.11, 3.12, 3.13 |
| Windows | 3.12 |
| macOS | 3.12 |

This is not a full OS/Python/architecture Cartesian matrix. Every cell downloads
the same build artifact. The sdist is rebuilt locally on that runner, and the
resulting Memleaf payload must equal the original wheel before it is tested.
Archive timestamps and packaging metadata may differ; implementation/resource
bytes must not. Neither cell rebuilds from a different checkout and calls that
the originally tested binary.

Each cell creates **two fresh virtual environments**, one for the original wheel
and one for the sdist-built wheel. Only public test assets and examples are copied
to a separate temporary workspace. The extracted source directory is removed
before either suite runs. The test Python uses isolated mode; inherited
`PYTHONPATH`, Python home settings and product/model configuration variables are
removed. Child crash/restart tests use that environment's Python too.

Before and after discovery/execution the reporter verifies that `memleaf` and
its loaded submodules come from that environment's site-packages. It compares
installed test IDs and count to the source-job inventory, fails on empty/missing
cases, errors, failures, skips or expected failures, and verifies installed
package bytes after execution. It does not report success just because a help
command returned zero, or because a source-tree import masked a broken install.

## Reuse existing contracts; add native-process evidence

The existing complete public suite remains the main acceptance surface. Added
focused checks exercise Unicode/spaces in Vault paths, exact UTF-8 and LF/CRLF
file handling, real process lock contention, OS process-exit lock release,
case-insensitive identity collisions and missing timezone data. These checks do
not patch `os.name` to pretend Linux is Windows.

The installed smoke check starts the actual generated `memleaf-mcp` executable
through byte pipes twice and uses the packaged Hermes MCP client for a
`scope_catalog -> search -> read` exchange. It overrides the child environment to
`PYTHONUTF8=0` and `PYTHONIOENCODING=cp1252`; the MCP transport must still exchange
UTF-8, including Chinese and emoji. Both actual console scripts also run directly.

Hermes' two base-interface classes are stubbed solely to load the bundled client.
Transport, executable, process boundary and Vault operations are real; a running
Hermes installation, Provider registration, installed-host lifecycle and user
configuration are **not** tested by this stub. The report leaves installed
Hermes/live semantic acceptance `not_run` and `switch_authorized=false`.

The suite's old POSIX-only exclusion for one symlink test is removed. Hosted
runners must support the filesystem primitives under test. Capability failures or
other skips make the gate fail rather than becoming a green reduced suite. A
separate native setup issue can then be diagnosed explicitly; assertions must
not be disabled merely to obtain a green matrix.

## Encoding and timezone fixtures

The general test runner explicitly enables UTF-8 for test-file I/O on all
platforms; historical tests contain Unicode fixtures. That harness setting is
not evidence that a user terminal has UTF-8 enabled. The separate stdio tests
intentionally disable it, so protocol correctness does not depend on the harness.

Windows does not necessarily have an IANA zoneinfo database. The build job obtains
`tzdata` as a **test-only wheel**, hashes it in the same manifest, and verification
jobs install only that bound wheel offline into the disposable test environments.
Memleaf runtime dependencies and its behavior when timezone data is unavailable
remain unchanged. No dependency is silently installed into a user's environment.
The negative missing-zone test still checks explicit failure and UTC fallback.
Build tools and runners can evolve; reports retain Python/OS/architecture and
artifact hashes, not a claim of hermetic or bit-reproducible build infrastructure.

## Reports, failure and release dependency

The source report and each cell's stage JSON/logs are uploaded separately. Reports
include actual test count and skips, test-inventory hash, package import root,
Python/OS/architecture, durations and the candidate manifest. An interrupted or
failed command fails the job; available earlier-stage reports remain available.
A wheel pass followed by an sdist failure is not a platform pass. `fail-fast:false`
allows other cells to finish without hiding their results; it does not relax the
aggregate dependency.

The existing release job now requires **build and all installed-artifact cells**.
Existing release conditions and explicit `release: v...` authorization remain.
Ordinary development commits do not create tags, GitHub Releases or PyPI uploads.
This change only adds a deterministic prerequisite; it cannot replace live Flash,
held-out multi-turn review, real host testing, backup/Forget checks or operator
switch authorization. Until a commit's matrix has actually run, its native
platform results are `not_run`, not inferred from a written workflow.

## Local reproduction

From a clean candidate source directory, with its declared build backend available:

```sh
python -I -X utf8 tests_public/run_contracts.py --tests tests_public --source-root src --report source-results.json
python -m build --wheel --sdist
python -m pip download --only-binary=:all: --no-deps --dest dist/test-dependencies tzdata
python tests_public/verify_distribution.py manifest --dist dist --source-report source-results.json --commit <candidate-sha>
python tests_public/verify_distribution.py verify --dist dist --output <new-output-directory> --commit <candidate-sha> --manifest-sha256 <printed-manifest-sha256>
```

`build`/setuptools are developer tools. The verifier itself uses the standard
library and the explicitly installed backend for the sdist build. It never
implicitly fetches a model or a package at runtime: artifact installations use
`--no-index --no-deps`. On a machine with system IANA data, a local manifest may
omit the test dependency; record that difference rather than claiming the local
run used CI's identical dependency set. Existing output is refused. Reports and
logs here contain synthetic test data only; real-model trace packages follow the
separate private acceptance contract and must not be uploaded by this job.
