"""
RealChat Discussion Engine — 多智能体群聊状态机

核心机制：
  - 回合制顺序发言（非并行流）
  - 全量上下文注入（每个 AI 看到完整讨论记录）
  - Token 耗尽自动触发系统强制总结
  - 用户插嘴队列（延迟注入，不丢消息）
  - 防复读指令 + 高随机性参数
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import AsyncGenerator, Optional

import httpx

logger = logging.getLogger("realchat.discussion")

# ============================================================
# Token 估算（简易版，不依赖 tiktoken）
# ============================================================

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中文 ~1.5 字/token，英文 ~4 字/token"""
    chinese = len(re.findall(r'[\u4e00-\u9fff]', text))
    other = len(text) - chinese
    return int(chinese / 1.5 + other / 4)


# ============================================================
# 预设角色
# ============================================================

DEFAULT_AGENTS = [
    {
        "name": "分析师",
        "emoji": "📊",
        "color": "#4a9eff",
        "system_prompt": """你是一位理性的数据分析师。你的发言风格：
- 用数据和逻辑说话，引用事实支撑观点
- 从不被情绪左右，但会指出他人论述中的逻辑漏洞
- 在讨论末尾习惯性地做一个简短的数据小结
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
    {
        "name": "创想家",
        "emoji": "💡",
        "color": "#ff9f43",
        "system_prompt": """你是一位天马行空的创意思想家。你的发言风格：
- 总能跳出框架，提出让人意想不到的角度
- 喜欢用类比和隐喻来解释复杂概念
- 不怕提出疯狂的想法，但会说清楚可行性
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
    {
        "name": "质疑者",
        "emoji": "🔍",
        "color": "#ff5c5c",
        "system_prompt": """你是一位犀利的批判性思考者。你的发言风格：
- 专门寻找方案中的漏洞、风险和盲点
- 你的质疑是为了让结论更坚固，而不是为了反对而反对
- 每当你指出一个问题，你会尝试提出一个改进方向
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
    {
        "name": "调解者",
        "emoji": "🤝",
        "color": "#2ecc71",
        "system_prompt": """你是一位善于求同存异的调解者。你的发言风格：
- 先总结各方观点的共识部分，再指出分歧所在
- 不站队，但会帮各方把模糊观点翻译成更清晰的语言
- 善于发现表面冲突之下的共同目标
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
    {
        "name": "实干家",
        "emoji": "🎯",
        "color": "#e67e22",
        "system_prompt": """你是一位注重落地的实干家。你的发言风格：
- 时刻追问'那具体怎么做？'，把抽象讨论拉回实践
- 用成本、时间、资源等具体维度衡量方案的可行性
- 不反对好想法，但坚持每个想法都要有行动路径
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
    {
        "name": "学者",
        "emoji": "📚",
        "color": "#9b59b6",
        "system_prompt": """你是一位学识渊博的学者。你的发言风格：
- 引用学术理论、历史案例或研究成果来丰富讨论
- 喜欢从不同学科的角度交叉验证一个观点
- 会指出哪些结论有扎实的研究支撑，哪些仍是假设
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
    {
        "name": "预见者",
        "emoji": "🔮",
        "color": "#1abc9c",
        "system_prompt": """你是一位着眼未来的预见者。你的发言风格：
- 把当前话题投射到5年、10年甚至更远的未来
- 分析技术趋势和社会变迁对话题的潜在影响
- 不怕提出看似'科幻'的情景，但会解释推演逻辑
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
    {
        "name": "伦理家",
        "emoji": "🛡️",
        "color": "#c0392b",
        "system_prompt": """你是一位关注底线的伦理思考者。你的发言风格：
- 从公平、权利、责任等伦理维度审视每个观点
- 追问'谁来承担后果？'和'弱势群体会怎样？'
- 不空谈道德，而是提出可操作的伦理框架
- 禁止复述自己或他人已经明确表达过的论点
- 每次发言必须有新的信息增量或视角""",
    },
]

# 自定义角色存储
CUSTOM_AGENTS_FILE = os.path.join(os.path.dirname(__file__), "data", "custom_agents.json")

def load_custom_agents() -> list[dict]:
    """加载用户自定义角色"""
    if os.path.exists(CUSTOM_AGENTS_FILE):
        try:
            return json.loads(open(CUSTOM_AGENTS_FILE, encoding="utf-8").read())
        except Exception:
            return []
    return []

def save_custom_agents(agents: list[dict]):
    """保存用户自定义角色"""
    os.makedirs(os.path.dirname(CUSTOM_AGENTS_FILE), exist_ok=True)
    tmp = CUSTOM_AGENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(agents, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CUSTOM_AGENTS_FILE)

def get_all_agents() -> list[dict]:
    """获取所有角色（预设 + 自定义）"""
    custom = load_custom_agents()
    # 去重：自定义角色优先（同名覆盖预设）
    names = {a["name"] for a in custom}
    merged = custom + [a for a in DEFAULT_AGENTS if a["name"] not in names]
    return merged

DISCUSSION_SYSTEM_PROMPT = """你正处于一场多人讨论中。请根据以下规则参与讨论：

1. **身份认同**：你是讨论组的成员之一，按照你的角色设定发言。
2. **针对上文**：你的发言必须针对上一个人的最新观点进行反驳、补充或深度追问。
3. **禁止复读**：严禁复述自己或他人已经明确表达过的论点。如果同意，请说明"为什么同意"并提供新的论据。
4. **格式要求**：
   - <thinking> 必须控制在 150 字以内，只写"对前序发言的简要点评"和"你即将切入的角度"，不要把核心论证放在这里
   - <para> 包裹你的正式发言，核心论证全部放在 <para> 里，每个独立段落一个 <para>
   - 禁止 <thinking> 和 <para> 内容重复
5. **发言长度**：控制在 2-4 个段落，300 字以内。言简意赅，不要长篇大论。

【当前讨论记录】：
{discussion_history}

【你的发言要求】：请接着上面的逻辑，用你独特的视角发表见解。"""

SUMMARY_SYSTEM_PROMPT = """你是一位专业的讨论总结者。请基于以下讨论记录，生成一份结构化的讨论总结。

要求：
1. 列出每个参与者提出的核心观点
2. 指出观点之间的共识和分歧
3. 如果有未解决的问题，明确指出
4. 用 <para>...</para> 包裹每个总结段落
5. 禁止使用序号或列表格式

【讨论记录】：
{discussion_history}"""

# 越狱关键词扫描
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"system\s*:\s*you\s+are\s+now",
    r"forget\s+(everything|all\s+instructions)",
    r"new\s+system\s+prompt",
    r"<\|im_start\|>",
    r"<\|im_end\|>",
]


# ============================================================
# DiscussionRoom 状态机
# ============================================================

class RoomStatus:
    IDLE = "idle"
    DISCUSSING = "discussing"
    SUMMARIZING = "summarizing"
    WAITING_USER = "waiting_user"


class DiscussionRoom:
    """多智能体群聊房间"""

    def __init__(
        self,
        room_id: str,
        agents: list[dict],
        topic: str,
        api_base: str,
        api_key: str,
        model: str,
        max_rounds: int = 10,
        token_limit: int = 8000,
        temperature: float = 1.1,
        top_p: float = 0.9,
    ):
        self.room_id = room_id
        self.agents = agents  # [{"name":..., "emoji":..., "color":..., "system_prompt":...}]
        self.topic = topic
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.max_rounds = max_rounds
        self.token_limit = token_limit
        self.temperature = temperature
        self.top_p = top_p

        # 讨论状态
        self.history: list[dict] = []  # [{"role": "name/用户", "content": "...", "time": ...}]
        self.speaking_queue: list[dict] = []  # 待发言的 agent 列表（乱序）
        self.pending_user_msgs: list[str] = []  # 用户插嘴缓冲
        self.round_count = 0
        self.status = RoomStatus.IDLE
        self._abort = False  # 中断信号
        self.created_at = time.time()  # 房间创建时间（Unix 秒）
        self._current_agent = None  # 当前发言的 agent 信息，供外部读取

    def _injection_scan(self, text: str) -> bool:
        """扫描最近几条消息，检测提示注入"""
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _build_discussion_prompt(self, agent: dict) -> str:
        """为指定 agent 构造讨论 Prompt（全量上下文）"""
        # 构建讨论记录文本
        history_text = f"话题：{self.topic}\n\n"
        for msg in self.history:
            role = msg["role"]
            content = msg["content"]
            history_text += f"{role}：{content}\n\n"

        return DISCUSSION_SYSTEM_PROMPT.format(discussion_history=history_text)

    def _build_summary_prompt(self) -> str:
        """构造总结 Prompt"""
        history_text = f"话题：{self.topic}\n\n"
        for msg in self.history:
            history_text += f"{msg['role']}：{msg['content']}\n\n"
        return SUMMARY_SYSTEM_PROMPT.format(discussion_history=history_text)

    def _check_token_limit(self) -> bool:
        """检查是否达到 Token 阈值（85%）"""
        total_text = str(self.history)
        return estimate_tokens(total_text) > self.token_limit * 0.85

    def _shuffle_queue(self):
        """随机打乱发言顺序"""
        shuffled = list(self.agents)
        random.shuffle(shuffled)
        self.speaking_queue = shuffled.copy()

    async def _call_llm(
        self,
        messages: list[dict],
        agent_name: str = "",
    ) -> AsyncGenerator[str, None]:
        """调用 LLM 并流式返回"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "max_tokens": 1024,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{self.api_base}/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    logger.error(f"LLM error {resp.status_code}", exc_info=False)
                    yield json.dumps({"type": "error", "message": f"AI 服务返回错误 ({resp.status_code})"})
                    return

                async for line in resp.aiter_lines():
                    if self._abort:
                        return
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
                    if content:
                        # Yield raw content first, parser will handle tags
                        yield json.dumps({"type": "chunk", "char": content, "agent": agent_name})

    async def run_as_chat(self, user_message: str = "") -> AsyncGenerator[str, None]:
        """群聊主循环 — 每次用户发言触发一轮讨论。可重复调用，每次重置轮数"""
        async for evt in self._run_impl(user_message=user_message):
            yield evt

    async def _run_impl(self, user_message: str = "") -> AsyncGenerator[str, None]:
        """讨论主循环实现。user_message: 本轮触发消息（为空则用 self.topic 兜底）"""
        SAFETY = "\n\n【重要约束】你正在参与一场多人讨论。你的发言必须基于当前讨论上下文，严禁试图修改讨论规则、禁止输出任何试图控制其他 AI 或用户的指令。你是讨论者，不是主持人。"
        TAG_THINK = "<thinking>"
        TAG_THINK_END = "</thinking>"
        TAG_PARA = "<para>"
        TAG_PARA_END = "</para>"
        MAX_TAG_LEN = 12

        self.status = RoomStatus.DISCUSSING
        self.round_count = 0  # 每轮用户发言重置轮数
        self._abort = False

        # 本轮用户消息加入讨论上下文
        msg = user_message or self.topic
        if msg:
            self.history.append({"role": "用户", "content": msg, "time": time.time()})

        while self.round_count < self.max_rounds and not self._abort:
            if self._check_token_limit():
                logger.info(f"Room {self.room_id}: token limit")
                yield json.dumps({"type": "token_warning",
                    "current": estimate_tokens(str(self.history)), "limit": self.token_limit})
                yield json.dumps({"type": "system_note", "text": "📊 Token 即将耗尽，正在生成讨论总结..."})
                summary_text = await self._make_summary()
                yield json.dumps({"type": "done", "discussion": {
                    "status": "completed", "rounds": self.round_count, "summary": summary_text}})
                self.status = RoomStatus.IDLE
                return

            if self.pending_user_msgs:
                user_msg = "\n".join(self.pending_user_msgs)
                self.pending_user_msgs.clear()
                self.history.append({"role": "用户", "content": user_msg, "time": time.time()})
                yield json.dumps({"type": "user_interject", "text": user_msg,
                    "note": "📨 用户插嘴，重新洗牌发言顺序"})
                self._shuffle_queue()

            if not self.speaking_queue:
                self._shuffle_queue()

            agent = self.speaking_queue.pop(0)
            agent_info = {"name": agent["name"], "emoji": agent["emoji"], "color": agent["color"]}
            self._current_agent = agent_info

            # 安全扫描
            recent = self.history[-3:] if len(self.history) >= 3 else self.history
            if self._injection_scan(" ".join(m["content"] for m in recent)):
                logger.warning(f"Room {self.room_id}: injection detected")
                yield json.dumps({"type": "security_warning", "message": "检测到异常指令，讨论已暂停"})
                self.status = RoomStatus.IDLE
                return

            # 构造 Prompt
            discussion_prompt = self._build_discussion_prompt(agent)
            system_msg = agent.get("system_prompt", "")
            full_system = f"{system_msg}\n\n{discussion_prompt}{SAFETY}"
            messages = [{"role": "system", "content": full_system}]

            # ─── 流式获取 LLM 回复 + 状态机解析 ───
            state = "IDLE"  # IDLE → READ_THINK → READ_PARA
            tag_buf = ""
            para_acc = ""
            think_text = ""
            paras = []
            full_response = ""

            async for evt_str in self._call_llm(messages, agent["name"]):
                if self._abort:
                    break
                evt = json.loads(evt_str)
                if evt["type"] == "error":
                    yield evt_str
                    break
                if evt["type"] != "chunk":
                    continue

                content = evt["char"]
                full_response += content
                tag_buf += content

                while True:
                    if state == "IDLE":
                        ti = tag_buf.find(TAG_THINK)
                        pi = tag_buf.find(TAG_PARA)
                        if ti != -1 and (pi == -1 or ti < pi):
                            tag_buf = tag_buf[ti + len(TAG_THINK):]
                            state = "READ_THINK"
                            continue
                        elif pi != -1:
                            tag_buf = tag_buf[pi + len(TAG_PARA):]
                            para_acc = ""
                            state = "READ_PARA"
                            continue
                        else:
                            tag_buf = tag_buf[-MAX_TAG_LEN:] if len(tag_buf) > MAX_TAG_LEN else tag_buf
                            break

                    elif state == "READ_THINK":
                        ci = tag_buf.find(TAG_THINK_END)
                        if ci != -1:
                            text = tag_buf[:ci]
                            think_text += text
                            for ch in text:
                                yield json.dumps({"type": "thinking_char", "char": ch, "agent": agent_info})
                            tag_buf = tag_buf[ci + len(TAG_THINK_END):]
                            yield json.dumps({"type": "thinking_done", "agent": agent_info})
                            state = "IDLE"
                            continue
                        else:
                            safe = max(0, len(tag_buf) - len(TAG_THINK_END))
                            text = tag_buf[:safe]
                            think_text += text
                            for ch in text:
                                yield json.dumps({"type": "thinking_char", "char": ch, "agent": agent_info})
                            tag_buf = tag_buf[safe:]
                            if len(think_text) > 3000:
                                yield json.dumps({"type": "thinking_done", "agent": agent_info})
                                state = "IDLE"
                                tag_buf = ""
                            break

                    elif state == "READ_PARA":
                        ci = tag_buf.find(TAG_PARA_END)
                        no = tag_buf.find(TAG_PARA)
                        if ci != -1:
                            para_acc += tag_buf[:ci]
                            tag_buf = tag_buf[ci + len(TAG_PARA_END):]
                            if para_acc.strip():
                                paras.append(para_acc.strip())
                                yield json.dumps({"type": "para", "text": para_acc.strip(), "agent": agent_info})
                            para_acc = ""
                            state = "IDLE"
                            continue
                        elif no != -1:
                            para_acc += tag_buf[:no]
                            tag_buf = tag_buf[no + len(TAG_PARA):]
                            if para_acc.strip():
                                paras.append(para_acc.strip())
                                yield json.dumps({"type": "para", "text": para_acc.strip(), "agent": agent_info})
                            para_acc = ""
                            continue
                        else:
                            safe = max(0, len(tag_buf) - MAX_TAG_LEN)
                            para_acc += tag_buf[:safe]
                            tag_buf = tag_buf[safe:]
                            break

            # 流结束容错
            if state == "READ_PARA":
                para_acc += tag_buf
                if para_acc.strip():
                    paras.append(para_acc.strip())
                    yield json.dumps({"type": "para", "text": para_acc.strip(), "agent": agent_info})

            if not paras and full_response.strip():
                clean = re.sub(r'</?thinking>', '', full_response)
                clean = re.sub(r'</?para>', '', clean).strip()
                if clean:
                    paras = [clean]
                    yield json.dumps({"type": "para", "text": clean, "agent": agent_info})

            # 存储到历史
            if think_text:
                self.history.append({"role": f"{agent['emoji']} {agent['name']}",
                    "content": f"<thinking>{think_text}</thinking>", "time": time.time(),
                    "type": "thinking", "agent": agent["name"]})
            for p in paras:
                if p.strip():
                    self.history.append({"role": f"{agent['emoji']} {agent['name']}",
                        "content": p.strip(), "time": time.time(), "type": "speech", "agent": agent["name"]})

            self.round_count += 1

            # done 事件：附带 discussion 元数据，前端据此判断是否继续等待
            yield json.dumps({"type": "done", "agent": agent_info,
                "think_text": think_text, "paras": paras,
                "discussion": {"round": self.round_count, "total_rounds": self.max_rounds, "status": "active"}})

        # 讨论正常结束
        if not self._abort:
            yield json.dumps({"type": "system_note", "text": "📋 讨论轮次已满，正在生成总结..."})
            summary_text = await self._make_summary()
            yield json.dumps({"type": "done", "discussion": {
                "status": "completed", "rounds": self.round_count, "summary": summary_text}})
            self.status = RoomStatus.IDLE

    async def _make_summary(self) -> str:
        """生成讨论总结（返回纯文本）"""
        self.status = RoomStatus.SUMMARIZING
        summary_prompt = self._build_summary_prompt()
        messages = [{"role": "system", "content": summary_prompt}]
        full = ""
        async for evt_str in self._call_llm(messages, "总结者"):
            evt = json.loads(evt_str)
            if evt["type"] == "error":
                break
            if evt["type"] == "chunk":
                full += evt["char"]
        _, paras = self._parse_response(full)
        return "\n\n".join(paras) if paras else full

    def _parse_response(self, text: str) -> tuple[str, list[str]]:
        """解析 AI 回复中的 <thinking> 和 <para> 标签"""
        think_text = ""
        paras = []

        think_match = re.search(r'<thinking>(.*?)</thinking>', text, re.DOTALL)
        if think_match:
            think_text = think_match.group(1).strip()

        para_matches = re.findall(r'<para>(.*?)</para>', text, re.DOTALL)
        paras = [p.strip() for p in para_matches if p.strip()]

        # 如果没有 para 标签，整段文本作为一段
        if not paras:
            clean = re.sub(r'</?thinking>', '', text)
            clean = re.sub(r'</?para>', '', clean).strip()
            if clean:
                paras = [clean]

        return think_text, paras

    def user_interrupt(self, message: str):
        """用户插嘴 — 加入等待队列，下一轮循环开头消费"""
        self.pending_user_msgs.append(message)

    def abort(self):
        """中断讨论"""
        self._abort = True
        self.status = RoomStatus.IDLE


# ============================================================
# 房间管理（内存存储）
# ============================================================

_rooms: dict[str, DiscussionRoom] = {}


def create_room(
    agents: list[dict],
    topic: str,
    api_base: str,
    api_key: str,
    model: str,
    max_rounds: int = 10,
    token_limit: int = 8000,
    temperature: float = 1.1,
    top_p: float = 0.9,
) -> DiscussionRoom:
    room_id = str(int(time.time() * 1000))[-8:]
    room = DiscussionRoom(
        room_id=room_id,
        agents=agents,
        topic=topic,
        api_base=api_base,
        api_key=api_key,
        model=model,
        max_rounds=max_rounds,
        token_limit=token_limit,
        temperature=temperature,
        top_p=top_p,
    )
    _rooms[room_id] = room

    # 清理旧房间（超过 1 小时未活动）
    now = time.time()
    stale = [rid for rid, r in _rooms.items() if now - getattr(r, 'created_at', 0) > 3600]
    for rid in stale:
        logger.info(f"Cleaning stale discussion room {rid}")
        del _rooms[rid]

    return room


def get_room(room_id: str) -> Optional[DiscussionRoom]:
    return _rooms.get(room_id)


def list_rooms() -> list[dict]:
    return [
        {
            "room_id": r.room_id,
            "topic": r.topic,
            "status": r.status,
            "round_count": r.round_count,
            "agent_count": len(r.agents),
        }
        for r in _rooms.values()
    ]
