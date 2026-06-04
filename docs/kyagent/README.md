# kyagent 完整项目说明

kyagent 是 A2 赛题“面向麒麟操作系统的安全智能运维 Agent”的实现。它把自然语言、MCP Tools、Linux 运维命令、安全护栏、最小权限执行和审计追踪串成一条可解释闭环。

根 README 负责“怎么跑起来”；本文件负责“这个系统是什么、为什么符合赛题”。

## 赛题要求对应关系

| 赛题要求 | 本项目落地 | 关键位置 |
| --- | --- | --- |
| OS 环境深度感知 | process、network、logs、service、filesystem、package、disk、system、security、compliance、loongarch 工具域 | `kyagent/mcp/tools/` |
| MCP 运维插件化 | 自研 MCP stdio server + 内置 ToolRegistry + 显式插件 allowlist | `kyagent/mcp/server.py`、`kyagent/mcp/plugins.py` |
| 安全意图校验器 | 自然语言意图过滤 + argv Guardrail 二次过滤 | `kyagent/safety/` |
| 最小权限代理执行 | `kyagent` 受限账户 + ExecutionProxy + sudoers 精确白名单 | `kyagent/executor/`、`configs/sudoers.kyagent` |
| 推理链路溯源 | USER_INPUT -> INTENT_CHECK -> TOOL_REQUEST -> SAFETY_CHECK -> EXECUTION_RESULT -> AGENT_REPLY | `kyagent/audit/` |
| B/S 架构 | FastAPI Web 控制台 + SSE 流式事件 + 浏览器审核卡片 | `kyagent/web/` |

## 一句话架构

```text
用户自然语言
  -> Agent.ask()
  -> LLM 选择 tool
  -> Tool.validate/build_argv()
  -> Guardrail 检查 argv
  -> ExecutionProxy 执行 Linux 命令
  -> Tool.format_result()
  -> AuditLogger 记录完整 trace
  -> Agent 回复用户
```

Tool 不是让 LLM 自由写 shell。Tool 是受控模板：LLM 只能选择工具和参数，工具自己把参数转换成 argv 列表，例如：

```text
sys_memory       -> ["free", "-h"]
sys_kernel       -> ["uname", "-a"]
process_list     -> ["ps", ...]
sys_dmi          -> ["dmidecode", "-s", "system-product-name"]
svc_restart      -> ["systemctl", "restart", "..."]
```

所有落地执行都走 `ExecutionProxy`，不会绕过安全层。

## Tool 和 Linux 命令怎么交互

每个 Tool 负责四件事：

1. 声明名字、描述、输入 schema、风险等级。
2. 校验 LLM 传入的参数。
3. 构造 Linux argv。
4. 把 stdout/stderr 格式化给 LLM。

示意：

```text
LLM tool_use:
  name: sys_memory
  input: {}

Tool:
  validate({})
  build_argv({}) -> ["free", "-h"]

ExecutionProxy:
  检查命令是否在 PATH 白名单
  需要 root 时包 sudo
  运行 subprocess
  截断输出并返回 ExecutionResult
```

项目坚持使用 argv 列表，不执行 LLM 拼出来的 shell 字符串，从源头降低注入风险。

## 安全链路

安全不是一层，而是多层叠加：

| 层 | 作用 |
| --- | --- |
| IntentGuard | 在用户原始自然语言进入 LLM 前识别危险意图和 prompt injection |
| Tool.validate | 限制参数类型、枚举、正则、路径和服务名 |
| Guardrail | 对最终 argv 做规则扫描，决定 allow/confirm/deny |
| Confirm | CLI/TUI/Web 对高风险操作要求用户确认 |
| ExecutionProxy | 干净环境、PATH 白名单、timeout、rlimit、非 root 默认执行 |
| sudoers | 只给 `kyagent` 少量 root 查询或显式业务服务变更能力 |
| Audit | 每个关键阶段落库，支持回溯和完整性校验 |

更多细节见 [安全模型](safety-model.md)。

## 权限模型

正式部署时，程序长期运行在 `kyagent` 受限账户下。

普通只读命令直接执行：

```text
requires_root = False
```

需要 root 的工具通过 sudoers 精确放行：

```text
requires_root = True
sudo -n -u root -- <argv>
```

`forbid_root=true` 的含义不是“所有 root 工具都禁用”，而是“非必要不用 root；确实声明 `requires_root=True` 的工具交给 sudoers 判断”。真正彻底禁用 root 提权的是 `forbid_root_strict=true`。

权限排障见 [最小权限部署](../deployment/permissions.md)。

## LLM 后端

项目支持多个后端名称：

```text
mock
openai
deepseek
qwen
openai_httpx
deepseek_httpx
qwen_httpx
anthropic
```

LoongArch/Kylin 默认推荐 `deepseek_httpx`。原因是它只依赖 `httpx`，不需要 OpenAI SDK，也避开 `jiter`、`pydantic-core` 等在 LoongArch Old World 上容易出问题的依赖。

key 只通过环境变量注入：

```bash
DEEPSEEK_API_KEY=sk-...
```

`configs/deepseek.yaml` 里的 `api_key_env: DEEPSEEK_API_KEY` 存的是环境变量名，不是 key 本体。

## Web 控制台

`kyagent web serve` 提供 B/S 入口。页面展示：

- 用户输入
- thinking 流式增量
- tool call start/end
- 高风险 approval_required 审核卡片
- approval_resolved 结果
- 最终回复

Web 默认监听 `127.0.0.1`。如果要监听 `0.0.0.0`，必须开启认证并配置 operator/reviewer/auditor/admin 四类 token，否则 fail closed。

部署见 [Web 控制台部署](../deployment/web.md)。

## TUI

`kyagent tui` 是终端交互入口，使用 `prompt_toolkit + rich`，不引入 Textual 或 tree-sitter。

常用内置命令：

```text
/tools   查看当前启用工具
/audit   回放最近一轮 trace
/reset   清空上下文
/exit    退出
```

TUI 只负责展示和确认，不直接执行 shell。它复用同一个 `Agent.from_config()`、`ConfirmRequest`、Guardrail、ExecutionProxy 和 AuditStore。

## 主要工具域

| 工具域 | 作用 |
| --- | --- |
| `process` | 进程列表、端口占用、进程树、资源快照 |
| `network` | 监听端口、连接、路由、DNS、连接状态 |
| `logs` | journal、dmesg、大日志、日志采样 |
| `service` | systemd 状态、列表、reload/restart 受控变更 |
| `filesystem` | df、du、目录文件、挂载信息 |
| `package` | rpm/dnf/yum 或 dpkg/apt 查询、校验、更新 |
| `disk` | I/O、SMART、块设备、磁盘统计 |
| `system` | uptime、内存、CPU、kernel、time、lsblk |
| `security` | SELinux/AppArmor/KySec、sudoers、setuid、capabilities |
| `compliance` | crontab、文件 hash、AIDE 等合规检查 |
| `loongarch` | 架构、Old/New World、异架构二进制兼容性 |

赛题关键场景对应：

- 僵尸进程：`process_zombies`、`process_tree`
- 磁盘 I/O 异常：`disk_io_stats`、`disk_io_diskstats`
- 配置漂移：`pkg_verify`、`compl_file_hash`、`compl_aide_check`
- 大日志：`log_files_top`、`log_size_sample`
- 麒麟安全能力：`sec_kysec_status`
- LoongArch 部署验证：`la_arch_info`、`la_world_check`、`la_binary_compat`

## 审计链路

每次 `ask()` 和每次 MCP `tools/call` 都创建 trace。典型事件序列：

```text
USER_INPUT
INTENT_CHECK
LLM_THOUGHT
TOOL_REQUEST
SAFETY_CHECK
EXECUTION
EXECUTION_RESULT
PERCEPTION
AGENT_REPLY
```

只读工具结果会落为 `PERCEPTION evidence_id`。RCA 报告必须引用当前 trace 中已经存在的 evidence，避免凭空编造根因。

生产部署可启用 HMAC 封印，使用：

```bash
kyagent audit verify <trace-id>
```

校验审计链完整性。

## 部署与交付

正式部署入口：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent
sudo bash scripts/install-loongarch.sh --yes --with-web
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

文档：

- [LoongArch/Kylin 正式部署](../deployment/loongarch.md)
- [Web 控制台部署](../deployment/web.md)
- [最小权限部署](../deployment/permissions.md)

比赛交付建议是 `tar.gz` 安装包 + 源代码压缩包 + 部署文档，而不是 Windows `.exe`。

## 测试

常用测试命令：

```bash
python -m pytest -q
python -m pytest --collect-only -q
python -m pytest tests/test_loongarch_deploy_docs.py -q
python -m pytest tests/test_web_server.py tests/test_web_security.py -q
```

文档和部署脚本一致性由 `tests/test_loongarch_deploy_docs.py` 覆盖。Web 启动脚本行为由 `tests/test_start_web_script.py` 覆盖。

## 继续阅读

| 文档 | 作用 |
| --- | --- |
| [architecture.md](architecture.md) | 模块边界和数据流 |
| [safety-model.md](safety-model.md) | 安全层和权限模型 |
| [../deployment/loongarch.md](../deployment/loongarch.md) | LoongArch/Kylin 部署 |
| [../deployment/web.md](../deployment/web.md) | Web 控制台 |
| [../deployment/permissions.md](../deployment/permissions.md) | 权限排障 |
| [../status/current.md](../status/current.md) | 当前仓库状态 |
