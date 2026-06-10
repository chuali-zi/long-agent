# 2026-06-10

修复非交互安全修复模式：启用 auto_approve_safe_remediation 时，系统提示要求模型不要等待确认或用 ask_user_choice 做执行确认；若仍调用 ask_user_choice，Agent 非阻塞返回错误并指引改用具体受控修复工具，避免 CLI 单轮 benchmark 卡死在用户选择。
