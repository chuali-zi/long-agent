# 2026-05-30 package.py 扩展工作日志

分支 `feature/tool-expansion`，**只追加** `kyagent/mcp/tools/package.py`。

## 新增 6 个工具（全部 `(PkgFamilyMixin, Tool)`，Mixin 在前保证 MRO）

| 工具 | RPM argv | DPKG argv |
| --- | --- | --- |
| `pkg_verify` | `rpm -V -- <name>` | `debsums -c -- <name>` |
| `pkg_updates` | `dnf check-update --quiet` | `apt list --upgradable` |
| `pkg_security_updates` | `dnf updateinfo list security` | `apt list --upgradable` (fallback) |
| `pkg_owns_file` | `rpm -qf -- <path>` | `dpkg -S -- <path>` |
| `pkg_repo_list` | `dnf repolist` | `apt-cache policy` |
| `pkg_history` | `dnf history list`（Python 截行） | `tail -n <limit> /var/log/apt/history.log` |

## 关键设计

- `PkgUpdatesTool.format_result` 覆写：仅 RPM 路径下 `returncode=100` 视为 ok=True（dnf 用 100 表示"有更新"，是正常状态）。DPKG 走默认逻辑。
- `PkgHistoryTool` 用实例字段 `_last_limit` 把 build_argv 时的 limit 透传给 format_result（MCP 串行执行保证安全）；RPM 路径 Python 端截行，DPKG 路径 tail 限幅。
- 所有工具 `requires_root=False`，root 决策交给 sudoers 白名单（KY_PKG_QUERY，NOPASSWD），工具层不强制。
- 严格 pattern：包名 `^[A-Za-z0-9._+-]+$ / maxLength 100`；路径 `^/...$ / 2-300`；limit `1..200`。
- 未触碰 `PkgInfoTool` / `PkgInstalledTool` / `pkg_family.py` / `__init__.py` / sudoers。

## 冒烟

RPM/DPKG 6 工具 argv 全部正确；UNKNOWN 抛 ToolError；非法 path/limit 校验拒绝；dnf rc=100 ok=True、rc=1 ok=False、DPKG rc=100 ok=False。
