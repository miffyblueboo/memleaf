# v0.1 发布检查

检查日期：2026-08-28。发布版本：`0.1.0`，GitHub 标签 `v0.1`。

本文件记录发布前检查快照；实际发布状态和远端测试结果以 GitHub Release / Actions 为准。

## 发布范围

- 仅支持 Hermes：原生 MemoryProvider 与主动 MCP 是两条独立链路。
- 安装、`init --all` 和默认模型发现均不接入 Codex/反重力；不卸载已有接入。
- 默认源码目录 `$HOME/memleaf`，数据目录 `$HOME/.memleaf`。
- 内部历史兼容代码保留，不代表本版承诺支持相应宿主。

## 已完成的本地检查

| 项目 | 结果 |
| --- | --- |
| Hermes-only 安装、初始化、模型发现和 MCP 定向测试 | 59 项通过 |
| 完整源码测试（Python 3.11） | 373 项通过 |
| 解包后的 source distribution 完整测试 | 373 项通过 |
| 隔离安装 | 含空格路径、无 pip/setuptools、重复安装、缺少模型失败、其他宿主配置不变等回归通过 |
| wheel 安装 | 全新虚拟环境离线安装；import 来自 site-packages；两个 CLI 版本一致 |
| 构建与静态检查 | wheel/sdist 构建、compileall、bash 语法、Git diff 空白检查通过 |
| 发行内容 | 包含安装脚本、两份 README、Hermes 插件、测试与计划文档；无第三方运行时依赖 |
| 隐私检查 | 本地验收/历史清理报告被排除；源码包未发现个人绝对路径、真实会话标识及已知业务名称；测试使用合成数据 |

安装测试使用隔离 HOME 和模拟 Hermes CLI；MCP 测试包含真实 stdio 子进程，但这些不等于真实 Hermes 对话/真实模型验收。隐私检查也不等于完整安全审计。

## 发布前仍需完成

- [ ] 最终候选版真实 Hermes 会话验收：自动绑定当前轮 `retrieval_id`；FOUND 后 read 预算入账；随机无匹配返回 NO_MATCH；纯查询不写知识；自然状态更新复用 ID 并保留 history；MCP 首次与闲置回收后调用成功。不得使用文件工具兜底冒充 MCP 成功。
- [x] 确认发布仓库为公开的 `miffyblueboo/memleaf`，当前 GitHub 插件账号具有管理与推送权限。旧 `Ahhry/memleaf` 地址不再用于安装。
- [x] 用户已明确授权正式发布 v0.1，并要求预告 v0.2 支持 Codex、后续逐步支持更多 Agent。
- [ ] 远端 GitHub Actions 的 Python 3.11/3.12/3.13 与构建任务通过后，由本次发布提交触发 Release；CI 配置本身不算通过证据。
- [ ] 对实际发布的 commit/tag 做干净 clone 安装并核对发行附件。

结论：本地自动化与打包检查通过，按用户授权发布；不能将其表述为真实 Hermes 场景全部验收通过。发布说明明确保留该验证边界。本次发布不覆盖现有运行目录或修改真实 Vault。
