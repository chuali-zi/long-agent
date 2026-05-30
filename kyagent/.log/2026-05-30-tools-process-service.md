# 2026-05-30 Tools Bundle A: process.py + service.py 扩展

分支：feature/tool-expansion。只追加，不修改已有工具。

## process.py 新增 5 个工具
- process_zombies — `ps -eo stat,pid,ppid,user,comm`，format_result 过滤 STAT^Z，统计 zombie_count
- process_tree — `ps -eo pid,ppid,user,comm --forest`，可选 user 过滤（更稳健，避免 pstree 依赖）
- process_fd_count — `ls -1 /proc/{pid}/fd`，format_result 返回 fd_count
- process_resource — `cat /proc/{pid}/status`，原样返回 VmRSS/VmSize/Threads/FDSize 等
- top_cpu_snapshot — `top -bn1 -w 256`，format_result 截首 30 行

## service.py 新增 8 个工具
- svc_is_active / svc_is_enabled / svc_show / svc_cat — 走 `_validate_unit` + pattern `^[A-Za-z0-9@._\-+:]+$`，全 `--`分隔
- svc_failed / svc_timers — 无参列表查询
- boot_analyze — `systemd-analyze blame`，截 30 行
- boot_logs — `journalctl -b [offset] -p err`，offset 范围 -10..0

## 约束
- 全部 risk=LOW，read_only=True，requires_root=False
- 走基类 validate（pattern/range）+ _validate_unit 双层校验
- format_result 重写都基于 super() 修改 content/data，不重拼

## 自检
- `python -c "from ...process import ProcessZombiesTool...; from ...service import SvcIsActiveTool..."` → OK
- register() 末尾追加 13 个 instance
