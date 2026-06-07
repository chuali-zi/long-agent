# scripts 模块工作日志

## 2026-06-06 新增 OS 层日志清空包装器
- 新增 `scripts/kyagent-log-clean`（python3）：受 sudoers 授权的最小化清空助手，
  realpath 预检 + O_NOFOLLOW（开原始路径）+ /proc/self/fd 权威越界判定 + 普通文件 +
  ftruncate。允许 `..` 语法、按解析后语义判越界，封死符号链接/TOCTOU。
- `setup-sudoers.sh`：render_log_clean 去掉 4 条裸 truncate 正则，改授权
  `/usr/local/bin/kyagent-log-clean ^/[A-Za-z0-9._/@-]+$`；新增 install_log_clean_wrapper
  （开关打开时以 root:root 0755 装到 /usr/local/bin，在白名单 PATH 内）。
- 修复 P1：旧 `/tmp/[A-Za-z0-9._/@-]+` 允许 `..` 越界清空 /etc/shadow。
