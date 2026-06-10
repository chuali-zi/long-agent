# 2026-06-10

调整清理写入预检：继续硬拒绝审计、认证、登录、数据库、symlink、非普通文件等目标；允许 /var/cache、临时 build/spool 残留和明确 stale/old/archive 命名日志进入 allow-confirm，以覆盖安全可丢弃但未轮转的清理项。
