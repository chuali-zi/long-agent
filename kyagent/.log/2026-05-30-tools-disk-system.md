# Bundle D：disk.py + system.py 新增

分支：`feature/tool-expansion`。仅新建两个文件，未改动 base / __init__ / sudoers / 其他工具。

## disk.py（7 个）

- `DiskIoStatsTool` —— **TrendTool 子类**，`iostat -dx <interval> <count>`，默认 `1 2`，interval 1..5 / count 2..5。
- `DiskIoDiskstatsTool` —— `cat /proc/diskstats`，LLM 自行间隔两次算 delta。
- `DiskInodeUsageTool` —— `df -i [-- path]`，path 走 `^/[A-Za-z0-9._/@-]+$`。
- `DiskOpenDeletedTool` —— `lsof +L1`，找已删除仍打开的句柄。
- `DiskMountTool` —— `findmnt -J [-- target]`，JSON 输出。
- `DiskSmartTool` —— `smartctl -H -A -- /dev/xxx`，**requires_root + MEDIUM**，device pattern `^/dev/[a-z]+[0-9]*$`。
- `DirLargestFilesTool` —— `find -printf "%s\t%p\n"`，`format_result` 重写按字节倒排截前 30 行（limit 暂未透传，使用默认）。

## system.py（9 个）

`sys_uptime / sys_loadavg / sys_memory / sys_swap / sys_kernel / sys_cpu_info / sys_dmi / sys_time_sync / sys_block_devices`。除 `SysDmiTool`（dmidecode 必须 root，MEDIUM）外全部 LOW、read_only、无参。

## 自检

冒烟脚本通过：iostat argv 正确、所有非 TrendTool 类未误继承、device/path pattern 正确拒绝非法输入。`requires_root` 名单：`DiskSmartTool, SysDmiTool`。TrendTool 子类：`DiskIoStatsTool`。
