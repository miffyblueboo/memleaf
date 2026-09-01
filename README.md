# 冥想盆 · memleaf

> 一个本地优先、Markdown 驱动、面向多个 AI Agent 的共享记忆核心。

[English](README.en.md) · [PyPI](https://pypi.org/project/memleaf/) · [GitHub](https://github.com/miffyblueboo/memleaf)

> **当前版本：0.2.8。**
> 核心库、Vault、stdio MCP Server、初始化 CLI、模型路由、提炼流程、受控检索协议和宿主适配器已经实现。memleaf 0.2.8 通过 PyPI 分发。
> **当前版本支持 Hermes 和 Codex。** Antigravity（反重力）不检测、不安装、不配置。

## 项目定位

memleaf 把 AI Agent 的长期记忆保存为用户自己拥有的本地 Markdown 文件，并让多个 Agent 共享同一个 Vault。

- 不依赖向量数据库、embedding 服务或常驻后台；
- 不要求 memleaf 账号、云端服务、云同步或遥测；
- `knowledge/` 中的 Markdown 是当前有效记忆的事实来源；
- 可用 Obsidian、VS Code、Vim 等普通工具直接查看和编辑；
- 通过本地 stdio MCP 提供主动检索、读取、记忆维护等能力；
- 核心运行时只使用 Python 标准库，要求 Python 3.11 或更高版本。

memleaf 不会把整个 Vault 或整段历史对话自动塞进模型上下文。自动检索采用“Scope Map → 候选目录 → 受控读取正文”的流程。

## 当前工作流

```text
可见的 user/assistant 对话
        │
        ├─ capture：脱敏后写入 inbox/<source>/<session>.md
        │
        ├─ 自动检索入口：只注入有界 Scope Map
        │       │
        │       └─ Agent 选择 Scope 和 Query
        │              └─ search：返回候选目录
        │                    └─ read：按需读取少量关键正文
        │
        └─ process / remember：由模型判断并写入 knowledge/
                                └─ 状态更新时旧版本进入 history/
```

### 自动注入和检索

自动注入只包含 Scope 的标识、父级和别名，以及检索协议提示；不包含记忆 ID、标题、正文或全量历史。Scope Map 单页最多 20 项、约 2000 字符。

正常用户消息的推荐链路是：

1. Agent 使用当前完整会话和 Scope Map 选择检索范围与查询词；
2. 至少调用一次 `search`；
3. `search` 候选默认只返回 `memory_id` 和标题；Scope 已由 Scope Map 与本次搜索参数确定，不随每条候选重复返回，也不直接返回正文；
4. 只有需要引用事实时，才使用同一轮返回的 `retrieval_id` 调用 `read`；
5. 根据读取到的关键记忆回答，不把所有候选全部读完。

当前限制：

- Scope Map 最多 20 项、约 2000 字符；
- search 候选单页最多 20 项、约 4000 字符；
- read 单页正文最多 2000 字符；
- 受管理轮次最多读取 3 个不同记忆、累计 6000 个正文字符；
- `retrieval_id` 必须属于当前轮次，且必须先有成功的 `search` 才能 `read`；
- `found`、`no_match` 和工具错误是不同状态，错误不能被伪装成无匹配；
- `context()` 和 Python `search(view="full")` 作为兼容接口保留，但不属于自动检索路径。

Hermes 使用原生 MemoryProvider 维护生命周期，并通过 MCP 获取 Scope Map；Hermes 的检索门控是 Soft Gate，不能宣称阻止所有未检索回答。

## 记忆提炼规则

memleaf 不把每句话都保存为记忆。处理一轮完整的 user + assistant 可见文本时，模型先判断是否存在明确的未来复用价值：

- `CREATE`：没有相关现存记忆，创建一条原子、可独立理解的记忆；
- `UPDATE`：同一未来用途的现存记忆需要更新，沿用原 `memory_id`；旧内容进入 `history/`；
- `NO_CHANGE`：只是重复、查询、临时状态、测试、审计、诊断或没有稳定复用价值，不新增记忆。

额外约束：

- 通常一轮产生 0～1 条记忆，而不是按句子拆分；
- 新记忆必须有稳定标题、完整正文和合理 Scope；
- 项目、负责人等归属不明确时延后记录，不猜测为 `global`；
- 相同未来用途优先 UPDATE/NO_CHANGE，不重复 CREATE；
- 用户显式要求保存时可调用 `remember`，但仍会整理、校验和去重；
- 模型、解析、写入或索引失败时保留 inbox 和处理水位，后续可重试；
- 自动清理有 24 小时安全期，不会因为一次处理失败就删除原始捕获。

## 安装

要求 Python 3.11+；当前版本支持 Hermes 和 Codex。Hermes 是默认安装目标，Codex 使用明确的独立安装命令。

### Windows

已经安装 Hermes 的 Windows 10/11 用户，在 **PowerShell** 中只需要执行一行：

```powershell
irm https://raw.githubusercontent.com/miffyblueboo/memleaf/main/install.ps1 | iex
```

Windows 安装器会优先使用 Hermes 自带的 Python 环境，因此不要求 Hermes 的 Python 已加入系统 PATH。它会从 PyPI 安装或升级 memleaf，然后自动完成 Hermes 接入。

默认情况下会识别 Hermes 官方 Windows 安装位置：

```text
%USERPROFILE%\.memleaf\                              # memleaf 数据 Vault
%LOCALAPPDATA%\hermes\plugins\memleaf\              # Hermes MemoryProvider
%LOCALAPPDATA%\hermes\memleaf.json                   # Provider 配置
%LOCALAPPDATA%\hermes\bin\hermes.exe / hermes.cmd   # Hermes 命令入口
```

如果设置了 `HERMES_HOME`，memleaf 会优先使用该目录。

### macOS / Linux

```bash
python -m pip install -U memleaf && python -m memleaf install
```

### 更新 memleaf

安装命令同时也是升级命令，不需要先卸载旧版本。Windows 用户重新执行上面的 PowerShell 一行命令；macOS / Linux 用户重新执行上面的 `pip install -U` 命令即可。

升级时不会迁移或删除现有记忆。Vault 选择顺序固定为：

1. 用户本次明确传入的 `--vault`；
2. 已有 Hermes `memleaf.json` 中正在使用的 Vault；
3. `MEMLEAF_VAULT` 环境变量；
4. 默认 `~/.memleaf`。

因此从早期版本升级时，即使原来使用的是自定义 Vault，也会继续使用原目录。若已有 `memleaf.json` 损坏或其中的 Vault 路径无效，升级会明确失败，而不会静默切换到一个新的默认 Vault。

两种安装方式最终都会自动：

1. 初始化默认 Vault；
2. 安装或升级 Hermes MemoryProvider；
3. 发现并保存可用的聊天模型路由；Hermes CLI 返回的脱敏凭证不会被当成真实 API key，而会继续尝试环境变量和 Hermes `.env`；已有有效 memleaf 路由会保留；
4. 激活 `memory.provider=memleaf`；
5. 通过 Hermes 官方 CLI 配置 memleaf MCP；
6. 配置 MCP lazy/idle 生命周期；
7. 验证 MCP Server 能发现 11 个工具；
8. 写入本地 Agent 状态索引。

完成后重启 Hermes。

如果没有检测到 Hermes、无法取得完整模型路由、Provider 激活失败或 MCP 的 11 工具验证失败，安装会明确返回失败，不会把未完成的接入报告为成功。

仓库中的 `install.sh` 保留给源码开发、离线源码安装和故障排查；普通 PyPI 用户不需要执行它。

### Codex

先安装或升级 memleaf，再明确选择 Codex：

```bash
python -m pip install -U memleaf && python -m memleaf install --host codex
```

该命令会复用已有 Hermes/Codex memleaf 配置中的唯一 Vault，或创建默认的 `~/.memleaf`；通过 Codex CLI 注册 memleaf MCP，并安全合并生命周期 Hook。它不会修改 Codex 的模型配置，也不会在两个宿主已指向不同 Vault 时擅自选边。

安装后打开 Codex，运行 `/hooks`，审核并信任 memleaf Hook。完成授权前，MCP 可用不等于自动捕获、检索门控和提炼已经激活；安装结果会明确显示 `pending_user_review`，不会把待审核状态报告为已启用。

Codex 只作为宿主，不作为 memleaf 的提炼模型来源。已有的独立 memleaf Model Route 会继续复用；如果当前 Vault 还没有完整 Model Route，Codex 安装仍可完成 MCP/Hook 配置，但结果会明确返回 `processing_status=model_route_required`，在配置模型前不能把“自动记忆提炼”视为已就绪。memleaf 不读取、不复制也不修改 Codex 的 `model`、`model_provider`、`base_url` 或认证信息，也不会为了提炼偷偷消耗 Codex 会话额度。Codex-only 用户可针对同一个 Vault 运行：

```bash
python -m memleaf init --no-hermes --vault /path/to/the/same/vault
```

完成独立 Model Route 配置后，再使用 Codex 自动提炼。DeepSeek、OpenRouter 或其他自定义 Codex provider 因此保持原样。

Antigravity（反重力）当前不支持，安装流程不会检测、安装或修改其配置。

### 高级初始化命令（可选）

先查看计划变更：

```bash
memleaf init --dry-run --json
```

确认后初始化：

```bash
memleaf init --all --defaults
```

常用选项：

```bash
memleaf init --vault /path/to/vault
memleaf init --no-hermes
memleaf init --no-model-discovery
memleaf init --json
```

`init` 下的 `--no-codex` 和 `--no-antigravity` 为兼容保留的无操作参数；Codex 必须通过 `install --host codex` 明确安装。宿主配置只会在检测证据可靠、结构可识别且没有冲突时修改；修改前会创建备份，未知或冲突配置保持不变。

## Codex 使用方式

- `UserPromptSubmit` 只注入有界 Scope Map 和当前轮次的检索协议，不注入记忆标题或正文；
- `PreToolUse` / `PostToolUse` 将 memleaf `search` 和 `read` 绑定到当前 `retrieval_id`，记录真实检索结果与读取预算；
- `Stop` 捕获可见的 assistant 内容并触发处理；未检索时最多进行有限续跑，最终会如实降级，不伪造检索成功；
- 压缩后的 `SessionStart` 恢复当前轮次检索上下文，但不会把 Vault 全量重新注入；
- 主动 `remember`、`forget`、`stats` 等操作继续通过同一个 memleaf MCP 完成。

Codex Hook 必须经过用户审核和信任。若 Hook 未激活、被禁用或 Codex 版本不支持对应生命周期事件，自动捕获和自动提炼不会生效；MCP 主动工具与 Hook 生命周期是两个独立状态。

## Hermes 使用方式

Hermes 的原生 Provider 和 MCP Server 是两个不同入口，但共用同一个 `$HOME/.memleaf`：

- Provider 负责自动捕获可见的 Hermes user/assistant 轮次，并在完整轮次后触发 `process`；
- Provider 只提供 Scope Map，不直接注入记忆正文；
- Agent 需要通过 MCP `search` 找到候选，再用当前 `retrieval_id` 调用 `read`；
- MCP 仍用于主动 `search`、`remember`、`forget` 和维护操作；
- MCP 连接失败时会关闭坏连接，后续请求可重建连接；
- `process` 使用已发现或用户配置的模型路由执行准入判断和记忆整理；失败数据保留在 inbox 中；
- cron、flush、subagent 等非主用户会话不会自动召回、捕获或处理。

安装完成后可检查：

```bash
hermes memory status
```

正常重启 Hermes 后，原生 Provider 和 MCP 应分别显示可用。安装脚本只在 Hermes 可执行文件存在时修改 Hermes 配置；没有 Hermes 时会跳过该部分并给出提示。

## MCP Server

直接运行本地 stdio Server：

```bash
memleaf-mcp --vault "$HOME/.memleaf"
```

也可以使用模块入口：

```bash
python -m memleaf.mcp_server --vault "$HOME/.memleaf"
```

不传 `--vault` 时默认使用 `~/.memleaf`；也可以设置 `MEMLEAF_VAULT`。正常情况下不需要手工常驻，Hermes 或 Codex 会按需启动它。stdout 只输出 JSON-RPC，日志不会污染协议通道。

当前提供 11 个工具：

| 工具 | 用途 |
| --- | --- |
| `capture` | 捕获一条明确传入的可见对话事件到 inbox |
| `context` | 兼容保留的有界轻量目录，不用于自动检索路径 |
| `scope_catalog` | 返回 Scope、父级和别名，不返回具体记忆正文 |
| `search` | 返回有界候选目录和 `found`/`no_match` 状态 |
| `read` | 使用当前 `retrieval_id` 分页读取选中的记忆正文 |
| `process` | 处理完整 inbox 轮次并按准入规则提炼 |
| `remember` | 用户明确要求时创建或更新记忆 |
| `forget_memory` | 按精确 ID 删除一条记忆 |
| `forget_about` | 忘记明确主题；有歧义时只返回候选 |
| `rebuild_index` | 重建可重建的本地派生索引 |
| `stats` | 返回 Vault 计数和诊断统计 |

`search` 的候选只是线索，标题不能单独作为事实依据。受管理检索必须使用同一轮的 `retrieval_id` 完成 `search → read`；工具错误、Scope 冲突或读取预算耗尽都应如实处理。

## Python API

核心库没有第三方运行时依赖：

```python
from pathlib import Path

from memleaf import Memleaf

service = Memleaf.initialize(Path("~/.memleaf").expanduser())

memory = service.create_memory(
    title="用户偏好",
    body="用户偏好使用本地 Markdown 保存长期记忆。",
    tags=["preference", "memleaf"],
    scopes=["global"],
    type="preference",
)

for item in service.search("Markdown"):
    print(item.memory_id, item.title)
```

常用接口：

```text
capture()             捕获可见事件
process()             处理完整 inbox 轮次，需要模型路由
remember()            显式保存，需要模型路由
create_memory()       直接创建一条 Markdown 记忆
search()              本地检索，不更新命中统计
context()             兼容接口，返回轻量目录
read() / read_page()  读取记忆或分页正文
forget_memory()
forget_about()
rebuild_index()
stats()
compact()             按阈值整理低优先级记忆，需要模型路由
```

离线示例默认使用临时 Vault，不写入 `~/.memleaf`，也不访问网络：

```bash
python examples/basic_usage.py
python examples/basic_usage.py --vault /path/to/your/vault
```

MCP stdio 示例：

```bash
python -m memleaf.mcp_server --vault /path/to/your/vault \
  < examples/mcp_stdio.ndjson
```

## 模型路由

捕获、索引、目录检索和读取可离线运行；`process()`、`remember()` 和 `compact()` 需要可用的模型能力。

`llm.mode` 支持：

- `auto`：优先使用显式注入的 host backend，失败后回退到完整 API 路由；
- `host`：只使用 Python API 显式传入的宿主回调；
- `api`：只使用本地配置的 HTTP API。

`memleaf init` 从 Hermes 的可读取模型配置中寻找完整的聊天模型路由，过滤非聊天模型，并确定性选择轻量模型作为提炼模型。Hermes CLI 输出中的 `***`、`sk-p...7890` 等显示脱敏值会被视为缺失凭证，并继续尝试环境变量或 Hermes `.env`；若仍找不到真实凭证则安装明确失败，不会写入一个看似成功但不可调用的模型路由。未发现可用路由时复用已有的有效 memleaf 配置；要保留自选路由并跳过扫描，使用 `--no-model-discovery`。不能调用的 OAuth-only 或只有模型名的配置不会被误判为可用。

API 配置示例：

```yaml
llm:
  mode: api
  provider: deepseek
  protocol: openai
  base_url: https://api.deepseek.com/v1
  api_key: your-api-key
  model: your-chat-model
  request_timeout: 120
  diagnostic_logging: false
```

也兼容 `api_key_env` 配置。当前新路由可以把 API key 直接写入 Vault 的 `config.yaml`；文件权限为 `0600`，日志和状态输出不会打印 key。若使用第三方或云端模型，提炼所需的输入会按用户选择的模型调用链发送；这不是 memleaf 的云同步。

支持的 HTTP 协议包括 OpenAI Chat Completions、Claude Messages、Gemini `generateContent` 及对应兼容端点。

`llm.diagnostic_logging` 默认关闭。开启后只写入有界的结构统计，不保存 prompt、模型正文、字段值、密钥、URL 或异常正文。

## 原生记忆源

可以在 `config.yaml` 中声明 Agent 自己维护的文本或 Markdown 文件作为只读原生源：

```yaml
native_sources:
  hermes_notes:
    agent: hermes
    path: /absolute/path/to/hermes-notes.md
    share: true
    enabled: true
    format: markdown
```

规则如下：

- 原生文件是源事实，memleaf 不修改它，也不会默认复制到 `knowledge/`；
- 每个源必须是唯一文件，当前单文件上限为 5 MiB；
- `share: true` 时允许其他 Agent 读取；否则仅源所属 Agent 可访问；
- memleaf 建立有界索引，读取正文前会重新校验源文件当前内容；
- 文件内容变化后会刷新索引，失效或缺失不会伪造旧正文。

## Vault 目录

默认 Vault 是 `$HOME/.memleaf`，也可以通过 `--vault` 或配置指定本地目录：

```text
$HOME/.memleaf/
├── config.yaml                  # Vault、Agent、模型和处理配置
├── README.md                    # Vault 说明
├── inbox/                       # 脱敏后的原始可见对话捕获
│   └── <source>/<session>.md
├── knowledge/                   # 当前有效记忆，Markdown 是事实来源
│   └── <memory_id>.md
├── history/                     # 更新或压缩前的历史版本
│   └── <memory_id>--<version>.md
├── _index/
│   ├── tags.json                # 标签、别名、关键词和链接索引
│   ├── processed.json           # 处理水位和幂等状态
│   ├── native_sources.json      # 原生源只读索引
│   ├── agents.json              # 宿主检测和配置状态
│   ├── host_ingest.json         # 宿主生命周期游标
│   ├── retrieval_gate.json      # 有界检索轮次账本，不含查询或正文
│   ├── retrieval_gate.lock
│   ├── vault.lock
│   └── compaction.json          # 压缩事务恢复账本
└── logs/                        # 仅 diagnostic_logging=true 时创建
    └── model-diagnostics.jsonl  # 有界结构诊断，不含模型正文
```

`knowledge/` 和 `history/` 是可读数据；`_index/` 主要是派生索引或运行状态。删除或直接修改 `_index/` 可能丢失处理水位、Hook 游标或当前检索账本；需要重建时优先使用 `rebuild_index()`。

默认目录尽量使用 `0700`，文件默认明文保存。memleaf 没有内置加密层，请自行保护 Vault、备份和模型调用凭证。

## 隐私与安全边界

- 捕获默认只接收调用方明确传入的 user/assistant 可见文本；不捕获 system prompt、developer prompt、隐藏推理、原始工具输出或附件全文；
- 捕获落盘前尽力脱敏常见 API key、Bearer token、Cookie、JWT 和私钥，但脱敏不是加密，也不能保证识别所有敏感信息；
- 路径校验、符号链接检查、Vault 锁、同目录临时文件、fsync 和原子替换用于保护本地写入；
- memleaf 不主动上传整个 Vault，也没有托管后台、遥测或账号系统；
- 选择 API/云端模型后，模型处理输入会离开本机并发送给该提供方；
- `context()` 或无宿主绑定的客户端只能获得单页边界，不能宣称具备跨轮硬预算。

## 开发与验证

要求 Python 3.11+。本地运行测试：

```bash
PYTHONPATH=src python3.11 -m unittest discover -s tests -p 'test_*.py' -v
python3.11 -m compileall -q src tests examples
git diff --check
```

GitHub Actions 覆盖 Linux 与 Windows 的 Python 3.11、3.12、3.13 测试，并验证 wheel/source distribution 构建、源码包测试和 Windows PowerShell 安装器语法。构建发行包需要 `build`：

```bash
python -m pip install build
python -m build --wheel --sdist
```


## 当前边界

以下内容不应被 README 或安装结果误解为已交付能力：

- Windows、macOS 和 Linux 都提供 Hermes 安装入口；Codex 通过 `memleaf install --host codex` 明确接入；源码 `install.sh` 仅保留给开发、离线源码安装和故障排查；
- 当前支持 Hermes 和 Codex；Antigravity（反重力）不检测、不安装、不配置；
- Codex 生命周期 Hook 需要用户在 `/hooks` 中审核并信任，安装程序不能代替用户完成授权；
- Hermes 的检索门控是 Soft Gate，不保证阻止所有未检索回答；
- 没有模型路由时只能捕获和检索，不能完成自动提炼、显式记忆或压缩；
- 真实宿主长期运行效果仍取决于本机 Agent 版本、配置、重启和模型可用性；
- 不提供 Obsidian 插件、Web 管理界面、云同步或透明加密层；
- `history/` 的批量清理不会被普通检索自动执行，删除操作需要明确调用维护接口。


## License

MIT，见 [LICENSE](LICENSE)。

**memleaf**
*Your memories, in files you own.*
