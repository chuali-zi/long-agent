# 2026-06-06 一键生产预设 sudoers

新增 scripts/setup-sudoers-prod.sh：setup-sudoers.sh 的薄封装，预开三个写操作开关
(KYAGENT_ENABLE_LOG_CLEAN/PKG_MGMT/PROC_KILL=1，可被已存在环境变量覆盖) 并给出
13 个常见可重启服务的默认 KYAGENT_SERVICE_ALLOWLIST。打印授权摘要→交互确认(--yes 跳过/
非交互无 --yes 直接退 1)→exec 核心脚本(由其 visudo 校验+回滚)。kyagent.sh 加
permissions-prod 子命令与 usage。tests/test_sudoers_least_privilege.py +5 用例锁定
默认开关/常见服务/非交互守卫/覆盖生效/子命令存在。README 加"写操作授权(一键生产预设)"节。
