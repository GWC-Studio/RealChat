# RealChat — 能群聊的拟真 AI 客户端

像真人聊天一样分条发送，AI 耐心倾听再分段回复。
特色：**多 AI 角色群聊辩论**，8 种预设角色 + 自定义人设，持久群聊房间。

## ✨ 功能

### 🗣️ 拟真单聊
- **碎片化输入等待** — 像微信一样逐条发送，AI 静静等你说完再统一回复
- **结构化分段输出** — AI 回复拆分为多条独立消息，逐条弹出，像真人在打字
- **可折叠思考过程** — AI 的推理过程折叠在 🧠 气泡里，点击展开

### 💬 多角色群聊
- **8 种预设角色**：分析师 / 创想家 / 质疑者 / 调解者 / 实干家 / 学者 / 预见者 / 伦理家
- **自定义角色**：自由编辑名称、Emoji、颜色、System Prompt，无限扩展
- **持久群聊房间**：创建后像普通聊天一样存在侧边栏，随时继续对话
- **每轮一条消息**：你说一句话 → 所有 AI 依次回应（2~32 轮可调）
- **随时插嘴**：AI 发言期间可以发消息，自动加入等待队列，当前发言结束后注入
- **逐字流式渲染**：每个 AI 的思考和发言实时流式输出，角色标识清晰

### 🧠 深度思考
- **7 级可调**：关闭 / 极简(512t) / 低(1k) / 中(2k) / 高(4k) / 极高(8k) / 最大(16k)
- **输入区一键开关** — 不用进设置，点一下 🧠 就切换
- 自动适配 DeepSeek `thinking` 和 OpenAI `reasoning_effort` 参数

### 📤 数据导入导出
- **选择性导出**：勾选需要备份的会话，一键下载 JSON
- **合并导入**：上传备份文件，自动去重合并（API Key / Token 不覆盖）
- 跨实例迁移数据零摩擦

### 🔐 网关令牌鉴权
- 可选 Bearer Token 登录验证
- 令牌支持环境变量 `REALCHAT_AUTH_TOKEN` 或设置面板配置

### 💾 多会话 + 持久化
- 侧边栏管理单聊/群聊，切换自如
- JSON 文件持久化，重启不丢失
- 群聊会话完整保存 Agent Prompt，旧会话可完整复现

### 🎨 其他
- 深色 / 浅色主题切换
- 完全响应式，手机也能用
- 兼容所有 OpenAI API 格式的模型（DeepSeek / OpenAI / 智谱 / 通义千问 / Moonshot…）

## 🚀 快速开始

```bash
git clone <repo-url> && cd RealChat
pip install -r requirements.txt

# 设 API Key
export REALCHAT_API_KEY="sk-..."

# (可选) 设网关令牌
export REALCHAT_AUTH_TOKEN="your-secret-token"

# 启动
python server.py

# 打开浏览器
open http://localhost:5000
```

首次启动自动创建 `data/` 目录。API Key、令牌、聊天记录、自定义角色都存于此。

## 🎯 使用方法

### 单聊
1. 像微信一样分条发送，AI 等你全部说完（2 秒静默后触发）
2. 输入 `/done` 或点发送立即触发
3. 🧠 按钮一键切换深度思考

### 群聊
1. 侧边栏点 **💬 群聊讨论** → 选择角色 + 轮数 → 创建
2. 群聊房间出现在侧边栏，输入框直接发消息开始讨论
3. AI 正在发言时可以继续打字 → 消息排队在当前发言结束后自动注入
4. 讨论结束后再发一句 → 新一轮讨论自动开始
5. 自定义角色：创建窗口点"自定义角色" → 编辑名称 / Emoji / 颜色 / Prompt

### 导入导出
- 📤 导出：勾选需备份的会话 → 下载 JSON
- 📥 导入：选择备份文件 → 自动合并去重

## ⚙️ 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `REALCHAT_API_BASE` | `https://api.deepseek.com/v1` | API 端点地址 |
| `REALCHAT_API_KEY` | (必填) | API 密钥 |
| `REALCHAT_MODEL` | `deepseek-chat` | 模型名称 |
| `REALCHAT_BUFFER_TIMEOUT` | `2.0` | 缓冲等待秒数 |
| `REALCHAT_THINKING_LEVEL` | `off` | 默认思考等级 |
| `REALCHAT_AUTH_TOKEN` | (空) | 网关鉴权令牌 |
| `REALCHAT_HOST` | `0.0.0.0` | 监听地址 |
| `REALCHAT_PORT` | `5000` | 监听端口 |

也可在设置面板中随时修改，保存后即时生效。

## 📁 项目结构

```
RealChat/
├── server.py          # FastAPI 后端（流式解析 + 鉴权 + 导入导出）
├── discussion.py      # 多智能体群聊状态机
├── config.py          # 环境变量配置
├── requirements.txt   # Python 依赖
├── .env.example
├── .gitignore
├── README.md
├── data/              # 运行时数据（不入 git）
│   ├── sessions.json         # 聊天历史（单聊 + 群聊）
│   ├── settings.json         # 设置（含 Key / Token）
│   └── custom_agents.json    # 用户自定义角色
└── static/
    ├── index.html     # 前端页面
    ├── style.css      # 样式
    └── app.js         # 前端逻辑
```

## 🛡️ 安全说明

- `data/` 目录已加入 `.gitignore`，不会提交到 Git
- 导出时 API Key 自动脱敏（`sk-a****3a`）
- 导入时不覆盖 Key / Token
- 速率限制：`/api/auth` 每 IP 每 60 秒最多 10 次
- Token 比对使用恒定时间算法
- 群聊角色 Prompt 末尾强制注入安全约束，防止指令注入

## 📄 License

GPLv3
