# Hermes MCP runtime and multi-environment installs

This document covers the Hermes-side `memleaf-mcp` entry, the supported
`--vault` argument, and what to do when an official Hermes Python environment
and a source/editable memleaf virtual environment both exist.

## Normal installation

The supported upgrade command is:

```bash
python -m pip install -U memleaf && python -m memleaf install
```

The installer now writes `mcp_servers.memleaf` through Hermes' canonical
`config set` command and reads `config.yaml` back before it reports success. It
does **not** treat a successful MCP connection probe as proof that the entry was
saved.

The persisted entry has this shape:

```yaml
mcp_servers:
  memleaf:
    command: /absolute/path/to/memleaf-mcp
    args:
      - --vault
      - /absolute/path/to/the/vault
    enabled: true
    lazy: true
    idle_timeout_seconds: 60
```

`memleaf-mcp` supports the following Vault forms:

```text
memleaf-mcp --vault /path/to/vault
memleaf-mcp --vault=/path/to/vault
```

The installer writes the first form as a YAML argument list.

## Two memleaf environments

A common development setup contains both:

- Hermes' managed Python environment; and
- a cloned repository installed into a separate `.venv` with `pip install -e`.

When `config.yaml` already points at a different absolute `memleaf-mcp`
executable for the same Vault, the default installer stops **before changing
Hermes' Provider or MCP configuration**. It prints both runtime paths and asks
for an explicit policy:

```bash
# Migrate Hermes MCP and Provider to the memleaf installation running this command
python -m memleaf install --mcp-runtime current

# Keep the executable already recorded in config.yaml
# This succeeds only when that executable exists and reports the exact same version.
python -m memleaf install --mcp-runtime existing
```

`auto` is the default policy and refuses to choose between two environments:

```bash
python -m memleaf install --mcp-runtime auto
```

The "current" runtime is resolved from the scripts directory of the Python
interpreter running the installer before consulting `PATH`. This prevents a
source `.venv` already present on `PATH` from being mistaken for the Hermes
managed environment.

## Windows examples

PowerShell, official Hermes environment selected by `install.ps1`:

```powershell
irm https://raw.githubusercontent.com/miffyblueboo/memleaf/main/install.ps1 | iex
```

Explicitly migrate from an old source environment to the environment running
the installer:

```powershell
python -m memleaf install --mcp-runtime current
```

Keep an already configured source environment after an exact version check:

```powershell
F:\memleaf\.venv\Scripts\python.exe -m memleaf install --mcp-runtime existing
```

A valid manual Windows entry is:

```yaml
mcp_servers:
  memleaf:
    command: "F:\\memleaf\\.venv\\Scripts\\memleaf-mcp.exe"
    args:
      - --vault
      - "F:\\memleaf\\vault"
    enabled: true
```

Windows drive-letter case and `/` versus `\` separators are treated as path
equivalents. Different absolute virtual-environment paths are still reported
as different runtimes.

## Manual recovery without `hermes mcp add`

The installer's error output includes ready-to-run recovery commands. Their
canonical form is:

```bash
hermes config set mcp_servers.memleaf.enabled false
hermes config set mcp_servers.memleaf.command /absolute/path/to/memleaf-mcp
hermes config set mcp_servers.memleaf.args '["--vault","/absolute/path/to/vault"]'
hermes config set mcp_servers.memleaf.enabled true
hermes config set mcp_servers.memleaf.lazy true
hermes config set mcp_servers.memleaf.idle_timeout_seconds 60
hermes mcp list
hermes mcp test memleaf
```

The entry is disabled first so an interrupted update cannot leave an active,
half-written command.

When using Hermes' interactive command directly:

```bash
hermes mcp add memleaf --command /absolute/path/to/memleaf-mcp --args --vault /absolute/path/to/vault
```

`--args` must be the last Hermes option. The line `Connected! Found ... tools`
only confirms discovery. Persistence is complete only after Hermes prints its
final `Saved 'memleaf' ... config.yaml` message. Always verify afterward with:

```bash
hermes mcp list
hermes mcp test memleaf
```

## Failure and rollback behavior

Before changing Hermes, the installer inspects the existing MCP entry and its
Vault. Once changes begin, it snapshots:

- Hermes `config.yaml`;
- Hermes `memleaf.json`; and
- the installed `plugins/memleaf` Provider directory.

If MCP persistence, MCP lifecycle configuration, the 12-tool test, Provider
copy/version validation, Provider activation, or native-source registration
fails, those Hermes paths are restored to their pre-install state. Each
snapshot is restored independently: a failure on one path does not prevent the
remaining paths from being attempted, and any incomplete restoration is
reported as `rollback_status=failed`. The failure output reports the exact
stage, nested MCP reason, runtime paths, backup path, rollback result, and
recovery commands. Use `--json` for the complete machine-readable result.

---

# Hermes MCP 运行环境与双环境安装（中文）

当机器上同时存在 Hermes 官方 Python 环境和源码仓库 `.venv` 时，默认执行：

```powershell
python -m memleaf install
```

若 Hermes 的 `config.yaml` 已指向另一套绝对路径的
`memleaf-mcp(.exe)`，安装器会在修改 Provider、`memleaf.json` 或 MCP 配置
之前停止，不再自动选边。

明确统一到当前执行安装命令的环境：

```powershell
python -m memleaf install --mcp-runtime current
```

保留 `config.yaml` 中已有的环境：

```powershell
python -m memleaf install --mcp-runtime existing
```

`existing` 只在已有 executable 存在，并且 `memleaf-mcp --version` 与当前
核心版本完全一致时通过。默认 `auto` 不执行配置文件中发现的另一套程序。

安装器不再依赖交互式 `hermes mcp add` 的返回码判断是否保存，而是通过
`hermes config set` 写入后直接读取 `config.yaml` 回验。`memleaf-mcp` 的
标准参数为：

```text
memleaf-mcp --vault <Vault绝对路径>
```

手工使用 `hermes mcp add` 时，`--args` 必须放在最后；看到
`Connected! Found ... tools` 只代表连接测试成功，必须继续完成交互并看到
最终 `Saved` 提示。之后仍应执行：

```powershell
hermes mcp list
hermes mcp test memleaf
```
