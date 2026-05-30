# 2026-05-30 network.py / logs.py 工具扩展

分支 `feature/tool-expansion`，Bundle B。仅追加，未改动现有工具/基类/__init__/sudoers。

## network.py 新增 9
- net_routes / net_arp / net_addr：`ip -j` JSON。
- net_link_stats：`ip -s -j link`，可选 iface 白名单 `^[a-zA-Z0-9._@-]+$`。
- net_firewall_iptables / net_firewall_nft：requires_root=True，走 sudoers。
- net_conn_state_summary：`ss -ant` 解析首列状态聚合，data 加 state_count。
- net_dns_resolve：`getent hosts`，name 严格 pattern。
- net_tcp_stats：`ss -s`。

## logs.py 新增 7
- log_files_top：find /var/log >1M，format_result 按 size 倒排截 top-N（limit 经 validate 暂存 self._limit）。
- log_size_sample：du -sb 批量采样，paths 白名单绝对路径。
- log_grep_recent：journalctl --grep，pattern 严格拒绝 `` `$|;&(){}<>`` + 换行。
- log_ssh_audit / log_auth_failed：journalctl 时间窗 grep。
- log_audit_summary：aureport，requires_root=True。
- log_rotated_count：find *.gz/*.1/*.2，format_result 计数 + 前 50 行。

## 验证
- 冒烟 import OK；registry.names() 共 21 个工具。
- argv 生成与 pattern 拒绝（backtick）均符合预期。

## 偏离
- LogFilesTopTool 的 limit 经 validate 阶段写入 self._limit 供 format_result 使用——
  方案没规定取参方式，这是 Tool 基类约束下最小侵入做法。
