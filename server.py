"""
RealChat Server v2 - 多会话 + 持久化

新增：
  - JSON 文件持久化聊天历史
  - 会话列表 / 重命名 / 删除 API
  - 会话自动命名
"""
import asyncio
import json
import uuid
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from config import (
    API_BASE_URL, API_KEY, MODEL,
    HOST, PORT, DEFAULT_SYSTEM_PROMPT,
    DEFAULT_THINKING_LEVEL, AUTH_TOKEN, _TOKEN_GENERATED
)
from discussion import (
    create_room, get_room, list_rooms,
    DEFAULT_AGENTS, get_all_agents, load_custom_agents, save_custom_agents,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("realchat")

app = FastAPI(title="RealChat")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ============================================================
# 令牌鉴权中间件
# ============================================================

PUBLIC_PATHS = {"/api/auth", "/api/auth-status", "/api/discussion/agents"}
AUTH_FAIL_MSG = "认证失败"  # 统一错误，不区分无token/错token

# 简易速率限制：内存存储
_auth_rate_limit: dict[str, list[float]] = {}
_AUTH_RATE_WINDOW = 60     # 窗口秒数
_AUTH_RATE_MAX = 10        # 窗口内最大尝试次数
_STREAM_RATE_MAX = 20      # 流式请求每 60 秒上限
_stream_rate_limit: dict[str, list[float]] = {}
MAX_BODY_SIZE = 256 * 1024  # 256KB 请求体上限
MAX_MESSAGE_LENGTH = 10000   # 单条消息最大字符数

@app.middleware("http")
async def body_size_middleware(request: Request, call_next):
    """限制请求体大小，防止大 payload 攻击"""
    if request.method in ("POST", "PUT", "PATCH"):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_BODY_SIZE:
            return JSONResponse(status_code=413, content={"detail": "请求体过大"})
    return await call_next(request)

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """注入安全响应头"""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP：允许本站脚本/样式/图片
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'"
    )
    return response

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """对所有 /api/* 请求校验 Bearer token，公开路径除外。"""
    path = request.url.path
    if not AUTH_TOKEN or not path.startswith("/api/") or path in PUBLIC_PATHS:
        return await call_next(request)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) < 8:
        return JSONResponse(status_code=401, content={"detail": AUTH_FAIL_MSG})
    if not secrets.compare_digest(auth[7:], AUTH_TOKEN):
        return JSONResponse(status_code=401, content={"detail": AUTH_FAIL_MSG})
    return await call_next(request)

# ============================================================
# 会话持久化存储
# ============================================================
DATA_DIR = Path(__file__).parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

class SessionStore:
    """JSON 文件持久化的会话存储"""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._sessions: dict[str, dict] = {}
        self._load()

    def _load(self):
        if SESSIONS_FILE.exists():
            try:
                self._sessions = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
                logger.info(f"Loaded {len(self._sessions)} sessions from disk")
            except Exception:
                self._sessions = {}
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._sessions = {}

    async def _save(self):
        async with self._lock:
            tmp = SESSIONS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._sessions, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(SESSIONS_FILE)

    def create(self, title: str = "新对话") -> str:
        sid = str(uuid.uuid4())[:8]
        now = time.time()
        self._sessions[sid] = {
            "id": sid,
            "title": title,
            "history": [],        # API 上下文（标准 role/content）
            "messages": [],       # 展示用（结构化：每条用户消息独立，AI思考/段落分开）
            "created_at": now,
            "updated_at": now,
        }
        return sid

    def get(self, sid: str) -> Optional[dict]:
        return self._sessions.get(sid)

    def list_all(self) -> list[dict]:
        """返回会话列表（按更新时间倒序，不含完整历史）"""
        result = []
        for s in sorted(self._sessions.values(), key=lambda x: x["updated_at"], reverse=True):
            item = {
                "id": s["id"],
                "title": s["title"],
                "created_at": s["created_at"],
                "updated_at": s["updated_at"],
                "msg_count": len(s.get("history", [])),
                "type": s.get("type", "chat"),
            }
            # 群聊会话附加 discussion 字段
            if s.get("type") == "discussion" and "discussion" in s:
                item["discussion_status"] = s["discussion"].get("status", "completed")
            result.append(item)
        return result

    async def update(self, sid: str, **kwargs):
        if sid not in self._sessions:
            return
        s = self._sessions[sid]
        for k, v in kwargs.items():
            if k in ("title", "history", "messages"):
                s[k] = v
        s["updated_at"] = time.time()
        await self._save()

    async def set_extra(self, sid: str, key: str, value):
        """设置 session 的额外字段（如 type, discussion）并保存"""
        if sid not in self._sessions:
            return
        self._sessions[sid][key] = value
        self._sessions[sid]["updated_at"] = time.time()
        await self._save()

    async def delete(self, sid: str):
        if sid in self._sessions:
            del self._sessions[sid]
            await self._save()

    def get_history_for_api(self, sid: str, system_prompt: str = None) -> list[dict]:
        s = self.get(sid)
        sp = system_prompt or DEFAULT_SYSTEM_PROMPT
        if not s:
            return [{"role": "system", "content": sp}]
        return [{"role": "system", "content": sp}] + s["history"].copy()

    async def add_message(self, sid: str, role: str, content: str):
        s = self.get(sid)
        if not s:
            return
        s["history"].append({"role": role, "content": content})
        # 自动命名
        self._auto_title(s, content, role)
        if len(s["history"]) > 40:
            s["history"] = s["history"][-40:]
        s["updated_at"] = time.time()
        await self._save()

    def _auto_title(self, s: dict, content: str, role: str):
        if s["title"] == "新对话" and role == "user":
            title = content[:30].replace("\n", " ")
            s["title"] = title + ("…" if len(content) > 30 else "")

    async def add_user_segments(self, sid: str, segments: list[str]):
        """存储多条用户消息，每条独立"""
        s = self.get(sid)
        if not s:
            return
        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            s["history"].append({"role": "user", "content": seg})
            s["messages"].append({"role": "user", "content": seg})
            self._auto_title(s, seg, "user")
        if len(s["history"]) > 40:
            s["history"] = s["history"][-40:]
        s["updated_at"] = time.time()
        await self._save()

    async def add_ai_parts(self, sid: str, think_text: str, paras: list[str]):
        """存储 AI 回复：思考 + 段落分开"""
        s = self.get(sid)
        if not s:
            return
        if think_text.strip():
            s["messages"].append({"role": "assistant", "type": "thinking", "content": think_text.strip()})
        for p in paras:
            p = p.strip()
            if p:
                s["messages"].append({"role": "assistant", "type": "para", "content": p})
        s["updated_at"] = time.time()
        await self._save()

store = SessionStore()


# ============================================================
# 全局设置管理
# ============================================================
def load_settings() -> dict:
    """加载设置，默认值兜底"""
    defaults = {
        # 连接
        "api_base_url": API_BASE_URL,
        # 模型
        "model": MODEL,
        # 深度思考
        "thinking_level": DEFAULT_THINKING_LEVEL,  # off / minimal / low / medium / high / extra_high / maximum
        "thinking_enabled": False,  # 开关状态，由控件控制
        # 推理参数
        "temperature": 0.8,
        "top_p": 0.9,
        "max_tokens": 2048,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "seed": None,
        "stop": [],
        # 提示词
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        # 网关令牌（空 = 不启用鉴权）
        "auth_token": "",
    }
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            defaults.update(data)
        except Exception:
            pass
    return defaults

def save_settings(settings: dict):
    """保存设置到文件"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)

def get_current_settings() -> dict:
    s = load_settings()
    return {
        "api_base_url": s["api_base_url"],
        "api_key": s.get("api_key", ""),
        "auth_token": s.get("auth_token", ""),
        "model": s["model"],
        "thinking_level": s.get("thinking_level", DEFAULT_THINKING_LEVEL),
        "thinking_enabled": s.get("thinking_enabled", False),
        "temperature": s["temperature"],
        "top_p": s["top_p"],
        "max_tokens": s["max_tokens"],
        "frequency_penalty": s["frequency_penalty"],
        "presence_penalty": s["presence_penalty"],
        "seed": s["seed"],
        "stop": s["stop"],
        "system_prompt": s["system_prompt"],
    }


# ============================================================
# 解析状态
# ============================================================
class State:
    IDLE       = 0
    READ_THINK = 1
    READ_PARA  = 2


# ============================================================
# 流式 AI 回复 — 核心状态机
# ============================================================
async def stream_and_parse(
    session_id: str,
    user_text: str,
) -> AsyncGenerator[str, None]:
    # Map thinking level to API parameters
    THINKING_MAP = {
        "off":        None,
        "minimal":    {"budget_tokens": 512},
        "low":        {"budget_tokens": 1024},
        "medium":     {"budget_tokens": 2048},
        "high":       {"budget_tokens": 4096},
        "extra_high": {"budget_tokens": 8192},
        "maximum":    {"budget_tokens": 16384},
    }

    # 清洗用户输入：移除可能被误解析的标签，防止提示注入
    user_text = re.sub(r'</?thinking>', '', user_text, flags=re.IGNORECASE)
    user_text = re.sub(r'</?para>', '', user_text, flags=re.IGNORECASE)
    user_text = user_text.strip()

    if not user_text:
        logger.warning("user input empty after sanitization")
        return

    settings = get_current_settings()
    messages = store.get_history_for_api(session_id, settings["system_prompt"])
    messages.append({"role": "user", "content": user_text})

    state       = State.IDLE
    tag_buf     = ""
    para_accum   = ""
    think_total  = 0
    think_max    = 3000
    ai_full_text = ""
    para_emitted  = False
    think_collected = ""   # 收集的完整思考文本
    paras_collected = []   # 收集的段落列表

    TAG_THINK     = "<thinking>"
    TAG_THINK_END = "</thinking>"
    TAG_PARA      = "<para>"
    TAG_PARA_END  = "</para>"
    MAX_TAG_LEN   = max(len(TAG_THINK), len(TAG_THINK_END), len(TAG_PARA), len(TAG_PARA_END))

    def sse(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def emit_para(text: str):
        nonlocal para_emitted
        text = text.strip()
        if text:
            para_emitted = True
            paras_collected.append(text)
            return sse("para", {"text": text})
        return ""

    api_key = settings.get("api_key") or API_KEY
    if not api_key:
        yield sse("error", {"message": "未配置 API Key，请在设置中填写或设置环境变量 REALCHAT_API_KEY"})
        return

    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": settings["model"],
            "messages": messages,
            "stream": True,
            "max_tokens": settings["max_tokens"],
            "temperature": settings["temperature"],
            "top_p": settings["top_p"],
            "frequency_penalty": settings["frequency_penalty"],
            "presence_penalty": settings["presence_penalty"],
        }
        if settings["seed"] is not None:
            payload["seed"] = settings["seed"]
        if settings["stop"]:
            payload["stop"] = settings["stop"]

        # 深度思考参数
        thinking_enabled = settings.get("thinking_enabled", False)
        thinking_level = settings.get("thinking_level", "off")
        if thinking_enabled and thinking_level != "off" and thinking_level in THINKING_MAP:
            thinking_cfg = THINKING_MAP[thinking_level]
            api_base = settings["api_base_url"]
            # DeepSeek 风格: thinking: { type: "enabled", budget_tokens: N }
            if "deepseek" in api_base.lower():
                payload["thinking"] = {
                    "type": "enabled",
                    **thinking_cfg,
                }
            else:
                # OpenAI 风格: reasoning_effort
                level_names = {
                    "minimal": "minimal", "low": "low", "medium": "medium",
                    "high": "high", "extra_high": "high", "maximum": "high"
                }
                payload["reasoning_effort"] = level_names.get(thinking_level, "medium")

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", f"{settings['api_base_url']}/chat/completions",
                                     headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    logger.error(f"API error {resp.status_code}", exc_info=False)
                    yield sse("error", {"message": f"AI 服务返回错误 ({resp.status_code})，请检查 API Key 和 Base URL"})
                    return

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if not content:
                        continue

                    ai_full_text += content
                    tag_buf += content

                    # 状态机
                    while True:
                        if state == State.IDLE:
                            th_idx = tag_buf.find(TAG_THINK)
                            pa_idx = tag_buf.find(TAG_PARA)
                            if th_idx != -1 and (pa_idx == -1 or th_idx < pa_idx):
                                tag_buf = tag_buf[th_idx + len(TAG_THINK):]
                                state = State.READ_THINK
                                continue
                            elif pa_idx != -1:
                                tag_buf = tag_buf[pa_idx + len(TAG_PARA):]
                                para_accum = ""
                                state = State.READ_PARA
                                continue
                            else:
                                tag_buf = tag_buf[-MAX_TAG_LEN:] if len(tag_buf) > MAX_TAG_LEN else tag_buf
                                break

                        elif state == State.READ_THINK:
                            close_idx = tag_buf.find(TAG_THINK_END)
                            if close_idx != -1:
                                text = tag_buf[:close_idx]
                                think_collected += text
                                for ch in text:
                                    yield sse("thinking_char", {"char": ch})
                                think_total += len(text)
                                tag_buf = tag_buf[close_idx + len(TAG_THINK_END):]
                                yield sse("thinking_done", {})
                                state = State.IDLE
                                continue
                            else:
                                safe_len = max(0, len(tag_buf) - len(TAG_THINK_END))
                                text = tag_buf[:safe_len]
                                think_collected += text
                                for ch in text:
                                    yield sse("thinking_char", {"char": ch})
                                think_total += len(text)
                                tag_buf = tag_buf[safe_len:]
                                if think_total > think_max:
                                    yield sse("thinking_done", {})
                                    state = State.IDLE
                                    tag_buf = ""
                                break

                        elif state == State.READ_PARA:
                            close_idx = tag_buf.find(TAG_PARA_END)
                            next_open = tag_buf.find(TAG_PARA)
                            if close_idx != -1:
                                para_accum += tag_buf[:close_idx]
                                tag_buf = tag_buf[close_idx + len(TAG_PARA_END):]
                                ev = emit_para(para_accum)
                                if ev: yield ev
                                para_accum = ""
                                state = State.IDLE
                                continue
                            elif next_open != -1:
                                para_accum += tag_buf[:next_open]
                                tag_buf = tag_buf[next_open + len(TAG_PARA):]
                                ev = emit_para(para_accum)
                                if ev: yield ev
                                para_accum = ""
                                continue
                            else:
                                safe_len = max(0, len(tag_buf) - MAX_TAG_LEN)
                                para_accum += tag_buf[:safe_len]
                                tag_buf = tag_buf[safe_len:]
                                break

                # 流结束容错
                if state == State.READ_PARA:
                    para_accum += tag_buf
                    ev = emit_para(para_accum)
                    if ev: yield ev

                if not para_emitted and ai_full_text.strip():
                    clean = re.sub(r'</?thinking>', '', ai_full_text)
                    clean = re.sub(r'</?para>', '', clean).strip()
                    if clean:
                        yield emit_para(clean)

                yield sse("done", {"ai_full_text": ai_full_text, "session_id": session_id, "think_text": think_collected, "paras": paras_collected})

    except httpx.ConnectError:
        yield sse("error", {"message": "无法连接到 API 服务器"})
    except httpx.TimeoutException:
        yield sse("error", {"message": "请求超时"})
    except Exception:
        # 不记录完整 traceback，避免 API Key 泄露到日志
        logger.error("stream error", exc_info=False)
        yield sse("error", {"message": "服务内部错误，请稍后重试"})


# ============================================================
# API 路由
# ============================================================

# ---- 鉴权 ----

@app.get("/api/auth-status")
async def api_auth_status():
    """检查是否需要 token 鉴权"""
    return {"auth_required": bool(AUTH_TOKEN)}

@app.post("/api/auth")
async def api_auth_login(req: Request):
    """验证令牌（带速率限制，防爆破）"""
    client_ip = req.client.host if req.client else "unknown"
    now = time.time()

    # 速率限制检查
    if client_ip not in _auth_rate_limit:
        _auth_rate_limit[client_ip] = []
    timestamps = _auth_rate_limit[client_ip]
    timestamps[:] = [t for t in timestamps if now - t < _AUTH_RATE_WINDOW]
    if len(timestamps) >= _AUTH_RATE_MAX:
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    timestamps.append(now)
    # 清理过期条目
    if len(_auth_rate_limit) > 1000:
        _auth_rate_limit.clear()

    body = await req.json()
    token = body.get("token", "").strip()
    if not AUTH_TOKEN:
        return {"status": "ok", "message": "无需认证"}
    if token and secrets.compare_digest(token, AUTH_TOKEN):
        return {"status": "ok"}
    raise HTTPException(401, AUTH_FAIL_MSG)

# ---- 核心聊天 ----

@app.post("/api/stream")
async def api_stream(req: Request):
    """核心接口：发送消息并返回 SSE 流"""
    # 流式端点速率限制
    client_ip = req.client.host if req.client else "unknown"
    now = time.time()
    if client_ip not in _stream_rate_limit:
        _stream_rate_limit[client_ip] = []
    stamps = _stream_rate_limit[client_ip]
    stamps[:] = [t for t in stamps if now - t < _AUTH_RATE_WINDOW]
    if len(stamps) >= _STREAM_RATE_MAX:
        raise HTTPException(429, "请求过于频繁，请稍后再试")
    stamps.append(now)

    body = await req.json()
    user_input = body.get("user_input", "").strip()
    user_segments = body.get("user_segments", [])
    session_id = body.get("session_id", "").strip()

    # 消息长度限制
    if len(user_input) > MAX_MESSAGE_LENGTH:
        raise HTTPException(400, f"消息过长，限制 {MAX_MESSAGE_LENGTH} 字符")

    # session_id 格式校验
    if session_id and not re.match(r'^[a-f0-9]{8}$', session_id):
        raise HTTPException(400, "会话 ID 格式无效")

    if not user_input:
        raise HTTPException(400, "用户输入不能为空")

    # 确保会话存在
    if not session_id or not store.get(session_id):
        session_id = store.create()

    async def stream_with_history():
        async for sse_msg in stream_and_parse(session_id, user_input):
            if sse_msg.startswith("event: done"):
                # 存储用户消息（逐条）
                segments = user_segments if user_segments else [user_input]
                await store.add_user_segments(session_id, segments)
                # 存储 AI 回复（结构化：思考 + 段落分开）
                try:
                    line = sse_msg.split("\n")[1]
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        ai_text = data.get("ai_full_text", "")
                        think = data.get("think_text", "")
                        paras = data.get("paras", [])
                        # history 中保存完整原始文本（供后续 API 上下文用）
                        if ai_text:
                            await store.add_message(session_id, "assistant", ai_text)
                        # messages 中保存结构化展示数据
                        await store.add_ai_parts(session_id, think, paras)
                except Exception:
                    pass
            yield sse_msg

    return StreamingResponse(
        stream_with_history(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- 会话管理 API ----

@app.get("/api/sessions")
async def api_list_sessions():
    """列出所有会话"""
    return {"sessions": store.list_all()}


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str):
    """获取单个会话详情（含历史）"""
    s = store.get(session_id)
    if not s:
        raise HTTPException(404, "会话不存在")
    is_disc = s.get("type") == "discussion"
    disc = s.get("discussion", {})
    result = {
        "id": s["id"],
        "title": s["title"],
        "history": s["history"],
        "messages": s.get("messages", []),
        "created_at": s["created_at"],
        "updated_at": s["updated_at"],
        "type": s.get("type", "chat"),
        "is_discussion": is_disc,
        "discussion_status": disc.get("status") if is_disc else None,
        "discussion": disc if is_disc else None,
    }
    # 转换 agent 消息为前端期望的嵌套格式（深拷贝避免原地修改存储数据）
    msgs = []
    for m in s.get("messages", []):
        m2 = dict(m)
        if m2.get("role") == "agent":
            m2["agent"] = {
                "name": m2.pop("agent_name", ""),
                "emoji": m2.pop("agent_emoji", ""),
                "color": m2.pop("agent_color", "#888"),
            }
        msgs.append(m2)
    result["messages"] = msgs
    return result


@app.patch("/api/sessions/{session_id}")
async def api_rename_session(session_id: str, req: Request):
    """重命名会话"""
    body = await req.json()
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, "标题不能为空")
    if not store.get(session_id):
        raise HTTPException(404, "会话不存在")
    await store.update(session_id, title=title)
    return {"status": "ok"}


@app.delete("/api/sessions/{session_id}")
async def api_delete_session(session_id: str):
    """删除会话"""
    if not store.get(session_id):
        raise HTTPException(404, "会话不存在")
    await store.delete(session_id)
    return {"status": "ok"}


@app.post("/api/sessions")
async def api_create_session(req: Request):
    """创建新会话"""
    body = await req.json()
    title = body.get("title", "新对话").strip() or "新对话"
    sid = store.create(title=title)
    return {"session_id": sid, "title": title}


@app.get("/api/status")
async def api_status():
    return {"status": "ok", "sessions": len(store._sessions)}


# ---- 设置 API ----

@app.get("/api/settings")
async def api_get_settings():
    """获取当前设置"""
    return get_current_settings()


@app.put("/api/settings")
async def api_save_settings(req: Request):
    """保存设置"""
    body = await req.json()
    current = load_settings()
    str_fields = ["model", "system_prompt", "api_base_url", "api_key", "auth_token", "thinking_level"]
    bool_fields = ["thinking_enabled"]
    float_fields = ["temperature", "top_p", "frequency_penalty", "presence_penalty"]
    int_fields = ["max_tokens"]
    list_fields = ["stop"]
    special_fields = ["seed"]  # None or int

    for k in str_fields:
        if k in body:
            current[k] = body[k]
    for k in float_fields:
        if k in body and body[k] is not None:
            current[k] = float(body[k])
    for k in int_fields:
        if k in body and body[k] is not None:
            current[k] = int(body[k])
    for k in list_fields:
        if k in body:
            current[k] = body[k] if isinstance(body[k], list) else []
    for k in bool_fields:
        if k in body:
            current[k] = bool(body[k])
    for k in special_fields:
        if k in body:
            current[k] = int(body[k]) if body[k] is not None and body[k] != "" else None

    save_settings(current)
    return {"status": "ok", "model": current["model"]}


# ============================================================
# 多智能体群聊 API
# ============================================================

@app.get("/api/discussion/agents")
async def api_list_agents():
    """获取所有角色列表（预设 + 自定义）"""
    return {"agents": get_all_agents(), "preset": DEFAULT_AGENTS, "custom": load_custom_agents()}


@app.post("/api/discussion/start")
async def api_start_discussion(req: Request):
    """启动一场多 AI 讨论"""
    body = await req.json()
    topic = body.get("topic", "").strip()
    agent_names = body.get("agents", [])  # 选中的 agent 名字列表
    max_rounds = min(body.get("max_rounds", 6), 32)
    token_limit = min(body.get("token_limit", 8000), 32000)

    if not topic:
        raise HTTPException(400, "讨论主题不能为空")

    settings = get_current_settings()
    api_key = settings.get("api_key") or API_KEY
    if not api_key:
        raise HTTPException(400, "请先配置 API Key")

    # 筛选选中的 agent（支持预设和自定义角色）
    all_agents = get_all_agents()
    if agent_names:
        agents = [a for a in all_agents if a["name"] in agent_names]
    else:
        agents = all_agents[:3]  # 默认 3 个

    if len(agents) < 2:
        raise HTTPException(400, "至少需要 2 个角色参与讨论")

    room = create_room(
        agents=agents,
        topic=topic,
        api_base=settings["api_base_url"],
        api_key=api_key,
        model=settings["model"],
        max_rounds=max_rounds,
        token_limit=token_limit,
        temperature=body.get("temperature", 1.1),
        top_p=body.get("top_p", 0.9),
    )

    # 创建对应的聊天会话（群聊作为特殊会话类型，空对话）
    title = topic if topic else f"{'、'.join(a['name'] for a in agents[:3])} 的群聊"
    sid = store.create(title=title)
    await store.set_extra(sid, "type", "discussion")
    await store.set_extra(sid, "discussion", {
        "room_id": room.room_id,
        "topic": topic or "",
        "agents": [{"name": a["name"], "emoji": a["emoji"], "color": a["color"], "system_prompt": a.get("system_prompt","")} for a in agents],
        "status": "active",
        "rounds": 0,
        "max_rounds": max_rounds,
    })
    await store.update(sid, messages=[{
        "role": "system", "type": "agents",
        "content": f"参与角色：{'、'.join(a['emoji']+a['name'] for a in agents)} | 每轮 {max_rounds} 次发言",
    }])

    return {
        "room_id": room.room_id,
        "session_id": sid,
        "agents": [{"name": a["name"], "emoji": a["emoji"], "color": a["color"]} for a in agents],
        "topic": topic,
    }


@app.post("/api/discussion/interrupt")
async def api_discussion_interrupt(req: Request):
    """用户在讨论中发消息（插嘴）"""
    body = await req.json()
    room_id = body.get("room_id", "").strip()
    message = body.get("message", "").strip()

    if not message:
        raise HTTPException(400, "消息不能为空")
    if len(message) > 2000:
        raise HTTPException(400, "消息过长")

    room = get_room(room_id)
    if not room:
        raise HTTPException(404, "讨论房间不存在")

    room.user_interrupt(message)
    return {"status": "ok", "queued": len(room.pending_user_msgs)}


@app.post("/api/discussion/abort")
async def api_discussion_abort(req: Request):
    """强制中断讨论"""
    body = await req.json()
    room_id = body.get("room_id", "").strip()

    room = get_room(room_id)
    if not room:
        raise HTTPException(404, "讨论房间不存在")

    room.abort()
    return {"status": "ok"}


@app.get("/api/discussion/rooms")
async def api_list_discussion_rooms():
    """列出活跃讨论房间"""
    return {"rooms": list_rooms()}


# ---- 自定义角色 CRUD ----

@app.get("/api/discussion/agents/custom")
async def api_get_custom_agents():
    """获取自定义角色列表"""
    return {"agents": load_custom_agents()}


@app.post("/api/discussion/agents/custom")
async def api_create_custom_agent(req: Request):
    """创建或更新自定义角色"""
    body = await req.json()
    name = body.get("name", "").strip()
    emoji = body.get("emoji", "🤖").strip()
    color = body.get("color", "#6c63ff").strip()
    system_prompt = body.get("system_prompt", "").strip()

    if not name:
        raise HTTPException(400, "角色名不能为空")
    if len(name) > 20:
        raise HTTPException(400, "角色名最长20字")
    if not system_prompt:
        raise HTTPException(400, "角色 Prompt 不能为空")

    agents = load_custom_agents()
    # 更新或新增
    existing = next((a for a in agents if a["name"] == name), None)
    if existing:
        existing["emoji"] = emoji
        existing["color"] = color
        existing["system_prompt"] = system_prompt
    else:
        agents.append({
            "name": name,
            "emoji": emoji,
            "color": color,
            "system_prompt": system_prompt,
        })
    save_custom_agents(agents)
    return {"status": "ok", "agent": {"name": name, "emoji": emoji, "color": color}}


@app.delete("/api/discussion/agents/custom")
async def api_delete_custom_agent(req: Request):
    """删除自定义角色"""
    body = await req.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "角色名不能为空")

    agents = load_custom_agents()
    agents = [a for a in agents if a["name"] != name]
    save_custom_agents(agents)
    return {"status": "ok"}


# ---- 群聊会话联动 ----

@app.post("/api/discussion/session-stream")
async def api_discussion_session_stream(req: Request):
    """群聊 SSE 流 — 每次用户发言触发一轮讨论，可重复调用"""
    body = await req.json()
    room_id = body.get("room_id", "").strip()
    session_id = body.get("session_id", "").strip()
    user_message = body.get("user_message", "").strip()

    room = get_room(room_id)
    if not room:
        raise HTTPException(404, "讨论房间不存在")

    # 存储用户消息到 session
    if user_message:
        s = store.get(session_id)
        if s:
            msgs = list(s.get("messages", []))
            msgs.append({"role": "user", "content": user_message})
            s["messages"] = msgs
            s["updated_at"] = time.time()

    async def event_stream():
        async for evt in room.run_as_chat(user_message=user_message):
            # 提取事件类型作为 SSE event: 行
            try:
                data = json.loads(evt)
                etype = data.get("type", "message")
            except Exception:
                etype = "message"
            yield f"event: {etype}\ndata: {evt}\n\n"
            # 同步写入 session 存储
            try:
                data = json.loads(evt)
                s = store.get(session_id)
                if not s:
                    continue
                if data.get("type") == "done":
                    disc = data.get("discussion", {})
                    agent = data.get("agent")
                    if agent:
                        msgs = list(s.get("messages", []))
                        if data.get("think_text"):
                            msgs.append({"role": "agent", "agent_name": agent["name"],
                                "agent_emoji": agent["emoji"], "agent_color": agent["color"],
                                "type": "thinking", "content": data["think_text"]})
                        for p in data.get("paras", []):
                            if p.strip():
                                msgs.append({"role": "agent", "agent_name": agent["name"],
                                    "agent_emoji": agent["emoji"], "agent_color": agent["color"],
                                    "type": "speech", "content": p.strip()})
                        s["messages"] = msgs
                    if disc:
                        s["discussion"]["status"] = disc.get("status", "active")
                        s["discussion"]["rounds"] = disc.get("rounds", 0)
                        if disc.get("summary"):
                            msgs = list(s.get("messages", []))
                            msgs.append({"role": "system", "type": "summary", "content": disc["summary"]})
                            s["messages"] = msgs
                    s["updated_at"] = time.time()
                elif data.get("type") == "user_interject" and data.get("text"):
                    msgs = list(s.get("messages", []))
                    msgs.append({"role": "user", "content": data["text"]})
                    s["messages"] = msgs
                    s["updated_at"] = time.time()
            except Exception:
                pass
        # 完成后保存（保持 active 状态，允许下一轮）
        try:
            s = store.get(session_id)
            if s:
                s["updated_at"] = time.time()
            await store._save()
        except Exception:
            pass
        yield "event: stream_end\ndata: {\"type\": \"stream_end\"}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 数据导入导出
# ============================================================

@app.get("/api/export")
async def api_export(ids: str = ""):
    """导出数据为 JSON 文件。ids: 逗号分隔的会话 ID，为空则导出全部"""
    # 筛选会话
    if ids:
        id_list = [i.strip() for i in ids.split(",") if i.strip()]
        sessions = {sid: store._sessions[sid] for sid in id_list if sid in store._sessions}
    else:
        sessions = store._sessions

    export_data = {
        "version": "realchat-v2",
        "exported_at": time.time(),
        "exported_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "sessions": sessions,
        "settings": load_settings(),
        "custom_agents": load_custom_agents(),
    }
    # 隐藏 API Key
    if "api_key" in export_data["settings"]:
        key = export_data["settings"]["api_key"]
        if key and len(key) > 8:
            export_data["settings"]["api_key"] = key[:4] + "****" + key[-4:]

    return JSONResponse(
        export_data,
        headers={"Content-Disposition": "attachment; filename=realchat-backup-{}.json".format(
            time.strftime("%Y%m%d-%H%M%S", time.localtime()))}
    )


@app.post("/api/import")
async def api_import(req: Request):
    """导入数据，合并到现有数据中"""
    body = await req.json()

    if body.get("version", "") != "realchat-v2":
        raise HTTPException(400, "不支持的备份文件格式")

    imported = 0
    skipped = 0

    # 导入会话
    sessions = body.get("sessions", {})
    if sessions:
        for sid, sdata in sessions.items():
            if sid not in store._sessions:
                sdata["imported_at"] = time.time()
                store._sessions[sid] = sdata
                imported += 1
            else:
                skipped += 1
        await store._save()

    # 导入自定义角色
    custom_agents = body.get("custom_agents", [])
    if custom_agents:
        existing = load_custom_agents()
        existing_names = {a["name"] for a in existing}
        for agent in custom_agents:
            if agent["name"] not in existing_names:
                existing.append(agent)
                imported += 1
        save_custom_agents(existing)

    # 导入设置（仅模型/提示词，不覆盖 Key/Token）
    settings = body.get("settings", {})
    if settings:
        current = load_settings()
        safe_fields = ["model", "system_prompt", "temperature", "top_p", "max_tokens",
                       "frequency_penalty", "presence_penalty", "thinking_level", "thinking_enabled"]
        changed = False
        for k in safe_fields:
            if k in settings:
                current[k] = settings[k]
                changed = True
        if changed:
            save_settings(current)

    return {"status": "ok", "imported": imported, "skipped": skipped}


# 静态文件
app.mount("/", StaticFiles(directory="static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    import sys
    print(f"\nRealChat v2 启动", file=sys.stderr)
    print(f"   地址: http://{HOST}:{PORT}", file=sys.stderr)
    print(f"   API : {API_BASE_URL}", file=sys.stderr)
    print(f"   模型: {MODEL}", file=sys.stderr)
    print(f"   存储: {SESSIONS_FILE}", file=sys.stderr)
    if _TOKEN_GENERATED:
        print(f"\n   🔐 首次部署，已自动生成网关令牌：", file=sys.stderr)
        print(f"      {AUTH_TOKEN}", file=sys.stderr)
        print(f"   ⚠️  请妥善保存！仅在首次部署时显示。", file=sys.stderr)
    uvicorn.run(app, host=HOST, port=PORT)
