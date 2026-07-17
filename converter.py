#!/usr/bin/env python3
"""
workbuddy2openai — 把 CodeBuddy / WorkBuddy 的订阅暴露成标准 OpenAI 兼容 API。

原理（直连后端，原生 function calling）：
  - 读取本机已登录的 CodeBuddy 桌面端凭据（auth 文件里的 token / uid / enterpriseId）。
  - 直接转发到 CodeBuddy 后端 `https://copilot.tencent.com/v2/chat/completions`。
    该后端本身就是标准 OpenAI chat/completions 协议（含原生 tools / tool_calls / SSE 流式）。
  - 转换器只做两件事：①注入鉴权 header（Authorization / X-User-Id 等）
    ②在本地 /v1/* 与后端 /v2/* 之间做路径映射与透传（含 Anthropic / Chat / Responses 三种协议）。
  - token 过期时自动调 `/v2/plugin/auth/token/refresh` 刷新，并回写 auth 文件。

跨平台：自动定位 auth 目录（macOS / Windows / Linux）。
依赖：fastapi + uvicorn + httpx（pip install fastapi "uvicorn[standard]" httpx）。

用法：
  python3 converter.py                       # 默认 127.0.0.1:8787
  python3 converter.py --port 9000
  python3 converter.py --api-key mysecret    # 启用客户端鉴权
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn

try:
    from desensitize import desensitize_body
except ImportError:  # 模块缺失时降级为不脱敏
    def desensitize_body(body, roles=("system",), desensitize_harness_user=False,
                         desensitize_tools=False, compact_harness=False,
                         strip_tool_metadata=False):
        return body

from responses_adapter import (
    responses_request_to_chat,
    ResponsesStreamConverter,
)
from responses_projection import project_responses_chat_body
from anthropic_adapter import (
    anthropic_request_to_chat,
    AnthropicStreamConverter,
)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"
USER_AGENT = "workbuddy2openai/2.0"

# ---------------------------------------------------------------------------
# 平台相关：定位 auth 目录
# ---------------------------------------------------------------------------

def auth_dirs() -> list[Path]:
    env_dir = os.environ.get("CODEBUDDY_AUTH_DIR")
    if env_dir:
        return [Path(env_dir)]
    home = Path.home()
    plat = sys.platform
    if plat == "darwin":
        return [home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [local / "CodeBuddyExtension" / "Data" / "Public" / "auth"]
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return [xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth"]


def _config_dir() -> Path:
    """获取程序 .config 目录（相对于 converter.py 所在目录）。"""
    return Path(__file__).resolve().parent / ".config"


def _active_auth_file() -> Path:
    """存储当前激活的授权文件名的标记文件。"""
    return _config_dir() / "active.txt"


def _get_active_auth_path() -> Path | None:
    """读取当前激活的授权文件路径。"""
    af = _active_auth_file()
    if af.is_file():
        try:
            content = af.read_text(encoding="utf-8").strip()
            if content:
                p = Path(content)
                if p.is_file():
                    return p
        except OSError:
            pass
    return None


def _set_active_auth(path: Path | str | None):
    """写入当前激活的授权文件路径。"""
    _config_dir().mkdir(parents=True, exist_ok=True)
    af = _active_auth_file()
    if path:
        af.write_text(str(path), encoding="utf-8")
    elif af.is_file():
        af.unlink()


def find_auth_file() -> Path | None:
    # 1) 优先使用显式激活的上传文件
    active = _get_active_auth_path()
    if active:
        return active

    # 2) 检查 .config 目录中的上传文件
    config_dir = _config_dir()
    if config_dir.is_dir():
        files = sorted(config_dir.glob("uploaded_*.info"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        if len(files) == 1:
            # 只有一个时自动激活
            _set_active_auth(files[0])
            return files[0]
        if len(files) > 1:
            # 多个但无激活标记，不自动选择，回退到系统 auth_dirs
            pass

    # 3) 回退到系统默认 auth 目录
    for d in auth_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.info")):
                return f
    return None

class CredentialManager:
    """从 auth 文件读取凭据；token 临近过期时自动刷新并回写。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._mtime: float = 0.0

    def _read_raw(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_if_stale(self):
        """若文件 mtime 变了（外部刷新过），重新加载缓存。"""
        try:
            mt = self.path.stat().st_mtime
        except OSError:
            return
        if self._cached is None or mt != self._mtime:
            self._cached = self._read_raw()
            self._mtime = mt

    def _session(self) -> dict:
        self._load_if_stale()
        if self._cached is None:
            raise RuntimeError(f"无法读取 auth 文件：{self.path}")
        return self._cached

    def _is_expired(self) -> bool:
        s = self._session()
        expires_at = (s.get("auth") or {}).get("expiresAt") or 0
        # 提前 60s 判定过期
        return time.time() * 1000 >= (expires_at - 60_000)

    def _refresh(self):
        """调后端刷新 token，写回 auth 文件与缓存。"""
        s = self._session()
        auth = s.get("auth") or {}
        headers = self._build_headers_from(auth, s.get("account") or {})
        headers["X-Refresh-Token"] = auth.get("refreshToken", "")
        headers["X-Auth-Refresh-Source"] = "plugin"
        url = f"{BACKEND}/v2/plugin/auth/token/refresh"
        try:
            with httpx.Client(timeout=15) as c:
                r = c.post(url, headers=headers, json={})
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"刷新 token 网络失败：{e}")
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(f"刷新 token 失败：{data.get('msg', data)}")
        new_auth = data["data"]
        # 继承部分字段
        new_auth["domain"] = new_auth.get("domain") or auth.get("domain")
        new_auth["lastRefreshTime"] = int(time.time() * 1000)
        # 计算 expiresAt（若后端没直接给）
        if not new_auth.get("expiresAt") and new_auth.get("expiresIn"):
            new_auth["expiresAt"] = int(time.time() * 1000) + new_auth["expiresIn"] * 1000
        if not new_auth.get("refreshExpiresAt") and new_auth.get("refreshExpiresIn"):
            new_auth["refreshExpiresAt"] = int(time.time() * 1000) + new_auth["refreshExpiresIn"] * 1000
        s["auth"] = new_auth
        # 原子写回
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self._cached = s
        self._mtime = self.path.stat().st_mtime

    def _build_headers_from(self, auth: dict, account: dict) -> dict:
        domain = auth.get("domain") or DEFAULT_DOMAIN
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {auth.get('accessToken','')}",
            "X-User-Id": account.get("uid", ""),
            "X-Enterprise-Id": account.get("enterpriseId", ""),
            "X-Tenant-Id": account.get("enterpriseId", ""),
            "X-Domain": domain,
            "User-Agent": USER_AGENT,
        }
        return h

    def get_headers(self) -> dict:
        """返回带最新 token 的后端请求 header；必要时先刷新。"""
        with self._lock:
            if self._is_expired():
                self._refresh()
            s = self._session()
            return self._build_headers_from(s.get("auth") or {}, s.get("account") or {})

    def summary(self) -> dict:
        s = self._session()
        auth = s.get("auth") or {}
        acct = s.get("account") or {}
        exp = auth.get("expiresAt", 0)
        return {
            "uid": acct.get("uid"),
            "nickname": acct.get("nickname"),
            "enterpriseName": acct.get("enterpriseName"),
            "token_expires_at": exp,
            "token_expired": self._is_expired(),
        }


# ---------------------------------------------------------------------------
# 模型列表
# ---------------------------------------------------------------------------

DEFAULT_MODELS = [
    "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "kimi-k2.7", "kimi-k2.6", "kimi-k2.5",
    "deepseek-v4-pro", "deepseek-v4-flash",
    "minimax-m3-pay", "hy3-preview-agent", "auto",
]

# 后端请求体里出现过的额外字段（透传时若客户端给了就保留）
PASSTHROUGH_BODY_KEYS = {
    "model", "messages", "tools", "tool_choice", "temperature",
    "max_tokens", "max_completion_tokens", "top_p", "stream",
    "stream_options", "stop", "presence_penalty", "frequency_penalty",
    "n", "response_format", "seed", "user", "reasoning_effort",
    "verbosity", "reasoning_summary",
}

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="workbuddy2openai", version="2.0")
CONFIG: dict = {"api_key": "", "cred": None, "log_path": None,
                "desensitize": False, "no_compact": False,
                "models_cache": None, "models_cache_ts": 0}  # cred: CredentialManager | None


# ---------------------------------------------------------------------------
# 日志（写文件）
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()


def _log(msg: str):
    """写一行日志到 CONFIG['log_path'] 指定的文件（追加，带时间戳）。未设置则丢弃。"""
    path = CONFIG.get("log_path")
    if not path:
        return
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    try:
        with _LOG_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass  # 日志失败不应影响主流程




def _truncate(s: str, n: int = 80) -> str:
    s = str(s).replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _check_auth(authorization: Optional[str], x_api_key: Optional[str]):
    key = CONFIG["api_key"]
    if not key:
        return
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    if not token and x_api_key:
        token = x_api_key
    if token != key:
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key", "type": "auth_error"}})


def _cred() -> CredentialManager:
    cred = CONFIG.get("cred")
    # 解析当前应使用的授权文件
    af = find_auth_file()
    if af is not None:
        if cred is None or str(cred.path) != str(af):
            cred = CredentialManager(af)
            CONFIG["cred"] = cred
    if cred is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "未找到登录凭据，请先在桌面端登录 CodeBuddy/WorkBuddy", "type": "auth_error"}})
    return cred


@app.get("/health")
def health():
    cred = CONFIG["cred"]
    info: dict = {"status": "ok", "platform": sys.platform, "python": sys.version.split()[0],
                  "auth_file": str(find_auth_file() or "(未找到)"), "mode": "direct-proxy (native function calling)"}
    if cred is not None:
        try:
            info["credential"] = cred.summary()
        except Exception as e:
            info["credential_error"] = str(e)
    return info


@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(default=None),
                x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)

    # 尝试从上游获取真实模型列表（缓存 5 分钟）
    now = time.time()
    cache = CONFIG.get("models_cache")
    cache_ts = CONFIG.get("models_cache_ts", 0)
    if cache and (now - cache_ts) < 300:
        return cache

    cred = CONFIG.get("cred")
    data = []
    if cred is not None:
        try:
            headers = cred.get_headers()
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{BACKEND}/v2/models", headers=headers)
            if r.status_code == 200:
                upstream = r.json()
                items = upstream.get("data") or upstream.get("models") or []
                data = [
                    {"id": m.get("id") or m.get("name") or str(m),
                     "object": "model", "created": m.get("created", 1700000000),
                     "owned_by": m.get("owned_by", "codebuddy")}
                    for m in items
                ]
        except Exception:
            pass

    # 上游获取失败时回退到静态列表
    if not data:
        data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "codebuddy"}
                for m in DEFAULT_MODELS]

    result = {"object": "list", "data": data}
    CONFIG["models_cache"] = result
    CONFIG["models_cache_ts"] = now
    return result


@app.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    # 构造后端 body：只透传已知的合法字段
    client_wants_stream = bool(payload.get("stream"))
    body = {k: payload[k] for k in PASSTHROUGH_BODY_KEYS if k in payload}
    body.setdefault("model", "auto")
    # 后端只支持流式：始终以 stream=True 调后端，非流式由转换器聚合
    body["stream"] = True
    if "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    # 可选：脱敏。缓解客户端合规模板（如 Codex CLI / ZCode 注入的说明文字）被后端误判为敏感词。
    # 处理 system / developer 消息、Codex 注入的上下文 user 消息，以及 tools 的 description。
    if CONFIG.get("desensitize"):
        body = desensitize_body(body, roles=("system", "developer"),
                                desensitize_harness_user=True,
                                desensitize_tools=True,
                                compact_harness=not CONFIG.get("no_compact"),
                                strip_tool_metadata=True)

    # 日志：请求摘要
    model_name = payload.get("model", "auto")
    tool_names = [t.get("function", {}).get("name") for t in (payload.get("tools") or [])
                  if isinstance(t, dict)]
    last_user = _last_user_text(messages)
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ REQUEST {model_name} | stream={client_wants_stream} | msgs={len(messages)}"
         + (f" | tools={tool_names}" if tool_names else "")
         + (f" | last_user={_truncate(last_user, 60)!r}" if last_user else ""))
    # 完整请求体（发往后端的实际内容；若启用脱敏，这里已是脱敏后）
    _log(f"[{rid}] ── REQUEST BODY (发往后端) ──\n{json.dumps(body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_upstream(url, headers, body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：后端只支持流式，这里把后端 SSE 聚合成单个 chat.completion 响应
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    raw = await r.aread()
                    _log(f"[{rid}] ✗ HTTP {r.status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
                    _log(f"[{rid}] ── ERROR BODY ──\n{raw.decode('utf-8','replace')}")
                    raise HTTPException(status_code=r.status_code, detail=_safe_err_raw(raw, r.status_code))
                collected = await _collect_stream(r)
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})
    _log_finish(model_name, t0, collected, rid)
    return JSONResponse(content=collected)


def _last_user_text(messages: list) -> str:
    """取最后一条 user 消息的文本，用于日志预览。"""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    return str(blk.get("text", ""))
            return ""
        return str(content)
    return ""


def _log_finish(model_name: str, t0: float, result: dict, rid: str = ""):
    """记录一次完成的请求：耗时 / finish_reason / usage / 工具调用 / 审核拦截 + 完整响应。"""
    elapsed = time.time() - t0
    prefix = f"[{rid}] " if rid else ""
    choice = (result.get("choices") or [{}])[0]
    finish = choice.get("finish_reason")
    msg = choice.get("message") or {}
    tcs = msg.get("tool_calls") or []
    usage = result.get("usage") or {}
    tag = ""
    if finish == "content-filter":
        tag = " ⚠️内容审核拦截"
    tc_names = [t.get("function", {}).get("name") for t in tcs]
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | finish={finish}{tag}"
         + (f" | tool_calls={tc_names}" if tc_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整响应体
    _log(f"{prefix}── RESPONSE BODY ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")


async def _collect_stream(response: httpx.Response) -> dict:
    """消费后端的 OpenAI SSE 流，聚合成单个非流式 chat.completion 对象。

    合并所有 chunk 的 delta（content / tool_calls），并取 usage / finish_reason。
    """
    content_parts: list[str] = []
    # tool_calls: index -> {id, name, arguments(分片拼接)}
    tool_calls: dict[int, dict] = {}
    model: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None

    async for line in response.aiter_lines():
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        model = chunk.get("model") or model
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

    tcs = None
    if tool_calls:
        tcs = [
            {"id": v["id"], "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"]}}
            for _, v in sorted(tool_calls.items())
        ]
        finish_reason = finish_reason or "tool_calls"

    message = {"role": "assistant", "content": "".join(content_parts) or None}
    if tcs:
        message["tool_calls"] = tcs
    return {
        "id": "chatcmpl-" + os.urandom(12).hex(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "unknown",
        "choices": [{"index": 0, "message": message,
                     "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _safe_err_raw(raw: bytes, status: int) -> dict:
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"error": {"message": raw.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status}}


async def _stream_upstream(url: str, headers: dict, body: dict,
                           model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """把后端 SSE 原样转发给客户端（后端已是标准 OpenAI SSE，含 tool_calls）。

    同时轻量解析流，统计 finish_reason / tool_calls / usage 用于日志，不阻塞转发。
    完整原始 SSE 累积后落盘到日志（调试用）。
    """
    finish_reason = None
    tool_names: list[str] = []
    usage: dict = {}
    saw_filter = False
    buf = b""
    raw_parts: list[bytes] = []   # 累积完整原始 SSE
    prefix = f"[{rid}] " if rid else ""

    def _feed(chunk: bytes):
        nonlocal finish_reason, saw_filter, buf
        # 行缓冲解析：把累计的 chunk 按 data: 行切出来统计
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
                for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                    nm = (tc.get("function") or {}).get("name")
                    if nm:
                        tool_names.append(nm)
            # 内容审核拦截常以 content-filter 或特殊中文文案返回
            try:
                text_repr = data.decode("utf-8", "replace")
            except Exception:
                text_repr = ""
            if "content-filter" in text_repr or "敏感" in text_repr or "审核" in text_repr:
                saw_filter = True

    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    _log(f"{prefix}── ERROR BODY ──\n{err.decode('utf-8','replace')}")
                    yield _err_event(err, r.status_code)
                    return
                async for chunk in r.aiter_bytes():
                    if chunk:
                        raw_parts.append(chunk)
                        _feed(chunk)
                        yield chunk
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        yield _err_event(str(e).encode(), 502)

    # 流结束：输出完成日志
    elapsed = time.time() - t0 if t0 else 0
    tag = " ⚠️内容审核拦截" if (saw_filter or finish_reason == "content-filter") else ""
    _log(f"{prefix}◀ RESPONSE {model_name} | {elapsed:.1f}s | stream finish={finish_reason}{tag}"
         + (f" | tool_calls={tool_names}" if tool_names else "")
         + f" | tokens={usage.get('total_tokens', '?')}")
    # 完整原始 SSE（后端返回的全部内容）
    _log(f"{prefix}── RESPONSE RAW SSE ──\n{b''.join(raw_parts).decode('utf-8','replace')}")


def _safe_err(r: httpx.Response) -> dict:
    try:
        return {"error": r.json()}
    except Exception:
        return {"error": {"message": r.text[:500], "type": "upstream_error", "code": r.status_code}}


def _err_event(msg: bytes, status: int) -> bytes:
    # 以 OpenAI SSE 错误 chunk 形式返回
    import json as _json, time as _time
    chunk = {
        "error": {"message": msg.decode("utf-8", "replace")[:500], "type": "upstream_error", "code": status},
    }
    return f"data: {_json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


def _looks_like_content_filter_text(text: str) -> bool:
    text = (text or "").lower()
    return (
        "content-filter" in text
        or "content_filter" in text
        or "敏感内容" in text
        or "内容审核" in text
        or "无法响应您的请求" in text
    )


def _chat_body_desensitize(body: dict, *, force_compact: bool = False) -> dict:
    if not CONFIG.get("desensitize"):
        return body
    return desensitize_body(
        body,
        roles=("system", "developer"),
        desensitize_harness_user=True,
        desensitize_tools=True,
        compact_harness=(force_compact or not CONFIG.get("no_compact")),
        strip_tool_metadata=True,
    )


async def _post_backend_once(url: str, headers: dict, body: dict) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=120) as c:
        async with c.stream("POST", url, headers=headers, json=body) as r:
            chunks: list[bytes] = []
            async for chunk in r.aiter_bytes():
                if chunk:
                    chunks.append(chunk)
            return r.status_code, b"".join(chunks)


async def _post_backend_with_filter_retry(url: str, headers: dict, body: dict,
                                          rid: str = "", model_name: str = "?") -> tuple[int, bytes, dict]:
    prefix = f"[{rid}] " if rid else ""
    status, raw = await _post_backend_once(url, headers, body)
    text = raw.decode("utf-8", "replace")
    if status == 200 and _looks_like_content_filter_text(text) and CONFIG.get("desensitize") and CONFIG.get("no_compact"):
        retry_body = _chat_body_desensitize(body, force_compact=True)
        _log(f"{prefix}↻ RESPONSES {model_name} | content filter detected, retry with compact harness")
        _log(f"{prefix}── RESPONSES RETRY CHAT BODY ──\n{json.dumps(retry_body, ensure_ascii=False, indent=2)}")
        retry_status, retry_raw = await _post_backend_once(url, headers, retry_body)
        retry_text = retry_raw.decode("utf-8", "replace")
        if retry_status == 200 and not _looks_like_content_filter_text(retry_text):
            return retry_status, retry_raw, retry_body
    return status, raw, body


# ---------------------------------------------------------------------------
# Responses API 端点（Codex CLI 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/responses")
async def create_response(request: Request,
                          authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """OpenAI Responses API 兼容端点。

    Codex CLI 使用 Responses API（wire_api = "responses"）而非 Chat Completions。
    本端点接收 Responses 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Responses 语义事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 转换请求：Responses → Chat
    try:
        chat_body = responses_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body, projection_stats = project_responses_chat_body(chat_body)
    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    chat_body = _chat_body_desensitize(chat_body)

    client_wants_stream = payload.get("stream", True)  # Codex CLI 默认 stream
    model_name = payload.get("model", "auto")
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ RESPONSES {model_name} | stream={client_wants_stream} | input_items={len(payload.get('input', []))}")
    _log(
        f"[{rid}] ── RESPONSES PROJECTION ── "
        f"mode={projection_stats.get('mode')} "
        f"| msgs {projection_stats.get('original_messages')}→{projection_stats.get('projected_messages')} "
        f"| chars {projection_stats.get('original_message_chars')}→{projection_stats.get('projected_message_chars')} "
        f"| tools {projection_stats.get('original_tools')}→{projection_stats.get('projected_tools')} "
        f"| tool_chars {projection_stats.get('original_tool_chars')}→{projection_stats.get('projected_tool_chars')} "
        f"| summarized_history={projection_stats.get('summarized_history_messages', 0)} "
        f"| dropped_harness={projection_stats.get('dropped_harness_messages', 0)} "
        f"| anchor_user={projection_stats.get('anchor_user_preserved', False)}"
    )
    _log(f"[{rid}] ── RESPONSES → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    if client_wants_stream:
        return StreamingResponse(
            _stream_responses(url, headers, chat_body, model_name, t0, rid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非流式：聚合后端 SSE → 非流式 Response 对象
    try:
        status_code, raw, final_body = await _post_backend_with_filter_retry(url, headers, chat_body, rid, model_name)
        if status_code != 200:
            _log(f"[{rid}] ✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            raise HTTPException(status_code=status_code, detail=_safe_err_raw(raw, status_code))
        converter = ResponsesStreamConverter(model=model_name)
        for line in raw.decode("utf-8", "replace").splitlines():
            converter.feed_line(line)
        chat_body = final_body
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        _log(f"[{rid}] ✗ 网络错误 | {model_name} | {e}")
        raise HTTPException(status_code=502, detail={"error": {"message": f"upstream error: {e}", "type": "upstream_error"}})

    result = converter.get_nonstream_response()
    elapsed = time.time() - t0
    _log(f"[{rid}] ◀ RESPONSES {model_name} | {elapsed:.1f}s")
    _log(f"[{rid}] ── RESPONSE OBJ ──\n{json.dumps(result, ensure_ascii=False, indent=2)}")
    return JSONResponse(content=result)


async def _stream_responses(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 Chat SSE，实时转换为 Responses API 事件流输出。"""
    converter = ResponsesStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        status_code, raw, _ = await _post_backend_with_filter_retry(url, headers, body, rid, model_name)
        if status_code != 200:
            _log(f"{prefix}✗ HTTP {status_code} | {model_name} | {_truncate(raw.decode('utf-8','replace'),200)}")
            error_evt = {"type": "error", "error": {"message": raw.decode('utf-8','replace')[:500], "code": status_code}}
            yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
            return
        raw_sse_lines = []
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.strip():
                raw_sse_lines.append(line)
            events = converter.feed_line(line)
            if events:
                yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "code": 502}}
        yield f"data: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    # 发送收尾事件
    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ RESPONSES {model_name} | {elapsed:.1f}s | stream done")
    _log(f"{prefix}── RESPONSES RAW SSE ──\n" + "\n".join(raw_sse_lines[-30:]))


# ---------------------------------------------------------------------------
# Anthropic Messages API 端点（Claude Code / CC Switch 兼容）
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def create_message(request: Request,
                         authorization: Optional[str] = Header(default=None),
                         x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic Messages API 兼容端点。

    Claude Code / CC Switch 使用 Anthropic Messages API（POST /v1/messages）。
    本端点接收 Anthropic 格式请求，转换为 Chat 格式发往后端，再将后端的 Chat SSE
    转换为 Anthropic SSE 事件流返回。
    """
    _check_auth(authorization, x_api_key)
    cred = _cred()

    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"bad json: {e}", "type": "invalid_request_error"}})

    # 将 Anthropic 格式消息、工具规范在进入后端前统一转换为 OpenAI Chat 格式。
    messages = payload.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail={"error": {"message": "messages is required", "type": "invalid_request_error"}})

    try:
        chat_body = anthropic_request_to_chat(payload)
    except Exception as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"request conversion error: {e}", "type": "invalid_request_error"}})

    chat_body.setdefault("model", "auto")
    chat_body["stream"] = True
    if "stream_options" not in chat_body:
        chat_body["stream_options"] = {"include_usage": True}

    if CONFIG.get("desensitize"):
        chat_body = desensitize_body(chat_body, roles=("system", "developer"),
                                     desensitize_harness_user=True,
                                     desensitize_tools=True,
                                     compact_harness=not CONFIG.get("no_compact"),
                                     strip_tool_metadata=True)

    model_name = payload.get("model", "auto")
    chat_messages = chat_body.get("messages", [])
    rid = os.urandom(4).hex()
    _log(f"[{rid}] ▶ ANTHROPIC {model_name} | msgs={len(chat_messages)} | anthropic_msgs={len(messages)}")
    _log(f"[{rid}] ── ANTHROPIC → CHAT BODY ──\n{json.dumps(chat_body, ensure_ascii=False, indent=2)}")

    headers = cred.get_headers()
    url = f"{BACKEND}/v2/chat/completions"
    t0 = time.time()

    return StreamingResponse(
        _stream_anthropic(url, headers, chat_body, model_name, t0, rid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_anthropic(url: str, headers: dict, body: dict,
                            model_name: str = "?", t0: float = 0.0, rid: str = ""):
    """消费后端 OpenAI Chat SSE，实时转换为 Anthropic Messages SSE 事件流。"""
    converter = AnthropicStreamConverter(model=model_name)
    prefix = f"[{rid}] " if rid else ""

    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code != 200:
                    err = await r.aread()
                    _log(f"{prefix}✗ HTTP {r.status_code} | {model_name} | {_truncate(err.decode('utf-8','replace'),200)}")
                    error_evt = {"type": "error", "error": {"message": err.decode('utf-8','replace')[:500], "type": "api_error", "code": r.status_code}}
                    yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
                    return
                async for line in r.aiter_lines():
                    events = converter.feed_line(line)
                    if events:
                        yield events.encode("utf-8")
    except httpx.HTTPError as e:
        _log(f"{prefix}✗ 网络错误 | {model_name} | {e}")
        error_evt = {"type": "error", "error": {"message": str(e)[:500], "type": "api_error", "code": 502}}
        yield f"event: error\ndata: {json.dumps(error_evt, ensure_ascii=False)}\n\n".encode("utf-8")
        return

    finish_events = converter.finish()
    if finish_events:
        yield finish_events.encode("utf-8")

    elapsed = time.time() - t0 if t0 else 0
    _log(f"{prefix}◀ ANTHROPIC {model_name} | {elapsed:.1f}s | stream done")


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request,
                       authorization: Optional[str] = Header(default=None),
                       x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """Anthropic token 计数端点（stub）。

    Claude Code 可能在发送消息前调用此端点。
    返回一个简单估算值，不做实际 token 计数。
    """
    _check_auth(authorization, x_api_key)
    return {"input_tokens": 0}


# ---------------------------------------------------------------------------
# 管理页面（根路由）
# ---------------------------------------------------------------------------

ROOT_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>workbuddy2api — 管理面板</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         max-width: 860px; margin: 2em auto; padding: 0 1em; line-height: 1.6; }
  h1 { font-size: 1.5em; margin-bottom: 0.2em; }
  .sub { color: #888; font-size: 0.9em; margin-bottom: 1.5em; }
  .card { border: 1px solid #ccc; border-radius: 8px; padding: 1em 1.2em; margin-bottom: 1.2em; }
  .card h2 { margin-top: 0; font-size: 1.1em; }
  .ok { color: #2da44e; } .err { color: #cf222e; } .warn { color: #d4a72c; }
  table { border-collapse: collapse; width: 100%; }
  td { padding: 4px 0; }
  td:first-child { color: #666; width: 10em; }
  .models { display: flex; flex-wrap: wrap; gap: 6px; }
  .models span { background: #eee; border-radius: 4px; padding: 2px 8px; font-size: 0.85em; }
  input[type=file] { margin: 8px 0; }
  button { padding: 4px 12px; cursor: pointer; border: 1px solid #888; border-radius: 4px; background: #f0f0f0; }
  button:hover { background: #ddd; }
  button.danger { border-color: #cf222e; color: #cf222e; }
  button.danger:hover { background: #fee; }
  .msg { margin-top: 8px; font-size: 0.9em; }
  .auth-table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  .auth-table td, .auth-table th { padding: 6px 8px; border-bottom: 1px solid #eee; text-align: left; font-size: 0.9em; }
  .auth-table th { color: #888; font-weight: normal; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 0.8em; }
  .badge-ok { background: #dafbe1; color: #1a7f37; }
  .badge-err { background: #ffebe9; color: #cf222e; }
  .badge-warn { background: #fff8c5; color: #9a6700; }
  .btn-sm { font-size: 0.8em; padding: 2px 8px; }
  @media (prefers-color-scheme: dark) {
    body { background: #111; color: #ddd; }
    .card { border-color: #444; }
    .models span { background: #333; }
    td:first-child, .auth-table th { color: #aaa; }
    button { background: #333; border-color: #555; color: #ddd; }
    button:hover { background: #444; }
    button.danger:hover { background: #422; }
    .auth-table td, .auth-table th { border-color: #333; }
    .badge-ok { background: #1a3d2b; color: #57ab5a; }
    .badge-err { background: #3d1a1f; color: #f47067; }
    .badge-warn { background: #3d3100; color: #d4a72c; }
  }
</style>
</head>
<body>
<h1>WorkBuddy2API</h1>
<div class="sub">CodeBuddy / WorkBuddy → OpenAI 兼容 API 转换器</div>

<div class="card">
  <h2>健康状态</h2>
  <div id="health">加载中…</div>
</div>

<div class="card">
  <h2>上传授权文件</h2>
  <p style="font-size:0.85em;color:#888;">上传 CodeBuddy 的 <code>*.info</code> 授权文件。上传后自动激活。</p>
  <input type="file" id="authFile" accept=".info">
  <button onclick="uploadAuth()">上传</button>
  <div class="msg" id="uploadMsg"></div>
</div>

<div class="card">
  <h2>授权文件管理</h2>
  <div id="authList">加载中…</div>
  <div class="msg" id="authMsg"></div>
</div>

<div class="card">
  <h2>模型列表</h2>
  <div class="models" id="models">加载中…</div>
</div>

<script>
const $ = (id) => document.getElementById(id);

async function loadHealth() {
  try {
    const r = await fetch('/health');
    const data = await r.json();
    let cred = data.credential || {};
    let html = '<table>';
    html += `<tr><td>状态</td><td><span class="ok">${data.status}</span></td></tr>`;
    html += `<tr><td>平台</td><td>${data.platform}</td></tr>`;
    html += `<tr><td>Python</td><td>${data.python}</td></tr>`;
    html += `<tr><td>后端</td><td>直连 (copilot.tencent.com)</td></tr>`;
    html += `<tr><td>当前授权</td><td>${data.auth_file || '(未找到)'}</td></tr>`;
    if (data.credential) {
      html += `<tr><td>账号</td><td>${cred.nickname || '-'} / ${cred.enterpriseName || '-'}</td></tr>`;
      html += `<tr><td>UID</td><td>${cred.uid || '-'}</td></tr>`;
      html += `<tr><td>Token 状态</td><td><span class="${cred.token_expired ? 'err' : 'ok'}">${cred.token_expired ? '已过期（将自动刷新）' : '有效'}</span></td></tr>`;
    } else if (data.credential_error) {
      html += `<tr><td>凭据错误</td><td><span class="err">${data.credential_error}</span></td></tr>`;
    }
    html += '</table>';
    $('health').innerHTML = html;
  } catch(e) {
    $('health').innerHTML = `<span class="err">加载失败: ${e}</span>`;
  }
}

async function loadModels() {
  try {
    const r = await fetch('/v1/models');
    const data = await r.json();
    let html = '';
    for (const m of data.data || []) {
      html += `<span>${m.id}</span>`;
    }
    $('models').innerHTML = html || '<span class="warn">无模型</span>';
  } catch(e) {
    $('models').innerHTML = `<span class="err">加载失败: ${e}</span>`;
  }
}

async function loadAuthFiles() {
  try {
    const r = await fetch('/auth-files');
    const data = await r.json();
    let html = '';
    const files = data.files || [];
    if (files.length === 0) {
      html = '<span style="color:#888;">暂无上传的授权文件</span>';
      if (data.system_auth_file) {
        html += `<br><span style="font-size:0.85em;color:#888;">当前使用系统默认授权: ${data.system_auth_file}</span>`;
      }
    } else {
      html = '<table class="auth-table"><tr><th></th><th>账号</th><th>UID</th><th>Token</th><th>状态</th><th>操作</th></tr>';
      for (const f of files) {
        let statusBadge = '';
        if (f.active) {
          statusBadge = '<span class="badge badge-ok">当前使用</span>';
        } else if (!f.token_valid) {
          statusBadge = '<span class="badge badge-err">无效</span>';
        } else {
          statusBadge = '<span class="badge badge-warn">待激活</span>';
        }
        let tokenBadge = f.token_valid
          ? '<span class="badge badge-ok">有效</span>'
          : '<span class="badge badge-err">' + (f.error || '过期/无效') + '</span>';
        let actions = '';
        if (!f.active) {
          actions += `<button class="btn-sm" onclick="activateAuth('${f.filename}')">激活</button> `;
        }
        actions += `<button class="btn-sm danger" onclick="deleteAuth('${f.filename}')">删除</button>`;
        html += `<tr>
          <td>${f.filename}</td>
          <td>${f.nickname || '-'}</td>
          <td>${f.uid || '-'}</td>
          <td>${tokenBadge}</td>
          <td>${statusBadge}</td>
          <td>${actions}</td>
        </tr>`;
      }
      html += '</table>';
      if (data.system_auth_file) {
        html += `<div style="margin-top:8px;font-size:0.8em;color:#888;">系统默认: ${data.system_auth_file}</div>`;
      }
    }
    $('authList').innerHTML = html;
  } catch(e) {
    $('authList').innerHTML = `<span class="err">加载失败: ${e}</span>`;
  }
}

async function uploadAuth() {
  const file = $('authFile').files[0];
  const msg = $('uploadMsg');
  if (!file) { msg.innerHTML = '<span class="err">请先选择文件</span>'; return; }
  const form = new FormData();
  form.append('file', file);
  msg.innerHTML = '上传中…';
  try {
    const r = await fetch('/upload-auth', { method: 'POST', body: form });
    const data = await r.json();
    if (r.ok) {
      msg.innerHTML = `<span class="ok">✓ ${data.message}</span>`;
      $('authFile').value = '';
      loadHealth();
      loadAuthFiles();
    } else {
      msg.innerHTML = `<span class="err">✗ ${data.detail?.error?.message || data.detail || JSON.stringify(data)}</span>`;
    }
  } catch(e) {
    msg.innerHTML = `<span class="err">上传失败: ${e}</span>`;
  }
}

async function activateAuth(filename) {
  try {
    const r = await fetch('/auth-files/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filename})
    });
    const data = await r.json();
    if (r.ok) {
      loadHealth();
      loadAuthFiles();
    } else {
      $('authMsg').innerHTML = `<span class="err">✗ ${data.detail?.error?.message || JSON.stringify(data)}</span>`;
    }
  } catch(e) {
    $('authMsg').innerHTML = `<span class="err">激活失败: ${e}</span>`;
  }
}

async function deleteAuth(filename) {
  if (!confirm(`确定删除 ${filename} 吗？`)) return;
  try {
    const r = await fetch('/auth-files/delete', {
      method: 'DELETE',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({filename})
    });
    const data = await r.json();
    if (r.ok) {
      loadHealth();
      loadAuthFiles();
    } else {
      $('authMsg').innerHTML = `<span class="err">✗ ${data.detail?.error?.message || JSON.stringify(data)}</span>`;
    }
  } catch(e) {
    $('authMsg').innerHTML = `<span class="err">删除失败: ${e}</span>`;
  }
}

loadHealth();
loadModels();
loadAuthFiles();
</script>
</body>
</html>"""


@app.get("/")
async def root():
    return HTMLResponse(content=ROOT_HTML)


@app.post("/upload-auth")
async def upload_auth(file: UploadFile = File(...),
                      authorization: Optional[str] = Header(default=None),
                      x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """上传授权文件，程序将使用该文件替代默认路径查找凭据。"""
    _check_auth(authorization, x_api_key)
    if not file.filename or not file.filename.endswith(".info"):
        raise HTTPException(status_code=400, detail={"error": {"message": "仅支持 .info 格式的授权文件", "type": "invalid_request_error"}})
    try:
        content = await file.read()
        data = json.loads(content.decode("utf-8"))
        if "auth" not in data and "account" not in data:
            raise ValueError("文件格式不正确，缺少 auth/account 字段")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail={"error": {"message": f"文件格式无效: {e}", "type": "invalid_request_error"}})

    # 检查是否已存在相同 UID 的授权文件，有则替换
    new_uid = (data.get("account") or {}).get("uid") or (data.get("auth") or {}).get("uid") or ""
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    replaced: Path | None = None
    if new_uid:
        for existing in sorted(config_dir.glob("uploaded_*.info")):
            try:
                ed = json.loads(existing.read_bytes().decode("utf-8"))
                euid = (ed.get("account") or {}).get("uid") or (ed.get("auth") or {}).get("uid") or ""
                if euid and euid == new_uid:
                    replaced = existing
                    break
            except Exception:
                pass

    if replaced:
        saved_path = replaced
        action = "替换"
    else:
        saved_path = config_dir / f"uploaded_{os.urandom(6).hex()}.info"
        action = "新增"
    with open(saved_path, "wb") as f:
        f.write(content)

    # 自动激活新上传的文件
    _set_active_auth(saved_path)
    CONFIG["cred"] = CredentialManager(saved_path)

    info = CONFIG["cred"].summary()
    _log(f"▲ {action}授权文件: {saved_path} | uid={info.get('uid')} | nickname={info.get('nickname')}")
    return {
        "message": f"授权文件已{action}，账号: {info.get('nickname') or info.get('uid')}",
        "credential": info,
        "path": str(saved_path),
    }


# ---------------------------------------------------------------------------
# 授权文件管理
# ---------------------------------------------------------------------------

@app.get("/auth-files")
async def list_auth_files(authorization: Optional[str] = Header(default=None),
                          x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """列出 .config 中所有授权文件及其状态。"""
    _check_auth(authorization, x_api_key)
    active = _get_active_auth_path()
    config_dir = _config_dir()
    files = []
    if config_dir.is_dir():
        for f in sorted(config_dir.glob("uploaded_*.info"), key=lambda x: x.stat().st_mtime, reverse=True):
            info: dict = {
                "filename": f.name,
                "path": str(f),
                "active": active is not None and str(f) == str(active),
                "mtime": f.stat().st_mtime,
            }
            # 尝试读取账号信息和 token 状态
            try:
                cm = CredentialManager(f)
                s = cm.summary()
                info["nickname"] = s.get("nickname") or "-"
                info["uid"] = s.get("uid") or "-"
                info["enterpriseName"] = s.get("enterpriseName") or "-"
                info["token_expired"] = s.get("token_expired", True)
                info["token_valid"] = not s.get("token_expired", True)
            except Exception:
                info["token_valid"] = False
                info["error"] = "无法读取凭据"
            files.append(info)
    # 也检查系统 auth 目录（作为参考）
    sys_file = None
    for d in auth_dirs():
        if d.is_dir():
            for f in sorted(d.glob("*.info")):
                sys_file = str(f)
                break
    return {
        "files": files,
        "active": str(active) if active else None,
        "system_auth_file": sys_file,
    }


@app.post("/auth-files/activate")
async def activate_auth_file(request: Request,
                             authorization: Optional[str] = Header(default=None),
                             x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """激活指定授权文件。"""
    _check_auth(authorization, x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    filename = (body or {}).get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 filename 参数", "type": "invalid_request_error"}})
    target = _config_dir() / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail={"error": {"message": f"文件不存在: {filename}", "type": "invalid_request_error"}})
    _set_active_auth(target)
    CONFIG["cred"] = CredentialManager(target)
    info = CONFIG["cred"].summary()
    _log(f"▲ 激活授权文件: {target} | uid={info.get('uid')} | nickname={info.get('nickname')}")
    return {"message": f"已激活: {info.get('nickname') or info.get('uid')}", "filename": filename, "active": str(target)}


@app.delete("/auth-files/delete")
async def delete_auth_file(request: Request,
                           authorization: Optional[str] = Header(default=None),
                           x_api_key: Optional[str] = Header(default=None, alias="X-Api-Key")):
    """删除指定授权文件。不能删除当前激活的文件。"""
    _check_auth(authorization, x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": {"message": "bad json", "type": "invalid_request_error"}})
    filename = (body or {}).get("filename")
    if not filename:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 filename 参数", "type": "invalid_request_error"}})
    target = _config_dir() / filename
    if not target.is_file():
        raise HTTPException(status_code=404, detail={"error": {"message": f"文件不存在: {filename}", "type": "invalid_request_error"}})
    active = _get_active_auth_path()
    if active and str(target) == str(active):
        raise HTTPException(status_code=400, detail={"error": {"message": "不能删除当前激活的授权文件，请先激活其他文件", "type": "invalid_request_error"}})
    target.unlink()
    _log(f"▲ 删除授权文件: {target}")
    return {"message": f"已删除: {filename}"}


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def preflight() -> bool:
    af = find_auth_file()
    sys.stderr.write("==== 预检 ====\n")
    sys.stderr.write(f"平台      : {sys.platform}\n")
    sys.stderr.write(f"Python    : {sys.version.split()[0]}\n")
    sys.stderr.write(f"后端      : {BACKEND} (直连，原生 function calling)\n")
    sys.stderr.write(f"登录文件  : {af or '(未找到)'}\n")
    if auth_dirs():
        sys.stderr.write(f"已查目录  : {', '.join(str(d) for d in auth_dirs())}\n")
    ok = True
    if af is None:
        sys.stderr.write("\n[警告] 未找到登录文件。请在桌面端完成登录（CodeBuddy/WorkBuddy）。\n")
        ok = False
    else:
        try:
            cm = CredentialManager(af)
            info = cm.summary()
            sys.stderr.write(f"账号      : {info.get('nickname')} / {info.get('enterpriseName')}\n")
            sys.stderr.write(f"token过期 : {'是(将自动刷新)' if info['token_expired'] else '否'}\n")
        except Exception as e:
            sys.stderr.write(f"[警告] 读取凭据失败：{e}\n")
            ok = False
    sys.stderr.write("================\n")
    return ok


def main():
    ap = argparse.ArgumentParser(description="CodeBuddy -> OpenAI 兼容转换器（直连后端）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
                    help="可选：要求客户端携带的 API key（默认不校验）")
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="开启日志并写到该文件（如 --log converter.log 或 --log /tmp/cb.log）。"
                         "不传则不记日志。")
    ap.add_argument("--desensitize", action="store_true",
                    help="启用脱敏：对 system 消息里的合规模板敏感词（DoS/exploit/credential 等）"
                         "插入零宽空格，缓解被后端内容审核误拦。默认关闭。")
    ap.add_argument("--no-compact", action="store_true",
                    help="配合 --desensitize 使用：跳过 system/harness 压缩，仅做零宽脱敏。"
                         "保留原始 system prompt 完整内容（如 Claude Code 的行为指令），"
                         "但审核误拦风险略高于默认压缩模式。")
    ap.add_argument("--skip-check", action="store_true", help="跳过启动预检")
    args = ap.parse_args()

    CONFIG["api_key"] = args.api_key
    CONFIG["desensitize"] = args.desensitize
    CONFIG["no_compact"] = args.no_compact
    # --log 直接指定文件路径即开启；不传则不记
    CONFIG["log_path"] = args.log if args.log else os.environ.get("CODEBUDDY2OPENAI_LOG")
    af = find_auth_file()
    CONFIG["cred"] = CredentialManager(af) if af else None

    if not args.skip_check:
        preflight()

    sys.stderr.write(f"\n✅ 监听 http://{args.host}:{args.port}（直连后端，原生 function calling）\n")
    sys.stderr.write("   GET  /                        管理面板\n")
    sys.stderr.write("   GET  /v1/models\n")
    sys.stderr.write("   POST /v1/chat/completions   (原生 tools/tool_calls，支持流式)\n")
    sys.stderr.write("   POST /v1/responses          (Responses API，Codex CLI 兼容)\n")
    sys.stderr.write("   POST /v1/messages           (Anthropic API，Claude Code / CC Switch 兼容)\n")
    sys.stderr.write("   POST /upload-auth           上传授权文件\n")
    sys.stderr.write("   GET  /auth-files            授权文件列表\n")
    sys.stderr.write("   POST /auth-files/activate   激活授权文件\n")
    sys.stderr.write("   DEL  /auth-files/delete     删除授权文件\n")
    sys.stderr.write("   GET  /health\n")
    if args.api_key:
        sys.stderr.write("   鉴权已启用（API key 已设置）\n")
    if CONFIG["log_path"]:
        sys.stderr.write(f"   日志      : {CONFIG['log_path']}\n")
    if args.desensitize:
        mode = "零宽脱敏 + 保留全文" if args.no_compact else "零宽脱敏 + 压缩摘要"
        sys.stderr.write(f"   脱敏      : 已启用（{mode}）\n")
    sys.stderr.write(f"   配置目录  : {_config_dir()}\n")
    sys.stderr.write("按 Ctrl+C 退出。\n\n")

    # 启动时写一条标记
    _log(f"==== converter 启动 ====")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
