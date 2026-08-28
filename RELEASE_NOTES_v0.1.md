# memleaf v0.1 — Hermes

冥想盆 memleaf 首次正式发布：把长期记忆保存在你自己拥有的本地 Markdown 文件里。

## v0.1 提供什么

- **仅支持 Hermes**：原生 MemoryProvider 自动捕获可见对话、触发提炼；主动 MCP 提供检索、读取和记忆维护。
- 自动输入只提供有界 Scope Map；`search → read` 按需读取正文，不一次注入整个记忆库。
- 按未来复用价值提炼，同一事项状态更新复用记忆 ID，旧版本进入 history；纯查询不写入长期记忆。
- 本地 Markdown Vault、可重建索引、失败保留 inbox 供重试；Python 3.11+，核心无第三方运行时依赖。
- 默认安装到 `$HOME/memleaf`，数据独立保存在 `$HOME/.memleaf`。

## 安装

macOS/Linux，先安装 Python 3.11+ 和 Hermes。首次安装：

```bash
git clone --branch v0.1 --depth 1 https://github.com/miffyblueboo/memleaf.git "$HOME/memleaf"
cd "$HOME/memleaf"
./install.sh
```

已有源码目录时不要重复 clone，先确认使用 v0.1 源码。安装后重启 Hermes。
安装会尝试发现 Hermes 的可调用模型配置；未发现时由用户补充并直接写入配置文件。
本次发布到 GitHub，不发布 PyPI。完整宿主安装使用源码目录；wheel 提供核心与 CLI。

## 预告

- **v0.2 将增加 Codex 支持。**
- 后续逐步支持更多 Agent 工具，遵守各宿主官方接入及授权规范。
- Codex 和反重力不属于 v0.1；本版不检测、不安装、不配置它们。

## 验证与边界

- 本地 Python 3.11 完整回归及源码包回归均通过 373 项测试。
- 本 Release 由 CI 在 Python 3.11/3.12/3.13 测试、构建与源码包测试全部通过后发布。
- Hermes 检索约束是 Soft Gate，不承诺阻止所有未检索回答。
- 最终版本的真实 Hermes 盲测、长期运行与真实模型语义验收仍需补充；自动化测试不替代这些验收。
- 使用云端提炼模型时，相关处理输入会发送给模型提供方；本地存储不等于所有处理都离线。
- 不迁移、不批量清理现有知识库，不自动覆盖已有运行目录。

## English summary

v0.1 supports Hermes only, with a native MemoryProvider, MCP search/read,
local Markdown storage, model-backed extraction, and versioned state updates.
v0.2 will add Codex support; more Agent tools will follow gradually.
This is a GitHub source release, not a PyPI release. Real-host blind tests and
long-running/model-semantic acceptance remain incomplete; automated tests are
not a substitute. See the bilingual READMEs for setup and limitations.
