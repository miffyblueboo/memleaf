# memleaf 0.1.4 — credential-safe Hermes discovery

## Fixes

- Hermes display-redacted credentials such as `***` and `sk-p...7890` are no longer accepted as real API keys.
- When the Hermes CLI returns a masked credential, memleaf continues to look for the real value in the configured environment variable and Hermes `.env`.
- If no real credential is available, installation fails explicitly instead of writing a model route that looks configured but cannot call the API.
- Existing memleaf routes containing a masked credential are no longer reused, and runtime model routing rejects them as well.
- Cross-platform regression tests cover both successful `.env` fallback and fail-closed behavior.

## Windows

```powershell
irm https://raw.githubusercontent.com/miffyblueboo/memleaf/main/install.ps1 | iex
```

## macOS / Linux

```bash
python -m pip install -U memleaf && python -m memleaf install
```

Restart Hermes after installation.
