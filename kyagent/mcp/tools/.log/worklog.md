# tools 模块工作日志

## 2026-06-06 修复 P1：日志清空越界（sudoers 最终防线）
- 背景：sudoers 旧授权 `truncate -s 0 -- /tmp/[A-Za-z0-9._/@-]+` 字符类含 `.` 和 `/`，
  `..` 可匹配，且 truncate 跟随符号链接 → root 越界清空任意文件。
- 决策：不"懒惰禁 `..`"，改 wrapper 在 OS 层语义校验。`fs_truncate.build_argv`
  从 `["truncate","-s","0","--",path]` 改为 `["kyagent-log-clean", path]`。
- 校验链下沉到 `scripts/kyagent-log-clean`：realpath 预检 → O_NOFOLLOW 打开原始路径
  → /proc/self/fd 权威越界判定 → 普通文件 → ftruncate。工具层 normpath 仍保留作纵深。
- 影响文件：filesystem.py、scripts/setup-sudoers.sh、scripts/kyagent-log-clean、
  docs/deployment/permissions.md、test_tools_expansion.py、test_sudoers_least_privilege.py。
