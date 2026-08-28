# memleaf v0.1.1 — one-line Hermes setup

This maintenance release turns the PyPI package into the primary installation path for Hermes users.

## Highlights

- Adds `memleaf install` as a first-class installer command.
- Packages the Hermes MemoryProvider inside the PyPI distribution.
- Installs or upgrades the user-level memleaf provider without requiring a Git checkout.
- Initializes the default Vault, discovers/configures the model route, activates the Hermes MemoryProvider, configures MCP, applies lifecycle settings, and verifies all 11 MCP tools.
- Supports upgrading an existing v0.1 source-installed memleaf provider symlink.
- Adds `python -m memleaf` as a CLI entry point.

## Recommended installation

```bash
python -m pip install -U memleaf && python -m memleaf install
```

The user does not need to clone the repository or run `install.sh`.

## Scope

v0.1.1 still supports Hermes only. Codex and Antigravity are outside the supported host scope for this release.
