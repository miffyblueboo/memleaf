# memleaf 0.1.3 — Windows native Hermes support

## Highlights

- Restores the project to the MIT License.
- Adds native Windows 10/11 Hermes integration.
- Detects the official Windows Hermes home and launcher locations.
- Adds a one-line PowerShell installer that installs memleaf from PyPI and
  configures Hermes automatically.
- Keeps the existing macOS/Linux one-line installation flow.
- Verifies the Windows code path in GitHub Actions on Python 3.11, 3.12, and 3.13.

## Windows

Run in PowerShell:

```powershell
irm https://raw.githubusercontent.com/miffyblueboo/memleaf/main/install.ps1 | iex
```

The installer uses Hermes' managed Python when available, then installs memleaf,
installs the Hermes MemoryProvider, configures MCP, and verifies all 11 tools.

## macOS / Linux

```bash
python -m pip install -U memleaf && python -m memleaf install
```

Restart Hermes after installation.
