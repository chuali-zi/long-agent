# 2026-05-30 工具集烟雾测试 + 文档对齐（Bundle F 收尾）

## 范围

73 个新工具落地后，缺一条"赛题评审能一目了然看到工具覆盖"的对齐工作。
本次只动测试与文档，不改任何 .py 工具实现。

## 测试

新增 `tests/test_tools_expansion.py`：123 条静态烟雾用例，按 10 个域分类
（`TestProcessExtensions` / `TestServiceExtensions` / `TestNetworkExtensions` /
`TestLogsExtensions` / `TestPackageMixin` / `TestDiskTools` / `TestSystemTools`
/ `TestSecurityTools` / `TestComplianceTools` / `TestLoongArchTools`），加
`TestRegistry` 跨域不变量。全部走 `Tool.validate → build_argv`，不打子进程、
不打网络。`registry` 用 `scope="session"` fixture 避免 module-level 触发。
单文件 0.25 s 跑完，全量 394 passed / 2 skipped（POSIX）。

负样本覆盖：`svc_is_active` 含 `;`、`net_dns_resolve` 含 `$`、`log_grep_recent`
含 backtick / `$` / `;`、`sec_sudoers_audit` 含 `/` 与大写、相对路径系列、
`PkgVerifyTool` 在 UNKNOWN family 抛 ToolError。

## 文档

- 根 `README.md` §5.5 新增"工具集（92 个）"小节，按域分小表，赛题场景一栏
  对应到具体工具名。
- `docs/kyagent/architecture.md` 新增 §5 工具集架构：10 个域、TrendTool 约定
  （doc-marker，原生 interval）、PkgFamilyMixin 设计（os-release 一次性判定 +
  缓存）、工具 → Guardrail → ExecutionProxy → Audit 单向依赖。
- `docs/kyagent/README.md` §7 工具清单刷新代表性条目，点出 KySec / LoongArch
  / TrendTool / PkgFamilyMixin 亮点；§8 测试数刷到 394。
- `docs/status/log.md` 追加 2026-05-30 工具大扩展条目。
