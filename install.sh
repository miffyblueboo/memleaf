#!/usr/bin/env bash

set -euo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
home_path=${HOME:?HOME is not set}
case "$home_path" in
  /*) ;;
  *) home_path="$PWD/$home_path" ;;
esac
home_root=$(cd -- "$home_path" && pwd -P)
requested_install_root=${MEMLEAF_INSTALL_ROOT:-"$home_path/memleaf"}
case "$requested_install_root" in
  /*) ;;
  *) requested_install_root="$home_path/$requested_install_root" ;;
esac
if [ -d "$requested_install_root" ]; then
  physical_install_root=$(cd -- "$requested_install_root" && pwd -P)
else
  physical_install_root="$requested_install_root"
fi
install_root="$requested_install_root"
python_bin=${MEMLEAF_PYTHON:-}
hermes_home=${HERMES_HOME:-"$home_path/.hermes"}

die() {
  printf 'memleaf install failed: %s\n' "$1" >&2
  exit 1
}

if [ -L "$requested_install_root" ]; then
  die "refusing symlinked installation root: $requested_install_root; place the local source at \"\$HOME/memleaf\""
fi

if [ "$project_root" != "$physical_install_root" ]; then
  die "run from $home_root/memleaf (place the local source at \"\$HOME/memleaf\"), or set MEMLEAF_INSTALL_ROOT only for an explicit isolated test/development checkout"
fi

[ -f "$project_root/pyproject.toml" ] || die "pyproject.toml is missing from $project_root"
[ -d "$project_root/src/memleaf/hermes_provider" ] || die "Hermes provider is missing from $project_root/src/memleaf/hermes_provider"

venv_path="$install_root/.venv"
user_bin="$home_root/.local/bin"
command_path="$venv_path/bin/memleaf-mcp"
vault_path="$home_root/.memleaf"
plugin_source="$project_root/src/memleaf/hermes_provider"

case "$hermes_home" in
  /*) ;;
  *) hermes_home="$home_root/$hermes_home" ;;
esac
plugin_target="$hermes_home/plugins/memleaf"

if [ -L "$venv_path" ]; then
  die "refusing to use symlinked virtual environment: $venv_path"
fi
if [ -e "$venv_path" ] && [ ! -d "$venv_path" ]; then
  die "refusing to overwrite non-directory virtual environment path: $venv_path"
fi
mkdir -p "$(dirname "$venv_path")"

if [ ! -x "$venv_path/bin/python" ]; then
  if [ -z "$python_bin" ]; then
    for candidate_python in python3.11 python3.12 python3.13 python3; do
      if command -v "$candidate_python" >/dev/null 2>&1 && \
          "$candidate_python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python_bin="$candidate_python"
        break
      fi
    done
  fi
  [ -n "$python_bin" ] || die "Python 3.11+ is required; install it or set MEMLEAF_PYTHON to its executable"
  if ! "$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    die "MEMLEAF_PYTHON must point to a working Python 3.11+ interpreter: $python_bin"
  fi
  "$python_bin" -m venv --without-pip "$venv_path"
fi
[ -x "$venv_path/bin/python" ] || die "virtual environment did not create $venv_path/bin/python"
"$venv_path/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11) or sys.prefix == sys.base_prefix)' \
  || die "existing .venv must be a Python 3.11+ virtual environment"

# memleaf has no third-party runtime dependencies.  A clean Python 3.12+
# virtual environment does not necessarily contain setuptools, so the local
# installer must not depend on pip or a network build step.  Install an
# editable source path and small console wrappers using only the stdlib.
"$venv_path/bin/python" - "$project_root" <<'PY'
from __future__ import annotations

import os
from pathlib import Path
import shlex
import sys
import sysconfig

project_root = Path(sys.argv[1]).resolve()
source_root = project_root / "src"
if not (source_root / "memleaf" / "__init__.py").is_file():
    raise SystemExit(f"memleaf source package is missing from {source_root}")
source_text = str(source_root)
if "\n" in source_text or "\r" in source_text:
    raise SystemExit("memleaf source path contains a newline")

venv_root = Path(sys.prefix).resolve()
purelib = Path(sysconfig.get_path("purelib")).resolve()
if purelib != venv_root and venv_root not in purelib.parents:
    raise SystemExit(f"virtual environment site-packages resolves outside venv: {purelib}")
purelib.mkdir(parents=True, exist_ok=True)

def replace(path: Path, content: str, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)

replace(purelib / "memleaf-local.pth", source_text + "\n", 0o644)
python = shlex.quote(str(Path(sys.executable).absolute()))
for command, module in (("memleaf", "memleaf.cli"), ("memleaf-mcp", "memleaf.mcp_server")):
    wrapper = f'#!/bin/sh\nexec {python} -m {module} "$@"\n'
    replace(venv_root / "bin" / command, wrapper, 0o755)
PY

for executable in memleaf memleaf-mcp; do
  [ -x "$venv_path/bin/$executable" ] || die "editable install did not create $venv_path/bin/$executable"
done

# An editable install must resolve to this checkout, not a stale checkout.
installed_source=$(
  "$venv_path/bin/python" - "$project_root" <<'PY'
from pathlib import Path
import sys

project_root = Path(sys.argv[1]).resolve()
try:
    import memleaf
except Exception as error:
    raise SystemExit(f"cannot import installed memleaf: {error}")
source = Path(memleaf.__file__).resolve()
expected = project_root / "src" / "memleaf"
if source.parent != expected and expected not in source.parents:
    raise SystemExit(f"installed memleaf resolves outside checkout: {source}")
print(source)
PY
)
[ -n "$installed_source" ] || die "could not verify editable memleaf source"

if [ -L "$user_bin" ]; then
  die "refusing to use symlinked user executable directory: $user_bin"
fi
if [ -L "$home_root/.local" ]; then
  die "refusing to use symlinked user local directory: $home_root/.local"
fi
if [ -e "$home_root/.local" ] && [ ! -d "$home_root/.local" ]; then
  die "refusing to overwrite non-directory user local path: $home_root/.local"
fi
if [ -e "$user_bin" ] && [ ! -d "$user_bin" ]; then
  die "refusing to overwrite non-directory user executable path: $user_bin"
fi
mkdir -p "$user_bin"
for executable in memleaf memleaf-mcp; do
  link="$user_bin/$executable"
  target="$venv_path/bin/$executable"
  if [ -L "$link" ]; then
    [ "$(readlink "$link")" = "$target" ] || die "existing user link points elsewhere: $link"
  elif [ -e "$link" ]; then
    die "refusing to overwrite existing non-symlink $link"
  else
    ln -s "$target" "$link"
  fi
done

export PATH="$venv_path/bin:$user_bin:$PATH"
"$venv_path/bin/memleaf" init \
  --vault "$vault_path" \
  --no-codex \
  --no-hermes \
  --no-antigravity

hermes_bin=""
candidate_hermes=$(command -v hermes 2>/dev/null || true)
if [ -n "$candidate_hermes" ] && [ -x "$candidate_hermes" ]; then
  hermes_bin="$candidate_hermes"
fi

if [ -z "$hermes_bin" ]; then
  printf 'memleaf install: Hermes executable not found; native provider registration was skipped. Install Hermes and rerun ./install.sh.\n' >&2
else
  if [ -L "$hermes_home" ]; then
    die "refusing to use symlinked Hermes home: $hermes_home"
  fi
  if [ -e "$hermes_home" ] && [ ! -d "$hermes_home" ]; then
    die "refusing to overwrite non-directory Hermes home: $hermes_home"
  fi
  if [ -e "$hermes_home/plugins" ] && [ -L "$hermes_home/plugins" ]; then
    die "refusing to use symlinked Hermes plugin directory: $hermes_home/plugins"
  fi
  if [ -e "$hermes_home/plugins" ] && [ ! -d "$hermes_home/plugins" ]; then
    die "refusing to overwrite non-directory Hermes plugin path: $hermes_home/plugins"
  fi
  mkdir -p "$hermes_home/plugins"

  if [ -L "$plugin_target" ]; then
    [ "$(readlink "$plugin_target")" = "$plugin_source" ] || die "existing Hermes plugin link points elsewhere: $plugin_target"
  elif [ -e "$plugin_target" ]; then
    die "refusing to overwrite existing Hermes plugin path: $plugin_target"
  else
    ln -s "$plugin_source" "$plugin_target"
  fi

  "$venv_path/bin/python" - "$hermes_home/memleaf.json" "$command_path" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

from memleaf.locking import atomic_write_json

config_path = Path(sys.argv[1]).expanduser()
command_path = str(Path(sys.argv[2]).expanduser())
if config_path.is_symlink():
    raise SystemExit(f"refusing to write symlinked Hermes config: {config_path}")

config: dict[str, object] = {}
if config_path.exists():
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise SystemExit(f"invalid Hermes memleaf config: {config_path}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"Hermes memleaf config must be an object: {config_path}")
    config.update(value)

config.update({
    "vault": "~/.memleaf",
    "command": command_path,
    "timeout": 5,
    "process_timeout": 300,
    "auto_process": True,
})
atomic_write_json(config_path, config, mode=0o600)
PY

  if ! "$hermes_bin" config set memory.provider memleaf; then
    die "Hermes was detected at $hermes_bin but could not activate memory.provider=memleaf"
  fi

  if ! status_output=$("$hermes_bin" memory status 2>&1); then
    die "Hermes memory status failed after activating memleaf"
  fi
  status_lower=$(printf '%s' "$status_output" | tr '[:upper:]' '[:lower:]')
  printf '%s' "$status_lower" | grep -Eq 'provider[=:][[:space:]]*memleaf' \
    || die "Hermes status does not confirm provider=memleaf"
  printf '%s' "$status_lower" | grep -Eq 'plugin:[[:space:]]*installed' \
    || die "Hermes status does not confirm the memleaf plugin is installed"
  printf '%s' "$status_lower" | grep -Eq 'status:[[:space:]]*available([^a-z]|$)' \
    || die "Hermes status does not confirm the memleaf provider is available"
  printf '%s' "$status_lower" | grep -Eq 'active|memory\.provider[=:][[:space:]]*memleaf' \
    || die "Hermes status does not confirm the memleaf provider is active"

  # Record the native Provider evidence before touching the separate MCP
  # chain.  A later MCP failure must not erase this successful check.
  "$venv_path/bin/python" - "$vault_path" "$hermes_bin" "$hermes_home/memleaf.json" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

from memleaf.adapters.base import update_agents_index

vault_path = Path(sys.argv[1]).expanduser()
hermes_executable = str(Path(sys.argv[2]).expanduser().resolve())
provider_config = str(Path(sys.argv[3]).expanduser().resolve())
updates = {
    "hermes": {
        "agent": "hermes",
        "detected": True,
        "confidence": "high",
        "reason": "official Hermes MemoryProvider is installed, available, and active",
        "executable": hermes_executable,
        "config_path": provider_config,
        "status": "configured",
        "provider_status": "active",
        "user_action_required": False,
    }
}
if not update_agents_index(vault_path / "_index" / "agents.json", updates):
    raise SystemExit("could not update Hermes provider status in agents index")
PY

  # Configure the active MCP entry through Hermes' official CLI.  Keep this
  # separate from the native provider check above: either chain may fail, and
  # neither one is evidence for the other's status.
  if ! "$venv_path/bin/python" - "$vault_path" "$hermes_bin" "$hermes_home" "$command_path" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path

from memleaf.adapters.hermes import HermesAdapter

vault_path = Path(sys.argv[1]).expanduser().resolve()
hermes_executable = str(Path(sys.argv[2]).expanduser().resolve())
hermes_home = Path(sys.argv[3]).expanduser().resolve()
memleaf_command_path = Path(sys.argv[4]).expanduser()
if not memleaf_command_path.is_absolute():
    memleaf_command_path = Path.cwd() / memleaf_command_path
memleaf_command = str(memleaf_command_path)
adapter = HermesAdapter(
    home=Path(os.environ["HOME"]).expanduser().resolve(),
    env=os.environ,
    hermes_home=hermes_home,
    memleaf_command=memleaf_command,
)
detection = adapter.detect()
if detection.executable != hermes_executable or detection.confidence != "high":
    raise SystemExit("Hermes executable could not be verified for MCP configuration")
configured = adapter.configure(detection, vault_path)
if configured.status not in {"configured", "already_configured"}:
    raise SystemExit("Hermes memleaf MCP entry could not be configured")
if not adapter.configure_mcp_lifecycle(detection, idle_timeout_seconds=60):
    raise SystemExit("Hermes memleaf MCP lifecycle could not be configured")
if not adapter.test_mcp(detection, expected_tools=11):
    raise SystemExit("Hermes memleaf MCP test did not confirm 11 tools")
PY
  then
    # Keep the already-verified Provider state, but never leave a stale MCP
    # active marker after a failed configuration or test.
    if ! "$venv_path/bin/python" - "$vault_path" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

from memleaf.adapters.base import update_agents_index

vault_path = Path(sys.argv[1]).expanduser()
if not update_agents_index(
    vault_path / "_index" / "agents.json",
    {"hermes": {"mcp_status": "failed", "mcp_availability": "unavailable"}},
):
    raise SystemExit("could not record Hermes MCP failure in agents index")
PY
    then
      die "could not record Hermes MCP failure in agents index"
    fi
    die "Hermes memleaf MCP configuration/test failed"
  fi

  # The MCP status is written only after its own official test succeeds.  This
  # merge changes no Provider or other host fields.
  "$venv_path/bin/python" - "$vault_path" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

from memleaf.adapters.base import update_agents_index

vault_path = Path(sys.argv[1]).expanduser()
if not update_agents_index(
    vault_path / "_index" / "agents.json",
    {"hermes": {"mcp_status": "active", "mcp_availability": "available"}},
):
    raise SystemExit("could not update Hermes MCP status in agents index")
PY
  printf 'Hermes memory provider: verified active\n'
fi

printf 'memleaf installed at %s\n' "$install_root"
printf 'Python environment: %s\n' "$venv_path"
printf 'Data vault: %s\n' "$vault_path"
if [ -n "$hermes_bin" ]; then
  printf 'Hermes provider: %s\n' "$plugin_target"
fi
