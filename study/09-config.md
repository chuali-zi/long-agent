# 09 · 配置系统

> 文件：
> - `kyagent/config.py`（Pydantic schema + YAML 加载 + 项目根 JSON 覆盖 + env 展开）
> - `configs/default.yaml`（默认 `deepseek_httpx` 后端）
> - `configs/openai.yaml`（OpenAI 协议兼容）

---

## 1. 设计目标

1. **强类型**：Pydantic 在加载时把所有字段校验到对应类型
2. **环境变量友好**：YAML 里写 `${VAR:-default}` 自动展开
3. **可发现**：没有配置文件时也能跑（全默认）
4. **可覆盖**：显式环境变量 > 项目根 `kyagent.json` > YAML 配置 > Pydantic 默认

---

## 2. Pydantic schema 树

```
Config
├── agent: AgentConfig
│   ├── name: str = "kyagent"
│   ├── llm_backend: str = "deepseek_httpx" # mock / anthropic / openai / deepseek / *_httpx
│   ├── anthropic: AnthropicConfig
│   │   ├── model: str = "claude-opus-4-7"
│   │   ├── max_tokens: int = 4096
│   │   └── api_key_env: str = "ANTHROPIC_API_KEY"
│   ├── openai: OpenAIConfig
│   │   ├── model: str = "gpt-4o-mini"
│   │   ├── max_tokens: int = 4096
│   │   ├── temperature: float = 0.2
│   │   ├── api_key_env: str = "OPENAI_API_KEY"
│   │   ├── base_url: str | None = None
│   │   └── organization: str | None = None
│   └── max_iterations: int = 8
├── executor: ExecutorConfig
│   ├── account: str = "kyagent"
│   ├── timeout: int = 30
│   ├── output_cap: int = 65536
│   ├── forbid_root: bool = True
│   └── path: list[str] = ["/usr/local/bin","/usr/bin","/bin"]
├── safety: SafetyConfig
│   ├── rules_file: str = "configs/safety-rules.yaml"
│   ├── policy: SafetyPolicy
│   │   ├── critical: str = "deny"
│   │   ├── high: str = "confirm"
│   │   ├── medium: str = "confirm"
│   │   └── low: str = "allow"
│   └── llm_review: bool = False
├── audit: AuditConfig
│   ├── database: str = "./var/audit.db"
│   ├── jsonl_file: str | None = "./var/audit.jsonl"
│   └── retain_days: int = 90
├── mcp: McpConfig
│   ├── enable_tools: list[str] = []
│   ├── server_name: str = "kyagent"
│   └── server_version: str = "0.1.0"
└── base_dir: Path = Path.cwd()
```

每一个 `*Config` 都是一个 `pydantic.BaseModel`，字段都带默认值，所以即便整段配置缺失也能正常构造。

---

## 3. \_expand\_env：环境变量展开（config.py:13）

```python
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-(.*?))?\}")

def _expand_env(value):
    if isinstance(value, str):
        def _sub(m):
            var, default = m.group(1), m.group(2) or ""
            return os.environ.get(var, default)
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value
```

支持 `${VAR}` 和 `${VAR:-default}` 两种语法：
- `${KYAGENT_LLM_BACKEND}` → 取环境变量，无值时变空字符串
- `${KYAGENT_LLM_BACKEND:-deepseek_httpx}` → 取环境变量，无值时回退 `deepseek_httpx`

递归处理 dict 和 list，所以 `path: ["${A:-/usr/bin}"]` 这种也能展开。

正则约束：
- 变量名必须以字母或下划线开头
- 后续只允许大写字母 / 数字 / 下划线
- 这种保守命名约定避免误把 `${something}` 当成变量

---

## 4. find_default_config（config.py:108）

```python
def find_default_config() -> Path | None:
    env = os.environ.get("KYAGENT_CONFIG")
    if env and Path(env).exists():
        return Path(env)
    for candidate in (
        Path.cwd() / "configs" / "default.yaml",
        Path(__file__).parent.parent / "configs" / "default.yaml",
    ):
        if candidate.exists():
            return candidate
    return None
```

查找优先级：
1. 环境变量 `KYAGENT_CONFIG` 指向的文件
2. 当前工作目录的 `configs/default.yaml`
3. 包安装目录的 `configs/default.yaml`
4. 都没有 → 返回 None（用全默认）

---

## 5. load_config（config.py:122）

```python
def load_config(path=None) -> Config:
    cfg_path = Path(path) if path else find_default_config()
    if cfg_path is None or not cfg_path.exists():
        return Config(base_dir=Path.cwd())

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = _expand_env(raw)
    raw = _apply_project_json_overrides(raw, project_root)
    cfg = Config.model_validate(raw)
    cfg.base_dir = cfg_path.parent.parent  # configs/ 的父目录是项目根
    return cfg
```

流程：
1. 解析 cfg_path（参数 > 自动发现）
2. 不存在 → 全默认 + base_dir = cwd
3. 存在 → yaml.safe_load + 环境展开 + 项目根 `kyagent.json` 轻量覆盖 + Pydantic 校验
4. `base_dir` 设为 `configs/` 的父目录（项目根），后续 `cfg.resolve("./var/audit.db")` 会以此为锚点

**`yaml.safe_load`** 而不是 `yaml.load`：杜绝 YAML 反序列化攻击（`!!python/object/apply:os.system` 这种）。

---

## 6. Config.resolve（config.py:101）

```python
def resolve(self, p: str | Path) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return (self.base_dir / path).resolve()
```

把相对路径解析成绝对路径，参照 `base_dir`。例如：
- `audit.database: ./var/audit.db` + base_dir=`D:\race\long\` → `D:\race\long\var\audit.db`

所有需要落地的路径都过 `cfg.resolve()`，避免相对路径在 CWD 改变时出错。

---

## 7. configs/default.yaml

```yaml
agent:
  name: kyagent
  llm_backend: ${KYAGENT_LLM_BACKEND:-deepseek_httpx}
  fallback_to_mock: false
  anthropic:
    model: claude-opus-4-7
    max_tokens: 4096
    api_key_env: ANTHROPIC_API_KEY
  max_iterations: 8

executor:
  account: kyagent
  timeout: 30
  output_cap: 65536
  forbid_root: true
  path:
    - /usr/local/bin
    - /usr/bin
    - /bin
    - /usr/sbin
    - /sbin

safety:
  rules_file: configs/safety-rules.yaml
  policy:
    critical: deny
    high: confirm
    medium: confirm
    low: allow
  llm_review: false

audit:
  database: ./var/audit.db
  jsonl_file: ./var/audit.jsonl
  retain_days: 90

mcp:
  enable_tools: []
  server_name: kyagent
  server_version: 0.1.0
```

要点：
- 默认 `llm_backend: deepseek_httpx`，优先使用真实 DeepSeek httpx 后端
- DeepSeek key 只允许通过 `DEEPSEEK_API_KEY` 或受控部署 env 文件注入；项目文件中的密钥字段会被忽略
- 两处都缺 key 时直接报错；离线演示需显式设置 `llm_backend=mock`
- 项目根 `kyagent.json` 可写 `{"llm_backend":"qwen_httpx"}` 覆盖默认 YAML
- 显式环境变量 `KYAGENT_LLM_BACKEND` 覆盖 `kyagent.json`（`KYAGENT_LLM_BACKEND=anthropic kyagent ask "..."`）
- `forbid_root: true` 默认禁用 root 提权
- `enable_tools: []` 空白名单 = 全部启用
- `llm_review: false` 默认关闭 LLM 复审

---

## 8. configs/openai.yaml

```yaml
agent:
  name: kyagent
  llm_backend: openai
  openai:
    model: ${KYAGENT_OPENAI_MODEL:-gpt-4o-mini}
    max_tokens: 4096
    temperature: 0.2
    api_key_env: OPENAI_API_KEY
    base_url: ${KYAGENT_OPENAI_BASE_URL:-}
    organization: ${KYAGENT_OPENAI_ORG:-}
  max_iterations: 8

executor:
  account: kyagent
  timeout: 30
  output_cap: 65536
  forbid_root: true
  path:
    - /usr/local/bin
    - /usr/bin
    - /bin
    - /usr/sbin
    - /sbin

safety:
  rules_file: configs/safety-rules.yaml
  policy:
    critical: deny
    high: confirm
    medium: confirm
    low: allow
  llm_review: false

audit:
  database: ./var/audit.db
  jsonl_file: ./var/audit.jsonl
  retain_days: 90

mcp:
  enable_tools: []
  server_name: kyagent
  server_version: 0.1.0
```

用法：

```bash
# 当前部署唯一推荐：DeepSeek（建议直接用 configs/deepseek.yaml，不必走 openai.yaml）
export DEEPSEEK_API_KEY=sk-...
export KYAGENT_CONFIG=$(pwd)/configs/deepseek.yaml
kyagent ask "查 80 端口"
```

> **关于其他 OpenAI 协议兼容端点**（OpenAI 官方 / vLLM / Ollama / Azure / 智谱 GLM / 通义千问 等）：
> 代码层支持 SDK 路径和 `openai_httpx / deepseek_httpx / qwen_httpx` 纯 httpx 路径。
> 当前阶段生产部署只推 DeepSeek；LoongArch Old World 用 `deepseek_httpx`，不安装 openai SDK extra。

---

## 9. configs/safety-rules.yaml

详细见 05-safety-layer.md 第 4 节。这里仅说结构：

```yaml
rules:
  - id: <唯一 id>
    risk: <low|medium|high|critical>
    description: <人话>
    pattern: <可选 regex on cmdline>
    command: <可选 argv[0] basename>
    flags_any: [<flags>]      # 任一出现即匹配
    flags_all: [<flags>]      # 全部出现才匹配
    target_in: [<paths>]      # 任一位置参数在这些前缀下
```

`safety/rules.py` 的 `load_rules()` 把每条规则 `from_dict` 成 `Rule` dataclass。

---

## 10. configs/sudoers.kyagent

不是 YAML，是 sudo 的 sudoers 语法。详细见 06-executor-sandbox.md 第 12 节。

要点：
- 安装到 `/etc/sudoers.d/kyagent`（权限 0440）
- kyagent 账户允许的命令通过 `Cmnd_Alias` 分组
- NOPASSWD 仅给只读 + 受控写
- 显式黑名单 sh / bash / python / awk / sed 等解释器

---

## 11. 怎么扩展配置

加一个新字段的流程：

1. 在 `kyagent/config.py` 对应的 `*Config` 类加字段并给默认值：
   ```python
   class ExecutorConfig(BaseModel):
       # ...
       my_new_field: int = 42
   ```

2. 在 `configs/default.yaml` 加同名 key（可选，省略时走默认）：
   ```yaml
   executor:
     my_new_field: 100
   ```

3. 在用到的地方读：
   ```python
   cfg.executor.my_new_field
   ```

Pydantic 会自动校验类型 / 必填。

---

## 12. 关键不变量

1. **配置加载是同步、惰性的**：第一次 `load_config()` 才读 YAML
2. **`cfg.base_dir`** 是相对路径的锚点：永远是 `configs/` 的父目录（项目根）
3. **`yaml.safe_load` 而非 `yaml.load`**：杜绝 YAML 反序列化攻击
4. **环境变量展开是字符串值上做的**：dict key / 数字字段不展开
5. **配置不存在不报错**：全默认兜底，让"零配置启动"成立
6. **项目根 JSON 只做轻量覆盖**：当前公开顶层 key 是 `llm_backend`，避免把两套完整配置格式混在一起

---

## 13. 下一步

继续 → [10-cli-entry.md](./10-cli-entry.md) 看 CLI 入口怎么把所有组件串起来。
