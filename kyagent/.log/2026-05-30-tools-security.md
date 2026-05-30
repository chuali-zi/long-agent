# 2026-05-30 安全/合规/LoongArch 工具组

新建 3 文件 22 工具，全 read_only=True，未改 base.py。

## security.py (13)
sec_selinux_status, sec_apparmor_status, sec_kysec_status (麒麟特有 /sys/kernel/security/kysec/state，非麒麟环境 returncode!=0 自然降级), sec_setuid_files, sec_world_writable, sec_capabilities, sec_passwd_audit (getent passwd + Python 端 split 过滤 UID=0 / shell 以 sh 结尾，规避 awk 黑名单), sec_sudoers_audit, sec_ssh_config (过滤注释/空行), sec_kernel_taints (按 bit 解码为 PROPRIETARY_MODULE/FORCED_MODULE 等 19 个标记), sec_kernel_modules, sec_listening_external (仅留 0.0.0.0/[::] 行), sec_audit_status。

## compliance.py (6)
compl_aide_check, compl_file_attr, compl_file_hash, compl_timestamp_audit (数组路径再过白名单), compl_hosts, compl_cron_dump (含 user 切 crontab -l)。

## loongarch.py (3)
la_arch_info (cpuinfo 关键字段过滤), la_world_check (rc=0 New World / rc!=0 Old World，自行组装绕开 super 的 ok=False 路径，输出 verdict + raw), la_binary_compat (file 输出提取 arch 写入 data)。

## requires_root
sec_apparmor_status, sec_sudoers_audit, compl_aide_check。

## MEDIUM risk
sec_setuid_files, sec_world_writable, sec_sudoers_audit, compl_aide_check, compl_cron_dump。

冒烟：22 tools import OK。
