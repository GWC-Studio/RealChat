"""
RealChat - Configuration
"""
import os

# API 配置
API_BASE_URL = os.getenv("REALCHAT_API_BASE", "https://api.deepseek.com/v1")
API_KEY = os.getenv("REALCHAT_API_KEY", "")
MODEL = os.getenv("REALCHAT_MODEL", "deepseek-chat")

# 用户消息缓冲等待时间（秒）
BUFFER_TIMEOUT = float(os.getenv("REALCHAT_BUFFER_TIMEOUT", "2.0"))

# 深度思考等级: off / minimal / low / medium / high / extra_high / maximum
DEFAULT_THINKING_LEVEL = os.getenv("REALCHAT_THINKING_LEVEL", "off")

# 网关令牌鉴权
# 优先级：环境变量 > settings.json > 首次部署自动生成
import secrets as _secrets
_AUTH_ENV = os.getenv("REALCHAT_AUTH_TOKEN", "")
_SETTINGS_DIR = os.path.join(os.path.dirname(__file__), "data")
_SETTINGS_PATH = os.path.join(_SETTINGS_DIR, "settings.json")

AUTH_TOKEN = ""
_TOKEN_GENERATED = False

if _AUTH_ENV:
    AUTH_TOKEN = _AUTH_ENV
else:
    try:
        import json as _json
        _settings = _json.load(open(_SETTINGS_PATH))
        AUTH_TOKEN = _settings.get("auth_token", "")
    except Exception:
        AUTH_TOKEN = ""

    # 首次部署：/data 目录不存在 → 自动生成令牌
    if not os.path.isdir(_SETTINGS_DIR):
        AUTH_TOKEN = _secrets.token_hex(16)
        _TOKEN_GENERATED = True
        try:
            os.makedirs(_SETTINGS_DIR, exist_ok=True)
            _existing = {}
            if os.path.exists(_SETTINGS_PATH):
                _existing = _json.load(open(_SETTINGS_PATH))
            _existing["auth_token"] = AUTH_TOKEN
            _tmp = _SETTINGS_PATH + ".tmp"
            with open(_tmp, "w", encoding="utf-8") as f:
                _json.dump(_existing, f, ensure_ascii=False, indent=2)
            os.replace(_tmp, _SETTINGS_PATH)
        except Exception:
            pass

# 服务器配置
HOST = os.getenv("REALCHAT_HOST", "0.0.0.0")
PORT = int(os.getenv("REALCHAT_PORT", "5000"))

# 默认 System Prompt
DEFAULT_SYSTEM_PROMPT = """你是一个模仿真人聊天的助手。你的回复必须严格遵守以下格式规则：

1. **思考过程**：所有推理、分析必须包裹在 <thinking>...</thinking> 标签内，且这部分必须作为整个回复的第一部分。
2. **分段消息**：正式回复内容，每个独立的、需要作为一条独立消息发送的句子或段落，都必须包裹在 <para>...</para> 标签中。
3. **禁止使用序号或列表**：用自然的段落表达，不要用"第一、第二"之类的序号。
4. **语气自然**：像朋友聊天一样自然，不要像客服机器人。

示例格式：
<thinking>用户的问题是关于XX，我需要先了解几个关键信息才能给出准确建议...</thinking>
<para>好的，那我来问几个问题帮你把情况搞清楚。</para>
<para>你用的是哪种材料？</para>
<para>平时大概多久会出现你说的现象？</para>

注意：<thinking> 标签必须作为回复的开头，且在整个回复中只出现一次。每个 <para> 标签包含一条独立的回复消息，按逻辑顺序排列。"""
