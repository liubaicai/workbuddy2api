# workbuddy2api

> 把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](./LICENSE)

---

## 适用场景

| 客户端 | 协议 | 接口 |
|--------|------|------|
| **Codex CLI** | OpenAI Responses | `/v1/responses` |
| **Claude Code / CC Switch** | Anthropic Messages | `/v1/messages` |
| **Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI** | OpenAI Chat | `/v1/chat/completions` |

---

## 这是什么

`workbuddy2api` 是一个本地协议转换器。读取你已登录的 WorkBuddy / CodeBuddy 桌面端凭据，转发到腾讯后端 `copilot.tencent.com`，暴露标准兼容 API：

| 端点 | 说明 |
|------|------|
| `GET /` | **Web 管理面板**（健康状态、授权管理、模型列表） |
| `POST /v1/chat/completions` | OpenAI Chat Completions（原生 tools / tool_calls / SSE） |
| `POST /v1/responses` | OpenAI Responses（Codex CLI 兼容） |
| `POST /v1/messages` | Anthropic Messages（Claude Code / CC Switch 兼容） |
| `GET /v1/models` | 模型列表（从上游动态获取，缓存 5 分钟） |
| `GET /health` | 健康检查 |
| `POST /upload-auth` | 上传授权文件 |
| `GET /auth-files` | 授权文件列表与状态 |
| `POST /auth-files/activate` | 激活指定授权文件 |
| `DELETE /auth-files/delete` | 删除授权文件 |

它不负责登录，不模拟桌面端，也不替你执行工具。只做三件事：

1. 读取本机登录态并注入鉴权头
2. 在 OpenAI / Anthropic 协议和腾讯后端协议之间转换
3. 对 `Codex CLI` 这类长上下文 agent 请求做后端友好的压缩投影

> **命名说明**：项目对外名称叫 `workbuddy2api`。代码里保留部分历史命名（`codebuddy2openai`、`CODEBUDDY2OPENAI_*`），兼容旧配置和环境变量。GitHub 仓库路径也可能沿用 `codebuddy2openai`，不影响使用。

---

## 3 分钟上手

### 1. 前置条件

- 本机已安装并登录 **WorkBuddy / CodeBuddy** 桌面端
- Python 3.8+
- 依赖：`fastapi`、`uvicorn`、`httpx`、`python-multipart`

登录态默认查找路径：

| 平台 | 路径 |
|------|------|
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/` |
| Windows | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\` |
| Linux | `~/.local/share/CodeBuddyExtension/Data/Public/auth/` |

### 2. 安装

推荐使用 `uv`：

```bash
git clone https://github.com/ShouZhuo0413/codebuddy2openai.git workbuddy2api
cd workbuddy2api

uv venv
uv pip install -r requirements.txt
```

或使用标准虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 启动

```bash
uv run converter.py --desensitize --log converter.log
```

看到 `监听 http://127.0.0.1:8787` 即启动成功。

### 4. 验证

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

---

## Web 管理面板

启动后访问 **`http://127.0.0.1:8787`** 即可打开管理面板，提供：

- **健康状态** — 服务状态、当前账号、Token 有效期
- **上传授权文件** — 上传 `.info` 格式的授权文件
- **授权文件管理** — 查看、激活、删除已上传的授权文件，显示 Token 状态
- **模型列表** — 从上游实时获取可用模型

### 授权文件管理规则

- 上传后**自动激活**
- 相同 UID 的文件上传时**自动替换**（不产生重复文件）
- 只有一个文件时**自动激活**，无需手动操作
- 多个文件时需**手动切换**激活哪个
- 授权文件保存在 `.config/` 目录，重启不丢失

---

## 客户端接入

### Codex CLI（推荐）

Codex CLI 走 `/v1/responses`。推荐启动：

```bash
uv run converter.py --desensitize --log converter.log
```

合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "你的任务描述"
```

> - 推荐保留 `--desensitize`
> - `/v1/responses` 默认已做投影压缩
> - 想保留原始 system prompt 可试 `--desensitize --no-compact`
> - `--no-compact` 下若命中审核，会自动退回紧凑模式重试一次

### Claude Code / CC Switch

走 `/v1/messages`（Anthropic Messages API）。推荐启动：

```bash
uv run converter.py --desensitize --log converter.log
```

CC Switch 配置：

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

> - 模型名需填写腾讯后端支持的真实模型名
> - Claude Code 场景**强烈建议**开启 `--desensitize`

### 其他 OpenAI 兼容客户端

适用于 Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI 等。

- **Base URL**: `http://127.0.0.1:8787/v1`
- **API Key**: 留空，或填 `--api-key` 设定的值
- **模型名**: 见 `/v1/models` 返回的列表

---

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | — | 客户端鉴权密钥 |
| `--log` | — | 日志输出路径 |
| `--desensitize` | 关 | 零宽脱敏 + 压缩运行时提示 + 去掉 tool description |
| `--no-compact` | 关 | 配合 `--desensitize`，保留完整 system prompt（仅做零宽脱敏） |
| `--skip-check` | 否 | 跳过启动预检 |

### curl 示例

```bash
# 非流式
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

# 流式
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'
```

---

## 日志与排障

### 推荐启动方式

```bash
uv run converter.py --desensitize --log converter.log
```

### 日志内容

每次请求带唯一 ID，常见日志行：

- `▶ REQUEST` / `▶ RESPONSES` / `▶ ANTHROPIC` — 请求摘要
- `── REQUEST BODY` / `── RESPONSES → CHAT BODY` — 发往后端的完整请求
- `── RESPONSES PROJECTION` — 投影压缩统计
- `── RESPONSE BODY` / `── RESPONSE RAW SSE` — 后端响应
- `⚠️内容审核拦截` — 触发审核

### 常见问题

**找不到登录文件**
→ 桌面端未登录，或登录路径不在默认位置。可通过 Web 面板上传 `.info` 授权文件替代。

**401 错误**
- 本地 401：启用了 `--api-key` 但客户端未携带 → 检查客户端配置
- 后端 401：腾讯 token 过期 → 重新登录桌面端，或重新上传授权文件

**被"敏感内容"拦截**
腾讯后端内容审核，常由 agent runtime 文本触发（`DoS`、`exploit`、`credential`、`sandbox`、竞争品牌词等）。

排查顺序：
1. 开启 `--log`
2. 查看同一请求的 `REQUEST BODY` 或 `RESPONSES → CHAT BODY`
3. 查看 `RESPONSES PROJECTION` 统计
4. 开启 `--desensitize`
5. 仍不稳则尝试 `--desensitize --no-compact`

**响应慢**
→ 换快手模型如 `deepseek-v4-flash`

---

## Docker 部署

### docker compose（推荐）

修改 `docker-compose.yml` 中的 auth 挂载路径后：

```bash
docker compose up -d --build
```

容器会持久化 `.config/`（上传的授权文件、激活状态）到宿主机的 `./config`。

### docker run

```bash
docker build -t workbuddy2api .

# macOS
docker run -d --name workbuddy2api -p 8787:8787 \
  -v ~/Library/Application\ Support/CodeBuddyExtension/Data/Public/auth:/data/auth:ro \
  -v ./config:/app/.config \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  workbuddy2api
```

### 环境变量

| 变量 | 说明 |
|------|------|
| `CODEBUDDY_AUTH_DIR` | 登录态目录 |
| `CODEBUDDY2OPENAI_KEY` | 本地 API Key |
| `CODEBUDDY2OPENAI_LOG` | 日志文件路径 |

---

## 模型列表

`/v1/models` 优先从上游 `copilot.tencent.com/v2/models` 动态获取（缓存 5 分钟）。获取失败时回退到内置列表：

`glm-5.2` `glm-5.1` `glm-5v-turbo` `kimi-k2.7` `kimi-k2.6` `kimi-k2.5` `deepseek-v4-pro` `deepseek-v4-flash` `minimax-m3-pay` `hy3-preview-agent` `auto`

具体可用模型取决于你的 WorkBuddy / CodeBuddy 订阅。

---

## 项目结构

```text
workbuddy2api/
├── converter.py              # 主入口，FastAPI 服务
├── responses_adapter.py      # OpenAI Responses ↔ Chat 适配
├── responses_projection.py   # Codex / agent 请求投影压缩
├── anthropic_adapter.py      # Anthropic Messages ↔ Chat 适配
├── desensitize.py            # 运行时文本压缩与零宽脱敏
├── codex-codebuddy.example.toml
├── test_responses_adapter.py
├── test_anthropic_adapter.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── README.md
├── LICENSE
└── .config/                  # 上传的授权文件 + active.txt
```

---

## 致谢

本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。

## 免责声明

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

## 开源协议

[MIT](./LICENSE)
