# 发布检查清单

本文件是 memleaf 的长期发布 SOP，不记录某个具体版本的发布说明。

版本历史统一维护在 `CHANGELOG.md`；GitHub Release 从对应版本的 CHANGELOG
段落生成。不要再新增 `RELEASE_NOTES_*.md` 文件。

## 1. 版本准备

- [ ] 明确本次版本号与变更范围。
- [ ] 更新 `pyproject.toml` 的项目版本。
- [ ] 更新 `src/memleaf/__init__.py` 的 `__version__`。
- [ ] 更新唯一 Hermes Provider manifest 的版本：
  `src/memleaf/hermes_provider/plugin.yaml`
- [ ] 更新所有依赖具体版本号的测试。
- [ ] 在 `CHANGELOG.md` 顶部新增本版本段落。
- [ ] 同步 `README.md` 与 `README.en.md` 的当前版本信息。
- [ ] 不新增单独的版本发布说明 Markdown 文件。

## 2. 自动化验证

- [ ] Linux Python 3.11 / 3.12 / 3.13 完整 unittest 通过。
- [ ] Windows Python 3.11 / 3.12 / 3.13 安装与集成验收通过。
- [ ] macOS Python 3.11 / 3.13 Hermes/Codex 宿主与记忆语义验收通过。
- [ ] Windows 真实 Provider → `memleaf-mcp` stdio 回归通过：
  `stats`、`scope_catalog`、user/assistant capture、inbox 落盘。
- [ ] Windows 与 macOS 使用当前受支持 Codex CLI 做真实 `codex mcp add/get` 验收，
  Unicode/空格路径正常，重复安装为 `already_configured` 且 `changed=false`。
- [ ] Codex `PreToolUse` 参数重写符合当前 Hook 合同：
  `permissionDecision=allow` 与 `updatedInput` 同时存在，当前轮 `retrieval_id` 不丢失。
- [ ] Codex → Codex、Hermes → Codex、Codex → Hermes 三条共享 Vault 持久记忆闭环全部通过。
- [ ] Scope Map 仍只注入 Scope/alias/检索协议；`search` 候选严格只返回
  `memory_id + title`，`read` 才读取选中正文，检索预算与 retrieval gate 保持。
- [ ] CREATE / UPDATE / NO_CHANGE、history、去重、process 继续由 Core 统一实现，
  Codex/Hermes Adapter 不复制记忆语义。
- [ ] wheel 与 sdist 构建通过。
- [ ] 从 wheel 安装后 `memleaf` / `memleaf-mcp` 入口正常。
- [ ] 从 sdist 解包后的测试通过。
- [ ] PowerShell 安装器语法验证通过。
- [ ] `git diff --check` / compileall 等静态检查无异常。

## 3. Hermes 真实环境重点

- [ ] Hermes 实际 Python 环境加载的是目标 memleaf 版本。
- [ ] Hermes `plugins/memleaf` Provider 副本也是目标版本。
- [ ] MemoryProvider 可正常加载；`registered (0 tools)` 属于预期。
- [ ] 独立 memleaf MCP 注册 11 个工具。
- [ ] `stats` / `scope_catalog` 无 OSError、WinError 10093、TimeoutError。
- [ ] 可见轮次能 capture 到 inbox。
- [ ] process 能将有长期价值的内容提炼到 knowledge。
- [ ] UPDATE 能复用 active memory ID，并将旧版归档到 history。
- [ ] 新 Hermes 会话能召回旧会话中的已保存信息。

## 4. 安全与兼容

- [ ] Hermes 脱敏 credential（如 `***`、头尾掩码）不会被当成真实 API key。
- [ ] 日志与状态输出不泄露真实 API key。
- [ ] Windows 路径仍支持 `%LOCALAPPDATA%\hermes` 及官方 launcher 布局。
- [ ] 从旧版本升级时保留已有 Hermes `memleaf.json` 中的自定义 Vault，不静默切换到默认 Vault。
- [ ] Provider 的 Windows stdio 实现不重新引入 `select.select(pipe)`。
- [ ] 普通升级/`memleaf install` 不自动配置 Codex；只有显式
  `memleaf install --host codex` 才修改 Codex memleaf 接入。
- [ ] Codex 安装前后的 model/model_provider/base_url、sandbox、approval、
  profiles、已有 MCP 与已有 Hooks 保持；DeepSeek/OpenRouter/自定义 provider 不被改写。
- [ ] Hermes/Codex 已有 Vault 唯一一致时复用；冲突时 `vault_conflict` fail closed。
- [ ] Codex 自动提炼只使用独立 memleaf Model Route；没有 Route 时明确返回
  `processing_status=model_route_required`，不复制 Codex credential、不隐式消耗 Codex 会话模型。
- [ ] shell、Python 与 PowerShell 安装路径都使用
  `src/memleaf/hermes_provider` 这一份权威 Provider 源码。

## 5. 正式发布

正式 release commit 的 subject 必须严格为：

```text
release: vX.Y.Z
```

然后：

- [ ] push 到 `main`。
- [ ] CI 全部通过。
- [ ] CI 从 `pyproject.toml` 读取版本，并确认 commit subject 与版本一致。
- [ ] CI 从 `CHANGELOG.md` 提取本版本段落创建 GitHub Release。
- [ ] GitHub Release tag、标题、wheel、sdist、SHA256SUMS 均对应目标版本。
- [ ] `Publish to PyPI` workflow 使用 Trusted Publishing / OIDC 成功。
- [ ] PyPI 页面显示目标版本。
- [ ] 安装命令实际可升级到目标版本。

## 6. 发布后

- [ ] 检查 GitHub Release 显示标题、tag 与正文版本一致。
- [ ] 检查 PyPI 与 GitHub Release 的版本一致。
- [ ] 如有真实宿主验收结果，更新 `MAINTAINER_HANDOFF.md` 中的维护证据或注意事项。
- [ ] 不修改或覆盖已发布的 PyPI 版本；有修复时递增新版本。
