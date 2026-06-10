# 2026-06-10

新增真实 LLM 非交互 bench runner：`benchmarks/run-real-llm.sh` 统一调用各题 `run.sh --ask`，每题写 `/tmp/kybench-rerun-*.log`，可选设置 permissions-prod 和逐题 teardown。同步修正 demo probe 文案，避免自动化依赖 Web 审核界面。
