# 工作日志

## 2026-06-04

- 按用户要求对文档体系做严肃重构，未改核心 Python 执行逻辑，未派出子 agent。
- 将根 `README.md` 改为场景入口：离线演示、LoongArch 正式部署、TUI/CLI、DeepSeek key、权限快速判断和比赛交付建议分开说明。
- 重写 `docs/deployment/loongarch.md`、`docs/deployment/web.md`、`docs/deployment/permissions.md`，明确 `/opt/kyagent`、`/etc/kyagent/env`、`sudo -u kyagent`、sudoers 和审计目录分别解决什么问题。
- 重写 `docs/kyagent/README.md`，改为赛题贴合与系统架构说明，突出 Tool 到 Linux argv、Guardrail、ExecutionProxy、Web/TUI、审计链路和交付形式。
- 更新 `docs/status/current.md`，同步新的文档布局、部署入口和权限边界。
- 新增 `scripts/write-prod-env.sh` 和 `scripts/kyagent.sh prod-env`，用于单独重写最低生产启动配置，避免调试 key/env 时反复跑完整安装器。

## 2026-06-01

- 按 A2 赛题和 LoongArch64 Linux 部署目标完成安全修复：项目密钥文件进入 `.gitignore`，`kyagent.json` 从 Git 跟踪移除，运行时不再从项目 JSON 读取 key。
- 收紧工具输入、执行器 wrapper、环境变量和工具输出信任边界；只读工具结果落为 `PERCEPTION evidence_id`，结构化 RCA 通过 `submit_rca_report` 校验引用并写入 `DIAGNOSIS`。
- MCP stdio 服务补齐 JSON-RPC 校验、初始化生命周期、通知静默、插件显式 allowlist 和退出资源释放。
- Web 控制台加入角色 token、Origin 校验、DNS rebinding Host 约束、非回环监听 fail-closed、主体隔离会话、限界审核/选择队列和非阻塞 SSE。
- LoongArch 安装器补齐离线 wheelhouse、命令库存检查、Old/New World 判定、`dnf > yum > rpm` 适配和审计 HMAC key 文件生成。

## 2026-05-31

- 对 README、部署文档、部署脚本、sudoers、Web 启动链路和 LoongArch 依赖做完整复核；派出 4 个 `gpt-5.5 medium` 子 agent 分别审查文档、脚本、架构兼容性和测试覆盖。
- 将 `scripts/start-web.sh` 改为 Web 组合入口：启动后端、等待健康检查并自动打开浏览器；无桌面环境时继续运行并打印 URL。新增 `scripts/start-web-backend.sh` 与 `scripts/open-web.sh` 作为分开脚本。
- Web 默认监听从 `0.0.0.0` 收紧为 `127.0.0.1`；局域网暴露必须显式传参，后续在 2026-06-01 已升级为认证 fail-closed。
- LoongArch 安装器新增 Linux-only 闸门、`SKIP_CYTHON=1` 纯 Python pydantic 路径、独立 Web requirements、editable `--no-deps`、安全 env 序列化和自定义执行账户同步。
- sudoers 默认移除任意 systemd unit 重启/reload 权限；确需服务变更时通过 `KYAGENT_SERVICE_ALLOWLIST` 显式生成固定命令 allowlist。
- 重写根 README 为 LoongArch Linux 高层入口，部署细节继续下沉到 `docs/deployment/`。

- 新增 `scripts/kyagent.sh` 统一入口，抽象 `install / permissions / chat / tui / web / tools` 常用操作；安装与 Web 启动保持解耦。
- 修复 `setup-sudoers.sh` 在本地化 sudo 输出下把版本误判为 `unknown` 的问题：版本检查固定使用 `LC_ALL=C sudo -V`，无法识别时保留原始首行。
- 将根 `README.md` 收缩为上层导航，并新增 `docs/deployment/permissions.md` 与 `docs/deployment/web.md` 承载详细操作。

## 2026-05-29 10:17:06 +08:00

- 按用户要求对根目录文档做整体整理：根目录保留 `README.md` 和必要的 agent 指令文件，详细说明、LoongArch 审查、状态与日志迁入 `docs/`。
- 迁移旧根目录项目说明到 `docs/kyagent/README.md`，迁移 LoongArch 审查到 `docs/deployment/loongarch.md`，迁移工作日志和状态快照到 `docs/status/`。
- 重写根 `README.md` 为使用手册入口，包含一键安装脚本、LoongArch/Kylin 部署、LLM key 配置、启动方式、CLI 使用、配置文件和验收命令；架构内容只链接到 `docs/kyagent/architecture.md` 和 `docs/kyagent/safety-model.md`。
- 派出 2 个 gpt-5.5 medium 子agent协同梳理文档迁移方案和 README 使用命令大纲，并据此更新引用与测试路径。

## 2026-05-29 09:43:03 +08:00

- 按用户要求继续推进 TUI demo：派出多个 gpt-5.5 medium 子agent，分别负责设计文档、实施计划和测试切入点考察。
- 新增 `docs/superpowers/specs/2026-05-29-tui-shell-design.md`，记录 TUI 目标、非目标、LoongArch 默认依赖策略、安全不变量、MVP 功能和后续缺口。
- 新增 `kyagent/tui.py` 作为轻量 TUI 壳：提供可测试的确认渲染、工具表、trace timeline 摘要、`TuiSession` 状态对象和 `TuiApp` 交互循环；TUI 只复用 `Agent.from_config(confirm=...)`，不重写安全/执行/审计逻辑。
- 在 `kyagent/cli.py` 增加 `kyagent tui` 子命令，支持持续交互、`/tools`、`/audit`、`/reset`、`/exit`、确认面板和每轮 trace timeline 回放。
- 将 `prompt_toolkit>=3.0,<4` 加入 `pyproject.toml`、`requirements.txt`、`requirements-loongarch.txt`，并更新 `docs/deployment/loongarch.md`；LoongArch 默认 TUI 仍走 `prompt_toolkit + rich`，不引入 Textual/tree-sitter/Rust 扩展。
- 新增 `tests/test_tui.py`，覆盖 ConfirmRequest 默认拒绝提示、工具白名单视图、trace 摘要、TuiSession reset/last_trace 和 CLI 注册；同时扩展 LoongArch 依赖测试，锁定默认依赖包含 prompt_toolkit 且不包含 textual/tree-sitter。

## 2026-05-28 23:20:44 +08:00

- 按用户要求围绕 “Codex CLI / opencode / Claude Code 风格 TUI 前端壳” 做只读设计考察，重点检查 LoongArch/龙芯默认可运行路径。
- 派出 3 个并行子agent协同调查，均按 AGENTS 指示使用 gpt-5.5 medium，分别覆盖现有 CLI/Agent 集成点、LoongArch TUI 依赖风险、TUI MVP 与安全审计状态暴露。
- 结论：TUI 方案合理，但不应重写安全/执行/审计逻辑；应作为新通道层复用 `Agent.from_config(confirm=...)`、`ConfirmRequest`、`AgentRunResult`、AuditStore 与 ToolRegistry。
- 推荐默认技术路线为 `prompt_toolkit + rich`，保留 Typer 子命令和现有 `ask/json/mcp` 自动化入口；Textual 仅作为后续可选 extra，不进入 LoongArch Old World 默认部署路径，且不要启用 tree-sitter/syntax 相关依赖。
- 主要缺口：`Agent.ask()` 当前同步阻塞且没有事件回调/流式状态；TUI 如需展示工具调用阶段、确认弹窗、trace timeline，后续应抽出事件 sink 或 run state 回调，同时保持 CONFIRM 只由主交互循环处理。

## 2026-05-28 21:10:28 +08:00

- 按用户要求派出 3 个并行子agent协同调查仓库状态，分别覆盖赛题定位、代码结构、git/环境/产物状态；子agent均按要求使用 gpt-5.5 medium。
- 确认仓库对应 A2 赛题“面向麒麟操作系统的安全智能运维 Agent”，状态总结已贴合赛题 5 大要求：OS 感知、MCP 插件化、安全意图校验、最小权限执行、推理链路溯源。
- 检查 README、docs/kyagent 项目说明、部署说明、study 文档、核心源码、配置、测试和 benchmark 产物。
- 运行验证：首次 `python -m pytest -q -p no:cacheprovider` 受 Windows pytest 临时目录权限影响失败；改用 `python -m pytest -q --basetemp pytest_tmp_run -p no:cacheprovider` 后通过 `237 passed, 2 skipped`。
- 新增状态文档，记录当前仓库具体状态、赛题贴合度、测试结果、git 状态、产物、风险缺口和下一步优先事项。

## 2026-05-28 22:40:25 +08:00

- 按用户要求继续执行 LoongArch 长任务：基于 3 个子agent报告和实际 Web 核查，完成依赖/架构兼容审查、文档纠偏、一键部署脚本与多轮审核。
- 新增 `scripts/install-loongarch.sh`，默认 LoongArch Old World 路径使用 `deepseek_httpx`，不安装 openai/anthropic/mcp extra，支持 dry-run、系统包安装、Python 检测、venv 安装、sudoers/env/selfcheck。
- 强化 `scripts/setup-sudoers.sh`，改为临时 sudoers 先校验、安装后再校验、失败回滚，降低写坏 `/etc/sudoers.d/kyagent` 的风险。
- 重写 LoongArch 部署文档，同步 README、配置、study 文档和 HTML 学习页，移除已删除旧实现笔记的引用，统一测试数为 244 collected。
- 新增 `tests/test_loongarch_deploy_docs.py`，锁定部署脚本、依赖清单、文档一致性和 `openai_httpx/deepseek_httpx/qwen_httpx` 后端说明。
- 验证结果：`bash -n` 两个脚本通过；`install-loongarch.sh --dry-run` 通过；`tests/test_loongarch_deploy_docs.py` 5 passed；全量 `python -m pytest -q --basetemp pytest_tmp_verify -p no:cacheprovider` 为 `242 passed, 2 skipped`；collect-only 为 `244 tests collected`。

## 2026-05-30

- streaming TUI: README §4/§8 更新；install 脚本核对（若有改动）；rich/prompt_toolkit 依赖核对完成
- v2 TUI 上线：每条用户/agent 发言独立 Panel（绿框"你" / 蓝框"kyagent (backend)"），LLM reasoning 以 `dim italic grey50` 流式打印（turn 结束擦除），底部状态行实时显示 🧠 思考中 / 🔧 调用工具 / ✅ 完成 / ❌ 错误，`Ctrl+L` 清屏，新增 `ask_user_choice` 工具（黄框选项面板）。
- LLM 真流式：`HttpxBackend`（默认 `deepseek_httpx`）走 OpenAI SSE，`OpenAIBackend` 用 SDK `stream=True`，`MockBackend` 按空格切块，`AnthropicBackend` 走基类 fallback（避免 jiter Rust 编译，保护 LoongArch 路径）。
- 零新增依赖；`rich` + `prompt_toolkit` + `httpx` 仍是主依赖；271 tests passed。
- 2026-05-30 工具集大扩展 +73 → 92 个工具。新增 TrendTool / PkgFamilyMixin 基础设施。覆盖赛题 4 大场景：僵尸进程 / 磁盘 I/O / 配置漂移 / 大日志。KySec 工具命中麒麟加分项。LoongArch 专属域 3 工具。`tests/test_tools_expansion.py` 123 用例静态校验 build_argv + schema 拒绝路径，全量测试 394 passed / 2 skipped。
- FastAPI Web 控制台补齐 TUI 同级动态展示：用户消息、浅色 thinking 增量、红色工具调用、加粗 final、状态栏和人工审核卡片；新增 `ApprovalBroker`、approve/reject API 与 `approval_required / approval_resolved` SSE。
- 新增 `scripts/start-web.sh` 一键启动浏览器控制台；LoongArch 安装器增加可选 `--with-web`，默认最小依赖路径不变。README、完整项目说明、LoongArch 部署审查和状态文档同步更新。
