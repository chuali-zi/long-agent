# 工作日志 2026-06-05 — sudoers opt-in 写操作授权

## 任务
在 setup-sudoers.sh 新增三个 opt-in 写操作授权开关，守住默认只读不变量。

## 变更
1. **scripts/setup-sudoers.sh**：新增 `render_log_clean`、`render_pkg_mgmt`、`render_proc_kill` 三个顶层函数，分别由 `KYAGENT_ENABLE_LOG_CLEAN=1`、`KYAGENT_ENABLE_PKG_MGMT=1`、`KYAGENT_ENABLE_PROC_KILL=1` 门控；main() 中紧跟 render_service_allowlist 依次调用这三个函数追加至 TMP_SUDOERS，同受 visudo -cf 校验。
2. **tests/test_sudoers_least_privilege.py**：新增 9 个测试，覆盖三个函数的开启/关闭两种状态，并新增 test_default_sudoers_does_not_grant_write_operations 守住默认只读不变量（KY_LOG_CLEAN/KY_PKG_MUTATE/KY_PROC_KILL/journalctl --vacuum/dnf -y install//usr/bin/kill 均不出现在模板）。
3. **docs/deployment/permissions.md**：补充"写操作授权（opt-in）"一节，说明三个开关的命令范围和安全约束。

## 验证
- `bash -n scripts/setup-sudoers.sh`：语法正确。
- `pytest tests/test_sudoers_least_privilege.py -q`：21 passed，0 fail，0 skip。
- configs/sudoers.kyagent 未修改，默认只读不变量守住。
