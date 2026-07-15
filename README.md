# RealChat — 模拟真人碎片化聊天的 AI 客户端

一个解决 AI 对话"一问一答"生硬感的聊天客户端，让 AI 像真人一样聊天。

## ✨ 功能

### 🗣️ 拟真对话
- **碎片化输入等待** — 像微信一样逐条发送，AI 静静等你说完再统一回复
- **结构化分段输出** — AI 回复拆分为多条独立消息，逐条弹出，像真人在打字
- **可折叠思考过程** — AI 的推理过程折叠在 🧠 气泡里，点击展开

### 🧠 深度思考
- **7 级可调**：关闭 / 极简(512t) / 低(1k) / 中(2k) / 高(4k) / 极高(8k) / 最大(16k)
- **输入区一键开关** — 不用进设置，点一下 🧠 就切换
- **设置面板精细调控** — 选好偏好的思考等级，开关按此等级启用
- 自动适配 DeepSeek `thinking` 和 OpenAI `reasoning_effort` 参数

### 🔐 网关令牌鉴权
- 可选 Bearer Token 登录验证，防止 API Key 被盗用
- 令牌支持环境变量 `REALCHAT_AUTH_TOKEN` 或设置面板配置
- 不设令牌则跳过鉴权，与普通本地工具无异

### 💾 多会话 + 持久化
- 侧边栏管理多个对话，切换自如
- JSON 文件持久化，重启不丢失
- 支持重命名、删除会话
- 会话标题自动生成

### 🎨 其他
- 深色 / 浅色主题切换
- 完全响应式，手机也能用
- 兼容所有 OpenAI API 格式的模型（DeepSeek / OpenAI / 智谱 / 通义千问 / Moonshot…）

## 🚀 快速开始

```bash
# 1. 克隆
git clone <repo-url> && cd RealChat

# 2. 安装依赖
pip install -r requirements.txt

# 3. 设 API Key
export REALCHAT_API_KEY="sk-..."

# 4. (可选) 设网关令牌，防止他人盗用你的 Key
export REALCHAT_AUTH_TOKEN="your-secret-token"

# 5. 启动
python server.py

# 6. 打开浏览器
open http://localhost:5000
```

首次启动会自动创建 `data/settings.json`。API Key、令牌、聊天记录都存于此目录。

## ⚙️ 配置

所有配置通过环境变量或设置面板管理：

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

也可以在设置面板中随时修改，保存后即时生效。

## 🎯 使用方法

1. **分条发送**：像微信聊天一样，一条一条发，AI 会等你全部说完
2. **2 秒等待**：停止输入 2 秒后，AI 自动回复
3. **立即触发**：输入 `/done` 或直接点发送立即触发
4. **思考开关**：输入框上方 🧠 按钮一键切换深度思考
5. **清空重来**：状态栏清空按钮重置对话

## 📁 项目结构

```
RealChat/
├── server.py          # FastAPI 后端（流式解析 + 鉴权中间件）
├── config.py          # 环境变量配置
├── requirements.txt   # Python 依赖
├── .env.example       # 环境变量示例
├── .gitignore
├── README.md
├── data/              # 运行时数据（不入 git）
│   ├── sessions.json  # 聊天历史
│   └── settings.json  # 设置（含 Key / Token）
└── static/
    ├── index.html     # 前端页面
    ├── style.css      # 样式
    └── app.js         # 前端逻辑（缓冲计时 + SSE + 鉴权）
```

## 🛡️ 安全说明

- `data/` 目录已加入 `.gitignore`，不会提交到 Git
- API Key 和 Auth Token 存储在 `data/settings.json`，建议设置文件权限 `chmod 600`
- 部署到公网时务必设置 `REALCHAT_AUTH_TOKEN` 启用鉴权

## 📄 License

GPLv3
