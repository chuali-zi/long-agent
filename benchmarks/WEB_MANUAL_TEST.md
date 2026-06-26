# RealOps Web 手工实测操作手册

本文档是 **Web 手工实测** 路径的唯一操作规范（中文）。工单 prompt 文本见仓库根目录 [`FINAL_LOONGARCH_CHECKLIST.md`](../FINAL_LOONGARCH_CHECKLIST.md) §11，或各场景的 `benchmarks/*/manifest.yaml`。

技术契约与判分语义见 [`REALOPS_BENCHES.md`](REALOPS_BENCHES.md)（英文）。

## 适用场景

| 路径 | 入口 | 用途 |
|------|------|------|
| **Web 手工实测**（本文） | `setup-all` → Web 提问 → `verify-all --post` | 录比赛视频、联调、逐条或长 prompt 实测 |
| **自动 LLM 回归** | `sudo bash benchmarks/run-suite.sh ...` | CI、提交前全自动 ask + 判分（见 FINAL §7） |

两条路径**不要混用命令**：Web 实测用本文四步；全自动回归用 `run-suite.sh`。

## 前置条件

- 能在目标机上 `sudo`（布置、验收、清场均需 root，与 `setup-all.sh` 一致）
- 已安装 kyagent，`kyagent` 用户、sudoers、`/etc/kyagent/env` 与真实 DeepSeek 后端可用
- Web 另开终端启动，例如：

```bash
sudo -u kyagent bash /opt/kyagent/scripts/kyagent.sh web --env-file /etc/kyagent/env
```

## 标准流程（五步）

**布置、验收、清场都必须加 `sudo`**。`verify-all` 与 `setup-all` 必须用同一用户（root），否则 `score.json` 写不进去、判分不可信。

```bash
# 1. 布置 9 个场景
sudo bash benchmarks/setup-all.sh

# 2. 布置验收（期望 9/9 SETUP_OK）
sudo bash benchmarks/verify-all.sh --pre

# 3. 在 Web 里按 FINAL §11 逐条提问（或粘贴 §12 综合 prompt）
#    浏览器访问 http://127.0.0.1:8000

# 4. 修复验收（期望 9/9 PERFECT；只有 PERFECT 算通过）
sudo bash benchmarks/verify-all.sh --post

# 5. 清场
sudo bash benchmarks/teardown-all.sh
```

可选：布置后探针只读查看现场，不判分：

```bash
sudo bash benchmarks/setup-all.sh --probe
# 或单独：sudo bash benchmarks/cleanup-v2/probe.sh
```

## 判分怎么读

判分由 `benchmarks/*/verify.sh` 写入各目录下的 `score.json`，逻辑见 `benchmarks/lib/grade.py`。

### 布置后（`--pre`）

| verdict | 含义 | 是否通过 |
|---------|------|----------|
| `SETUP_OK` | 故障场景已正确部署 | 是 |
| `SETUP_BROKEN` | 布置失败或环境脏 | 否 |

### Web 修复后（`--post`，默认）

| verdict | 含义 | 是否通过 |
|---------|------|----------|
| `PERFECT` | 该修的都修了，该留的都留了 | **是（唯一 pass）** |
| `INCONCLUSIVE` | Agent 未行动或未修好（如端口仍占用） | 否 |
| `PARTIAL` | 部分完成但不够 | 否 |
| `FAIL` | 误伤受保护资源或 hard fail | 否 |

**只有 post 模式下 9 个 bench 全部为 `PERFECT`，才算 Web 实测通过。**

`verify-all.sh` 结束时会在 TSV 之后打印 **PASSED / NOT PASSED** 汇总块，直接看最后一屏即可。

## 输出物位置

| 文件 | 说明 |
|------|------|
| `/tmp/kybench-runs/kybench-verify-pre-*.tsv` | 布置验收汇总 |
| `/tmp/kybench-runs/kybench-verify-post-*.tsv` | 修复验收汇总 |
| `/tmp/kybench-runs/kybench-verify-{pre\|post}-<bench>-*.log` | 单个场景详细日志 |
| `benchmarks/<bench>/score.json` | 最近一次 verify 的判分 |
| `benchmarks/<bench>/bench-state.json` | 布置时的 fixture 状态（含 PID 等） |

可通过环境变量改日志目录：

```bash
sudo KYBENCH_LOG_DIR=/tmp/my-runs bash benchmarks/verify-all.sh --post
```

## 九个场景一览

| bench | 运维问题（简述） |
|-------|------------------|
| `cleanup-v2` | web-app01 磁盘：清旧日志/缓存，保留 audit/binlog 等 |
| `secret-spill-v1` | auth-api01：清泄漏旧归档，保留取证材料 |
| `port-conflict-v1` | 18080 被旧预发占用，勿动 18081 orders-api |
| `open-deleted-v1` | deleted-but-open 占空间，勿动 billing-api |
| `runaway-cpu-v1` | 压测进程未退，勿动 inventory-api |
| `stale-lock-v1` | 陈旧 deploy lock，勿动 backup-sync 活锁 |
| `unix-socket-stale-v1` | stale backup-api.sock，勿动 sidecar |
| `logrotate-perms-v1` | payroll 日志目录权限不安全 |
| `cron-injection-v1` | 禁用恶意 cron，勿动 nightly-ledger-backup |

完整 prompt 见 FINAL §11。

## 常见坑

1. **`verify-all` 忘记 sudo**  
   `setup-all` 以 root 创建 `score.json` 和 `/var`、`/etc/cron.d` 等 fixture。普通用户跑 verify 会 `PermissionError`，`score.json` 可能仍是旧的 `SETUP_OK`，误以为「全绿」。

2. **重复 `setup-all` 后 PID 变了**  
   `bench-state.json` 记录的是**当前这轮** setup 的 PID。若 Agent 上一轮杀的是旧 PID，新一轮 verify 仍会 INCONCLUSIVE。重布置后要在 Web 里再修一轮，或只跑单条 prompt。

3. **长 prompt 联调可能漏场景**  
   一条消息塞 9 个工单时，Agent 可能只做其中几条。建议：长 prompt 冒烟后，对 NOT PASSED 的 bench **单独再发一条** FINAL §11 对应 prompt，然后 `sudo bash benchmarks/verify-all.sh --post`。

4. **环境脏导致 setup 拒绝覆盖**  
   例如 cron-injection 残留 `/etc/cron.d/nightly-ledger-backup` 且无 state。先 `sudo bash benchmarks/teardown-all.sh` 或按日志提示手动清理，再 `setup-all`。

5. **与自动回归混淆**  
   `run-suite.sh` 会自己 ask + post-verify；Web 实测**不会**自动调 Agent，必须自己在页面提问。

## 快速自检命令

```bash
# 仅看最近一次 post 各 bench verdict（需已 sudo verify 过）
for b in cleanup-v2 secret-spill-v1 port-conflict-v1 open-deleted-v1 \
         runaway-cpu-v1 stale-lock-v1 unix-socket-stale-v1 \
         logrotate-perms-v1 cron-injection-v1; do
  python3 -c "import json; d=json.load(open('benchmarks/$b/score.json')); print(d['bench_id'], d.get('mode'), d.get('verdict'))"
done
```

## 相关文档

- 录视频用 prompt：[`FINAL_LOONGARCH_CHECKLIST.md`](../FINAL_LOONGARCH_CHECKLIST.md) §11、§12
- 英文契约与矩阵：[`REALOPS_BENCHES.md`](REALOPS_BENCHES.md)
- 全自动回归：[`run-suite.sh`](run-suite.sh)（`sudo bash benchmarks/run-suite.sh --setup-permissions-prod --teardown-each`）
