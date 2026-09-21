# Installed artifact and native-platform verification

This is a deterministic packaging gate, not a runtime subsystem or live-model
acceptance gate. The public repository intentionally contains no test files.
CI therefore validates the source and the built artifacts without checking out
or executing an in-repository test suite.

## Build once, install the same bytes

The build job:

1. compiles `src/` with Python's standard-library compiler;
2. builds one wheel and one source distribution;
3. uploads only those two distributions as the workflow artifact.

The `verify-artifacts` job downloads those exact bytes and installs each artifact
in its own fresh virtual environment. It imports `memleaf` and checks the
reported package version. It does not read a source-tree checkout, run a model,
access a production Vault or create a release.

The matrix is:

| Native runner | Python |
|---|---|
| Ubuntu | 3.11, 3.12, 3.13 |
| Windows | 3.12 |
| macOS | 3.12 |

The source distribution is installed with its declared build backend. The
wheel and source distribution are checked separately so a successful wheel
install cannot hide a broken source package. `fail-fast: false` lets every
native cell report its own result; it does not relax the aggregate dependency.

## Release dependency

The release job still requires the build and every installed-artifact cell.
Only an explicitly authorized `release: v<version>` commit on `main` can create
a GitHub Release. Existing release assets are checked against the local bytes
before a missing asset is uploaded; existing assets are never replaced.

Ordinary commits do not create tags, GitHub Releases or PyPI uploads. These
checks do not establish live provider behavior, Hermes host installation,
Windows process semantics, migration safety or semantic quality.

## Local reproduction

From a clean checkout with the declared build backend available:

```sh
python -m compileall -q src
python -m build --wheel --sdist
```

For an isolated local import check, create a temporary virtual environment and
install the wheel and source archive separately with `--no-deps`. Do not add
local test files to this repository; they are intentionally ignored and are not
part of the release artifacts.
