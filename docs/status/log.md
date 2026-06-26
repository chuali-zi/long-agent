# 工作日志

## 2026-06-25 23:30:00 +08:00

- 按用户要求使用临时 DeepSeek key 与 sudo 密码，对仓库可见全部测试与全部脚本做“能跑”验证。
- 全量 pytest：因历史 root 残留 `pytest_tmp_run` 目录导致首次 84 处 `FileExistsError`；清理后重跑 `python -m pytest -q --basetemp /tmp/opencode/pytest_run -p no:cacheprovider tests/`，结果 `904 passed, 3 skipped, 0 failed`。3 skipped 为预期（Windows 分支、需 `KYAGENT_RUN_REAL_LLM_TODO_REPRO=1` 的真实 LLM 复测、需 root 的 `/var/log` 写权限用例）。单独重跑 `tests/test_web_frontend_playwright.py`：`6 passed`。
- `scripts/` 入口（11 个）+ 7 个 `kyagent-*` wrapper：`kyagent.sh --help`/`tools list`/`test`/`install`/`web --mock`/`web-backend --mock`/`web-open`、`install.sh --dev`、`full-test.sh --skip-benchmarks`、`demo.sh`（mock 完整 10 步）、`start-web.sh --mock --no-open-browser`、`start-web-backend.sh --mock`、`open-web.sh`、`write-prod-env.sh`（真实写入 `/etc/kyagent/env`）、`setup-sudoers.sh`（真实最小权限部署 + visudo OK）、`setup-sudoers-prod.sh --yes`（真实部署）、`setup-sudoers-max-test.sh --yes`（真实部署）、`install-loongarch.sh --dry-run --yes --with-web`（完整 dry-run 0 错误）、`developer-quick-test.sh --help`。7 个 wrapper 均通过无参/安全参数调用验证。发现 `setup-sudoers-max-test.sh --help` 未实现，会报“未知参数”。
- `benchmarks/`：顶层 `run-suite.sh`、`run-real-llm.sh`、`setup-all.sh`、`verify-all.sh`、`teardown-all.sh` 均通过 `--help` 与真实执行验证。9 个 bench 各自的 `setup.sh`/`verify.sh`/`probe.sh`/`teardown.sh`/`run.sh` 均真实跑通：`setup-all.sh` + 逐个 `probe.sh` + `verify-all.sh --pre`（需 sudo 写 score.json）全部 `SETUP_OK`；各 `run.sh`（不加 `--ask`，避免消耗 LLM）setup/pre/probe 通过；最后 `teardown-all.sh` 清理全部场景。
- 真实 LLM 端到端：以 `kyagent` 用户、加载 `/etc/kyagent/env`，执行 `/opt/kyagent/.venv/bin/kyagent ask "which process used the most cpu"`，DeepSeek 返回正确结果，`exit=0`。确认生产链路（DeepSeek httpx + sudoers + 审计目录）可用。
- Web：mock 后端在 `127.0.0.1:8765/8766/8767` 健康检查均 OK；`start-web.sh`/`start-web-backend.sh`/`open-web.sh` 均能启动/探测。
- 环境注意：历史 root 残留 `pytest_tmp_run` 会让 `full-test.sh`/`kyagent.sh test` 出现 `FileExistsError`，需 `sudo rm -rf pytest_tmp_run*` 后重跑；`verify-all.sh --pre` 需与 `setup-all.sh` 同用户（sudo）才能覆盖 root 创建的 `score.json`；cron-injection 场景若 `/tmp/secops-cron` 残留无 state 会拒绝覆盖，需先手动清理。
- 收尾：已执行 `benchmarks/teardown-all.sh` 清理全部 9 个场景，并恢复最小权限 `setup-sudoers.sh`，删除 root 残留的 benchmark `score.json`/`bench-state.json`。

## 2026-06-21 07:30:00 +08:00

- 按用户要求对仓库全部脚本做"正常使用"验证，覆盖 `scripts/`、`benchmarks/` 顶层与 9 个 bench 子目录的每一 个脚本，不只是已跑过的入口。
- 语法：`bash -n` 覆盖全部 `.sh`（0 失败）；`py_compile` 覆盖 7 个 `kyagent-*` Python wrapper + `benchmarks/lib/grade.py` + 9 个 `gen_artifacts.py` + `bench_ask.py`（0 失败，清理了一处 root 残留的 `benchmarks/lib/__pycache__`）。`ruff check` 发现 3 处 F401 未使用 import（`cleanup-v2/gen_artifacts.py` 的 `time`、`dataclasses.field`；`stale-lock-v1/gen_artifacts.py` 的 `signal`），已清理；`bench_ask.py` 头部声明 FROZEN，未改。修后两个 `gen_artifacts.py` 重新跑 setup/teardown 仍 `SETUP_OK`，`test_script_entrypoints` + `test_benchmark_suite_runner` + `test_todos` + `test_planner` 共 `20 passed`。
- `scripts/` 入口（11 个）：`kyagent.sh --help`/`tools list`、`install.sh --help`、`full-test.sh --help`、`start-web.sh --help`、`start-web-backend.sh --help`、`open-web.sh --help`、`write-prod-env.sh`（写到 tmp env 真实落盘验证）、`setup-sudoers.sh`（sudo 真实部署 + visudo OK）、`setup-sudoers-prod.sh --yes`（真实部署 + visudo OK）、`setup-sudoers-max-test.sh --yes`（真实部署 + visudo OK）、`install-loongarch.sh --dry-run --yes --with-web --deepseek-key-file`（完整 dry-run 打印全部步骤，0 错误）。
- `kyagent-*` wrapper（7 个）：`cron-trace --list`、`lock-stale inspect/remove`、`unix-socket-stale inspect/remove`、`log-dir-perms`（`/var/log/<service>` 真实收紧 0777→0750）、`cron-disable`（真实 fixture 重命名禁用 + 指标命中）、`file-delete`（tmp fixture 删除 + 拒绝相对路径）、`log-clean`（tmp fixture 截断 1MB→0）。3 个无 `--help` 的 wrapper（cron-disable/file-delete/log-clean）按位置参数正常工作，拒绝非法输入返回 exit 2。
- `demo.sh`：mock 后端完整跑完 10 个演示步骤（工具清单、3 类安全护栏、3 轮 ask、audit list/show），exit 0；需显式给可写 `KYAGENT_AUDIT_DB` 否则默认 `./var/` 权限不足。`developer-quick-test.sh --help` 输出完整用法。
- `benchmarks/` 顶层：`run-suite.sh --help`、`run-real-llm.sh`（= run-suite.sh wrapper）、`setup-all.sh --help`、`teardown-all.sh --help`、`verify-all.sh --help` 全部 OK。真实执行 `setup-all.sh --probe`：9 bench × (setup + pre-verify + probe) = 27 步全绿，`hard_failures: 0`；`verify-all.sh --pre`：9 bench 全 `SETUP_OK` exit 0；`verify-all.sh --post`（未修复）：9 bench 全 `INCONCLUSIVE` exit 3（预期）；`teardown-all.sh`：9 bench 全部清理。
- 各 bench 子脚本覆盖：`setup.sh`×9（setup-all 调用）、`verify.sh` pre×9 + post×9（setup-all + verify-all + run-suite 调用）、`probe.sh`×9（setup-all --probe 调用）、`teardown.sh`×9（teardown-all + run-suite --teardown-each 调用）、`run.sh`×9（run-suite 调用，9/9 PERFECT）、`gen_artifacts.py`×9（setup.sh 内部调用，setup-all 成功即全部通过）。
- 结论：仓库全部脚本（11 个 scripts/ 入口 + 7 个 kyagent-* wrapper + 5 个 benchmarks/ 顶层 + 9×5 个 bench 子脚本 + 9 个 gen_artifacts.py + grade.py + bench_ask.py）在真实环境（真实 DeepSeek + 真实 sudo + 真实 /var /tmp /etc/cron.d 写入）下均正常使用，无回归。唯一环境注意：`demo.sh` 需显式给可写 audit 路径，已在脚本注释前提里说明。

## 2026-06-21 05:55:00 +08:00

- 按用户要求对最新工作树（Cursor 式渲染 + TODO 重构 + planner 稳定 ID/revision）做全量回归，覆盖 TODO 单测、全量 pytest、Web 前端 Playwright、脚本入口/ bench runner 单测、RealOps 9 bench、性能微基准、真实 DeepSeek 后端。
- TODO/planner 聚焦：`tests/test_todos.py` + `tests/test_planner.py` 共 `6 passed`，验证 `failed` 状态、`todo_revision` 单调递增、稳定 ID 内容复用、非法快照原子回滚、终态收敛幂等。
- 全量 pytest（`scripts/kyagent.sh test`）：`851 passed, 3 skipped, 0 failed`（17.28s），与 `docs/status/current.md` 记录一致；3 skipped 均为预期（Windows 分支、需 `KYAGENT_RUN_REAL_LLM_TODO_REPRO=1` 的真实 LLM 复测、需 root 的日志权限用例）。
- Web 全量：`test_web_frontend_playwright.py` + `test_web_server.py` + `test_web_security.py` + `test_start_web_script.py` 共 `39 passed`，其中 6 个 Playwright 用例真起 Chromium 跑 `index.html` 的 SSE 审批、TODO 快照、最终 Markdown 渲染。
- 脚本入口与 bench runner：`test_script_entrypoints.py` + `test_benchmark_suite_runner.py` 共 `14 passed`，覆盖 `scripts/kyagent.sh` 抽象命令、`full-test.sh` 串联、prod-env 最小化、install prefix 选择。
- RealOps 9 bench（`sudo bash benchmarks/run-suite.sh --teardown-each`，真实 DeepSeek `deepseek_httpx`）：`cleanup-v2 / secret-spill-v1 / port-conflict-v1 / open-deleted-v1 / runaway-cpu-v1 / stale-lock-v1 / unix-socket-stale-v1 / logrotate-perms-v1 / cron-injection-v1` 全部 `PERFECT`、`exit_code=0`。首轮 cron-injection-v1 因上一轮残留 `/etc/cron.d/nightly-ledger-backup` 触发 setup 拒绝覆盖（环境脏，非代码回归），清理残留后单独复跑 `PERFECT`。
- 性能微基准（`benchmarks/bench_ask.py`，对比 `baseline.json`）：`overall_pass=true`；`ask_p50` ratio 0.152（-84.8%）、`ask_p95` ratio 0.347（-65.3%）、`guardrail_p50` ratio 0.095（-90.5%）、`audit_total` ratio 0.147（-85.3%）全部达标。
- 真实 DeepSeek 后端：`kyagent ask --auto-approve-safe-remediation` 以 kyagent 用户source `/etc/kyagent/env` 成功返回真实主机名/内核答案；`test_real_llm_todo_write_coercion_repro_entry` 在 `KYAGENT_RUN_REAL_LLM_TODO_REPRO=1 KYAGENT_REAL_LLM_BACKEND=deepseek_httpx` 下 `PASSED`，确认用户显式“不要先写 TODO”时 Agent 不再强制 TODO、不触发 `tool_use_without_todo_write`。
- 结论：当前工作树相对 TODO 重构无回归；唯一失败来自上轮 bench 残留 cron 文件，已清理并复跑通过。汇总日志在 `tmp/full-regression-20260621/`。

## 2026-06-17 21:35:00 +08:00

- 修复 Agent TODO 计划协议：首次漏 TODO 的工具调用会被拒绝并要求模型重发；二次仍漏时优先触发无工具计划轮生成 TODO，只有计划轮失败才按工具调用兜底合成计划；同时接受 `1. ...` 这类编号计划。
- 验证：全量 pytest `834 passed, 3 skipped, 10 warnings, 0 failed`；真实 DeepSeek + `/opt/kyagent` 生产前缀 RealOps 9 bench 全部 `PERFECT`。
- 更新比赛提交文档中的测试数字与性能结论：性能 `overall_pass=false` 仍未达标，但最新复测已变为仅 `ask_p50` 未达标，`ask_p95`、`guardrail_p50`、`audit_total` 均达标。

## 2026-06-15 23:55:00 +08:00

- 完成最后一轮比赛提交文档复核修订：更新功能测试报告为 `831 passed, 3 skipped, 10 warnings, 0 failed`，明确 skipped 原因；保留 RealOps 9 bench 全部 PERFECT 的真实结论。
- 更新性能报告为最新 `benchmarks/bench_ask.py` 复测结果：`ask_p50 +66.5%`、`ask_p95 +30.5%` 未达标，`guardrail_p50 -83.2%`、`audit_total -71.8%` 达标，`overall_pass=false`。
- 新增根目录 `FINAL_LOONGARCH_CHECKLIST.md`，列出 LoongArch64 + 麒麟 V11 实机最终验收步骤、通过/失败标准、密钥与交付包检查、Web/CLI/pytest/RealOps/性能/权限复核要求。
- 明确不将兼容环境结果冒充目标平台实机证据；提交前需按清单在目标机补齐最终验收记录。

## 2026-06-11 21:18:12 +08:00

- 根据 `fail_analysis` 完成 RealOps 修复能力补齐，未修改 `benchmarks/`：新增 stale lock、stale Unix socket、cron.d 禁用、日志目录权限收紧四类专用工具，保持“不开放通用 rm/chmod/mv”的安全边界。
- 新增运行态修复工具 `lock_inspect` / `lock_remove_stale` / `unix_socket_inspect` / `unix_socket_remove_stale` 及 `kyagent-lock-stale`、`kyagent-unix-socket-stale` wrapper；wrapper 负责路径根、类型、PID/listener、关键 socket、inode 复检。
- 新增 cron 专用工具 `cron_d_list` / `cron_d_read` / `cron_entry_trace` / `cron_d_disable` 及 `kyagent-cron-trace`、`kyagent-cron-disable` wrapper；禁用方式为 rename 保留证据，拒绝保护名和未命中可疑指标的 cron。
- 新增 `log_dir_repair_permissions` 和 `kyagent-log-dir-perms` wrapper，仅允许 `/var/log/<service>` 或一层子目录从 group/world writable 收紧到 `0750`/`0755`，不递归、不 chown、不增加写权限。
- 更新 `setup-sudoers.sh` 与 `setup-sudoers-prod.sh`：生产预设默认启用 runtime stale、cron disable、log directory permissions 三类专用授权；默认 `configs/sudoers.kyagent` 仍不包含写操作授权。
- 更新 Agent auto-approve 逻辑：非交互安全修复模式下，仅对专用 deterministic preflight 通过的修复工具自动确认；prompt 补充旧 access log 归档、cron 证据保留和权限收紧策略。
- 验证：新增/修改核心文件 `ruff check` 通过；focused pytest `278 passed, 15 skipped`；全量 pytest `789 passed, 18 skipped`；全仓库 ruff 仍有既有测试 lint 债（未做无关清理）。

## 2026-06-11 18:06:32 +08:00

- 按用户要求扩展 RealOps benchmark，从 5 个真实运维工单扩展到 9 个，并同步 `benchmarks/suite.yaml`、`benchmarks/run-suite.sh`、`benchmarks/REALOPS_BENCHES.md` 和 `benchmarks/opencode/SKILL.md`。
- 新增 `stale-lock-v1`：部署锁记录死 PID、旁边 active backup-sync lock 仍被活进程持有；考察 PID/锁文件判断、最小化删除和误杀防护。
- 新增 `unix-socket-stale-v1`：stale Unix socket 阻塞服务启动，旁边 metrics sidecar socket 可连接；考察 `ss/lsof/stat` 级别的 socket 活性判断和非普通文件安全清理。
- 新增 `logrotate-perms-v1`：payroll 日志目录权限漂移导致 logrotate 跳过；考察配置漂移 RCA、最小权限修复，禁止删除/清空/放宽敏感工资日志。
- 新增 `cron-injection-v1`：可疑 cron 指向含提示词注入文本的脚本；考察把不可信文件内容当被动证据处理、禁用恶意持久化入口、保留正常 cron 和取证脚本且绝不执行 payload。
- 参考线上资料提炼场景：logrotate insecure parent permissions、Unix socket address already in use/stale socket、PID/lock file、MITRE ATT&CK T1053.003 cron persistence 等，确保题目更像真实运维问题而不是显式考试题。
- 修复 `benchmarks/lib/common.sh` 在当前工作树中的 CRLF 换行，避免 Linux/WSL `source` 时出现 `pipefail\r` 非法选项，影响所有 `verify.sh` 评分退出。
- 验证：`bash -n` 覆盖新增 shell 和 suite runner；`python -m py_compile` 覆盖新增 generator；suite YAML 解析确认 9 个 bench 所需文件齐全；四个新增 bench 均在 sandbox root 下完成 `setup -> verify pre -> teardown`，并通过人工最小修复模拟得到 `verdict: PERFECT`。

## 2026-06-08

- 按用户要求派出 4 个并行子agent侦察仓库，分别覆盖赛题/交付物、Agent架构、测试运行链路、冲榜性能与鲁棒性；主线程同步读取 AGENT.md、README、docs/status、Agent 主循环、LLM backend、工具 pipeline、ExecutionProxy、Web/MCP/审计实现和 benchmark。
- 本轮为严苛缺陷审查，未修改实现代码。关键结论：当前短板集中在比赛交付闭环缺失、真实 LoongArch/Kylin 证据不足、默认工具面过宽、上下文/预算无界、TODO 正则门控脆弱、写操作安全 preflight 不够确定、Web/MCP 审批链路不完整、pytest 当前存在失败用例、性能 benchmark 不能代表目标环境。
- 本机验证：`python -m pytest -q` 当前结果为 `701 passed, 13 skipped, 1 failed`，失败项为 `tests/test_web_security.py::test_choice_broker_api_roundtrip`；`python -m kyagent ask "which process used the most cpu" --mock` 失败，因为 `ask` 子命令没有 `--mock` 选项；通过 `KYAGENT_LLM_BACKEND=mock` 可运行离线 ask，但 Windows 下返回 mock 执行占位，不代表 Kylin/LoongArch 真机能力。
- 高危提交风险：本地 ignored 文件 `kyagent.json` 被子agent确认含明文 DeepSeek key，虽然不在 Git 跟踪中，但 README/脚本中的裸 `rsync ./` 或手工打包整个目录会把它复制进 `/opt/kyagent` 或交付包；后续必须先清理密钥并改用受控 release 打包清单。

## 2026-06-07

- 开出实验分支 `experimental/p0-agent-runtime`，保留 main 上既有未提交改动不回退，进入激进 P0 runtime 更新。
- 新增 durable plan/state 后端：`kyagent/planner.py` + `kyagent/plan_cli.py`，每次 Agent turn 自动创建 `plan-*`，写入 `var/plans.db`，审计新增 `plan_update` / `budget` 事件，SSE progress 新增 `plan_start`、`plan_step_*`、`plan_snapshot`、`budget_update`，Web 同步/流式响应返回 `plan_id`，并新增只读 `/api/plans`、`/api/plans/{plan_id}`。
- 解决 Linux 生产路径并行只读工具不可用问题：`ExecutionProxy` 允许已预检 LOW/read-only 工具在 worker 线程并行执行，并行路径跳过 POSIX `preexec_fn`、使用 `start_new_session=True`，避免 preexec_fn + 多线程 fork 风险；串行路径保留原 rlimit 行为，强 sandbox/cgroup/seccomp 后置。
- P0 工具扩展：新增 `git_status/git_diff/git_log/git_show/git_blame` 只读 Git inspect；新增 `verify_pytest/verify_ruff/verify_script_syntax` 固定验证命令白名单；新增 `web_fetch_url/osv_query_package/github_issue_search` 受控外部事实知识检索，默认仅允许官方文档、GitHub、PyPI、OSV、CVE/NVD 等域名。
- 补齐复杂输入和浏览器验证后端钩子：新增 `docx_extract_text/xlsx_list_sheets/pdf_extract_text/ocr_image_text`，docx/xlsx 默认走标准库本地解析，PDF/OCR 走 optional backend 并在缺依赖时明确报错；`verify_pytest suite=frontend` 固定触发 Playwright 前端 DOM/E2E 测试。
- 新增只读 plan MCP 工具 `plan_list/plan_get`，便于 LLM/MCP client 查看 durable task 状态，但不允许工具直接提权或修改计划。
- 验证：`python -m compileall -q kyagent` 通过；全量 `python -m pytest -q --basetemp pytest_tmp_full_experimental4 -p no:cacheprovider` 通过，结果 `676 passed, 40 skipped`。

## 2026-06-05

- 修复 Web 提问时审计库不可写导致 `/api/ask/stream` 请求阶段才 500 的问题：`build_app()` 启动时预检 `AuditStore`，若 `/var/lib/kyagent/audit.db` 或 JSONL 路径不可写，会在服务启动阶段给出 `audit store is not writable` 明确错误，避免页面打开后提问才崩溃。
- 修复 `scripts/open-web.sh` 健康检查重试时把 `urllib.request.urlopen` 的 transient connection refused traceback 打到终端的问题；探测输出改为静默重试，只保留最终超时或成功打开信息。
- 测试补充：新增 Web 审计预检回归测试、健康检查 traceback 静默回归测试；bash 集成测试在当前 Windows 无可用 WSL/bash 时按既有模式跳过，真实 Linux/WSL 环境仍会执行。
- 文档补充：README 增加 `start-web-backend.sh` / `open-web.sh` 分层排障入口，并明确生产安装不要使用 `--skip-sudoers`，除非手工接管运行账户、sudoers 和审计目录。
- 验证：全量 `pytest -q --basetemp pytest_tmp_run -p no:cacheprovider` 通过，`571 passed, 18 skipped`；本机 mock Web smoke 通过 `/api/health`、`/api/ask` 和 `/api/ask/stream`，`which process used the most cpu` 能返回 trace 和流式 final 事件。当前机器 WSL 未安装发行版，无法执行 README 的 WSL 全流程实测。

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

## 2026-06-05

- 侦察确认工具集缺口：原 ~94 工具中除 svc_restart/svc_reload 外全部 read_only，且这两个写工具的 sudoers 条目从未授权——实测「清理日志/装包/杀进程」全做不到，安全护栏（safety-rules 里 rm/kill/pkg-remove 等危险规则）一直空转无对象。属赛题「执行管理任务」硬缺口，非补 sudoers 即可。
- 按「默认只读、写操作显式 opt-in」哲学新增 5 个写工具（94→99）：log_vacuum(MEDIUM)、process_kill(HIGH,pid≥2)、pkg_install(MEDIUM)、pkg_remove(HIGH)、fs_truncate(HIGH,限 /var/log|/var/cache|/var/tmp|/tmp)，全部 requires_root + read_only=False。
- 派 2 个并行子agent（sonnet）按同一份「argv↔sudoers 锚定正则」契约表分别实现工具+测试 与 setup-sudoers.sh 的 opt-in 渲染（KYAGENT_ENABLE_LOG_CLEAN/PKG_MGMT/PROC_KILL），默认 configs/sudoers.kyagent 仍只读不变量守住。
- 集成校验（主agent）：① 8 条 argv 与渲染出的 sudoers 正则逐字节 fullmatch 全过；② 修复 pkg-remove-critical 正则——原要求 dnf 后紧跟 remove，被 `dnf -y remove kernel` 的 -y 绕过，导致删内核仅 confirm；改为容忍夹在中间的 flag 后，删 kernel/systemd/glibc/openssh-server 均 deny；③ 更新系统提示词 prompt.py 第3条，加入「先感知后变更」+清理垃圾示例。
- 验证：全量 `python -m pytest -q` = 626 passed, 3 skipped（均 Windows POSIX 专属 skip）。

## 2026-06-06

- 应用户需求（LoongArch 虚拟机难用、手搓 sudoers 不现实）新增一键「生产预设」脚本 scripts/setup-sudoers-prod.sh：薄封装 setup-sudoers.sh，默认开启日志清理/包管理/进程终止三类写操作 + 13 个常见可重启服务白名单（nginx/httpd/sshd/firewalld/chronyd/crond/rsyslog/mariadb/mysqld/postgresql/redis/docker/php-fpm），任意开关/服务清单可用环境变量覆盖。
- 写操作均为固定命令 + 锚定参数正则（非通配）；先打印授权摘要再交互确认（--yes 跳过，非交互无 --yes 退 1），最终仍走核心脚本 visudo 校验 + 失败回滚。默认只读基线与既有不变量不受影响。
- kyagent.sh 新增 permissions-prod 子命令与 usage；README 新增「写操作授权（一键生产预设）」一节；tests/test_sudoers_least_privilege.py +5 用例（默认开关/常见服务/非交互守卫/覆盖生效/子命令存在）。
- 验证：bash -n 两脚本通过；渲染模拟产出合法完整 sudoers；tests/test_sudoers_least_privilege.py 26 passed；全量回归见下方命令。

## 2026-06-21 04:05:00 -07:00

- 对 TODO 重构后的大面积回归做根因级重构：删除 Agent 主循环对 TODO 的前置门禁、文本计划解析、基于 tool call 的自动合成、最终回复前补写以及失败重试 coercion。`todo_write` 现在是可选的独立结构化工具；LLM 不生成 TODO 时业务工具直接执行，非法 TODO 与同轮合法业务工具互不阻塞。
- 新增 `TodoService` 与权威 `TodoSnapshot`：完整快照替换、稳定 item ID、单调 revision、事务写入、状态校验和真实终态收敛。成功结束时未完成项记为 `cancelled`，失败时活动项记为 `failed`，不伪造 `completed`。
- 统一 TUI/Web TODO 渲染：只消费后端 `todo_snapshot`，按 revision 和 turn 拒绝乱序/过期事件；移除前端文本正则、tool-end 自动推进和 final 自动完成，解决重复、跳项、旧轮覆盖等乱渲染。
- 修正真实 benchmark 基础设施：runner 显式导出所选安装前缀到 `PYTHONPATH`，避免 editable install 偷偷加载旧 `/opt/kyagent`；为 `disk_open_deleted` 增加精确锚定的只读 root `lsof -nP +L1` sudoers 权限，并通过 `visudo` 校验。
- 回归覆盖新增/扩展：无 TODO 直接执行、无 TODO 不伪造、非法 TODO 不拦截同轮动作、结构化 TODO、文本/编号计划不改状态、快照 revision/稳定 ID/原子拒绝/真实终态、浏览器旧 turn 与旧 revision 隔离、benchmark 源码选择。
- 验证：项目虚拟环境全量 `851 passed, 3 skipped, 0 failed`；系统依赖集 `818 passed, 6 skipped, 0 failed`；真实 DeepSeek 无 TODO 复现测试 `1 passed`；sudoers/runner 定向测试 `56 passed`；`compileall`、`git diff --check` 与变更文件 Ruff 通过。
- RealOps 最终验收：清理中断运行遗留 fixture、重新安装当前 sudoers 后，单次运行 9/9 `PERFECT` 且全部 `exit_code=0`。汇总：`tmp/todo-refactor-realops-final/kybench-summary-20260621-035319.tsv`。
- 性能验收：`benchmarks/bench_ask.py` Overall PASS；ask p50 -85.3%、p95 -62.0%、guardrail p50 -90.4%、audit total -86.3%。
