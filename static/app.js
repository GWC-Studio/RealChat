/**
 * RealChat v2 - Multi-chat with Sidebar
 */

const BUFFER_TIMEOUT_MS = 2000;
const MAX_BUFFER_ITEMS  = 10;

let authToken = sessionStorage.getItem('realchat_token') || '';
let authRequired = true;  // 默认需要认证，等 checkAuthStatus 更新

// ============================================================
// 认证
// ============================================================

async function checkAuthStatus() {
    try {
        const resp = await fetch('/api/auth-status');
        const data = await resp.json();
        authRequired = data.auth_required;
        if (!authRequired) {
            // 无需认证，直接进入
            hideLogin();
            return;
        }
        // 需要认证，检查是否有已保存的 token
        if (authToken) {
            const valid = await verifyToken(authToken);
            if (valid) {
                hideLogin();
                return;
            }
            authToken = '';
            sessionStorage.removeItem('realchat_token');
        }
        showLogin();
    } catch (err) {
        console.error('Auth check failed:', err);
        showLogin();
    }
}

async function verifyToken(token) {
    try {
        const resp = await fetch('/api/auth', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token }),
        });
        return resp.ok;
    } catch {
        return false;
    }
}

async function doLogin() {
    const input = $('auth-token-input');
    const errorEl = $('auth-error');
    const token = input.value.trim();
    if (!token) {
        errorEl.textContent = '请输入令牌';
        errorEl.classList.remove('hidden');
        return;
    }
    errorEl.classList.add('hidden');
    const resp = await fetch('/api/auth', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
    });
    if (resp.ok) {
        authToken = token;
        sessionStorage.setItem('realchat_token', token);
        hideLogin();
        await initApp();
    } else {
        errorEl.textContent = '令牌无效，请重试';
        errorEl.classList.remove('hidden');
        input.value = '';
        input.focus();
    }
}

function showLogin() {
    const overlay = $('auth-overlay');
    if (overlay) overlay.classList.remove('hidden');
    $('auth-token-input')?.focus();
}

function hideLogin() {
    const overlay = $('auth-overlay');
    if (overlay) overlay.classList.add('hidden');
}

async function authFetch(url, options = {}) {
    if (authToken) {
        options.headers = options.headers || {};
        options.headers['Authorization'] = 'Bearer ' + authToken;
    }
    const resp = await fetch(url, options);
    if (resp.status === 401) {
        authToken = '';
        sessionStorage.removeItem('realchat_token');
        showLogin();
        throw new Error('认证已过期，请重新登录');
    }
    return resp;
}

const STATE = { IDLE: 'idle', BUFFERING: 'buffering', STREAMING: 'streaming' };

let appState     = STATE.IDLE;
let sessionId    = '';       // 当前活跃会话 ID
let bufferMsgs   = [];
let bufferTimer  = null;
let streamAbort  = null;

// 思考框 DOM
let thinkBubble  = null;
let thinkBody    = null;
let statusMsg    = null;

// DOM
const $ = id => document.getElementById(id);
const DOM = {
    messages:        $('messages'),
    messageInput:    $('message-input'),
    sendBtn:         $('send-btn'),
    bufferIndicator: $('buffer-indicator'),
    bufferCount:     $('buffer-count'),
    statusText:      $('status-text'),
    clearBtn:        $('clear-btn'),
    themeBtn:        $('theme-btn'),
    chatArea:        $('chat-area'),
    sidebar:         $('sidebar'),
    chatList:        $('chat-list'),
    newChatBtn:      $('new-chat-btn'),
    toggleSidebarBtn:$('toggle-sidebar-btn'),
};

// ============================================================
// 深度思考控制
// ============================================================
let currentThinkingLevel = 'off';   // 偏好等级（从设置面板选）
let thinkingEnabled = false;        // 开关状态

function updateThinkingUI(level, enabled) {
    currentThinkingLevel = level;
    thinkingEnabled = enabled;
    const toggle = $('think-toggle-btn');
    if (!toggle) return;
    toggle.classList.toggle('on', enabled);
    const label = toggle.querySelector('.think-label');
    if (label) {
        const names = { off:'深度思考', minimal:'极简', low:'低', medium:'中', high:'高', extra_high:'极高', maximum:'最大' };
        label.textContent = enabled ? (names[level] || level) : '深度思考';
    }
}

async function saveThinkingEnabled(enabled) {
    try {
        await authFetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ thinking_enabled: enabled }),
        });
    } catch (err) {
        console.error('Failed to save thinking enabled:', err);
    }
}

async function toggleThinking() {
    thinkingEnabled = !thinkingEnabled;
    updateThinkingUI(currentThinkingLevel, thinkingEnabled);
    await saveThinkingEnabled(thinkingEnabled);
}

async function loadThinkingLevel() {
    try {
        const resp = await authFetch('/api/settings');
        const data = await resp.json();
        const level = data.thinking_level || 'off';
        const enabled = data.thinking_enabled === true;
        updateThinkingUI(level, enabled);
    } catch (err) {
        console.error('Failed to load thinking level:', err);
    }
}
const now = () => new Date();
const fmtTime = d => `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
const fmtDate = ts => {
    const d = new Date(ts * 1000);
    const today = new Date();
    if (d.toDateString() === today.toDateString()) {
        return fmtTime(d);
    }
    return `${d.getMonth()+1}/${d.getDate()}`;
};
const scrollBottom = () => { DOM.chatArea.scrollTop = DOM.chatArea.scrollHeight; };
function esc(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}

// ============================================================
// 侧边栏
// ============================================================
function toggleSidebar() {
    const sb = DOM.sidebar;
    const fab = document.getElementById('sidebar-toggler-fab');
    sb.classList.toggle('collapsed');
    const btn = DOM.toggleSidebarBtn;
    if (sb.classList.contains('collapsed')) {
        btn.textContent = '▶';
        btn.title = '展开侧边栏';
        if (fab) fab.classList.add('visible');
    } else {
        btn.textContent = '◀';
        btn.title = '收起侧边栏';
        if (fab) fab.classList.remove('visible');
    }
    localStorage.setItem('realchat_sidebar', sb.classList.contains('collapsed') ? 'collapsed' : 'open');
}

function restoreSidebar() {
    const fab = document.getElementById('sidebar-toggler-fab');
    if (localStorage.getItem('realchat_sidebar') === 'collapsed') {
        DOM.sidebar.classList.add('collapsed');
        DOM.toggleSidebarBtn.textContent = '▶';
        DOM.toggleSidebarBtn.title = '展开侧边栏';
        if (fab) fab.classList.add('visible');
    }
}

// ============================================================
// 主题切换
// ============================================================
function toggleTheme() {
    const html = document.documentElement;
    const isDark = html.getAttribute('data-theme') !== 'light';
    const next = isDark ? 'light' : null;
    if (next) {
        html.setAttribute('data-theme', 'light');
        DOM.themeBtn.textContent = '☀️';
        DOM.themeBtn.title = '切换深色';
    } else {
        html.removeAttribute('data-theme');
        DOM.themeBtn.textContent = '🌙';
        DOM.themeBtn.title = '切换浅色';
    }
    localStorage.setItem('realchat_theme', next || 'dark');
}

function restoreTheme() {
    const saved = localStorage.getItem('realchat_theme');
    if (saved === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
        DOM.themeBtn.textContent = '☀️';
        DOM.themeBtn.title = '切换深色';
    } else {
        DOM.themeBtn.textContent = '🌙';
        DOM.themeBtn.title = '切换浅色';
    }
}

// ============================================================
// 设置面板
// ============================================================
const SETTING_FIELDS = [
    { id: 'setting-api_base_url',       key: 'api_base_url',       type: 'string' },
    { id: 'setting-api_key',            key: 'api_key',            type: 'string' },
    { id: 'setting-auth_token',         key: 'auth_token',         type: 'string' },
    { id: 'setting-thinking_level',     key: 'thinking_level',     type: 'string' },
    { id: 'setting-model',              key: 'model',              type: 'string' },
    { id: 'setting-temperature',        key: 'temperature',        type: 'float' },
    { id: 'setting-top_p',              key: 'top_p',              type: 'float' },
    { id: 'setting-max_tokens',         key: 'max_tokens',         type: 'int' },
    { id: 'setting-frequency_penalty',  key: 'frequency_penalty',  type: 'float' },
    { id: 'setting-presence_penalty',   key: 'presence_penalty',   type: 'float' },
    { id: 'setting-seed',               key: 'seed',               type: 'seed' },
    { id: 'setting-stop',               key: 'stop',               type: 'stop' },
    { id: 'setting-prompt',             key: 'system_prompt',      type: 'string' },
];

const RANGE_DISPLAYS = {
    'setting-temperature':       'val-temp',
    'setting-top_p':             'val-topp',
    'setting-frequency_penalty': 'val-freq',
    'setting-presence_penalty':  'val-pres',
};

function bindRangeDisplays() {
    for (const [sliderId, valId] of Object.entries(RANGE_DISPLAYS)) {
        const slider = $(sliderId);
        const label  = $(valId);
        if (!slider || !label) continue;
        slider.addEventListener('input', () => {
            label.textContent = parseFloat(slider.value).toFixed(2);
        });
    }
}

async function openSettings() {
    const modal = $('settings-modal');
    modal.classList.remove('hidden');
    try {
        const resp = await authFetch('/api/settings');
        const data = await resp.json();
        for (const f of SETTING_FIELDS) {
            const el = $(f.id);
            if (!el) continue;
            const val = data[f.key];
            if (f.type === 'stop') {
                el.value = Array.isArray(val) ? val.join(', ') : '';
            } else if (f.type === 'seed') {
                el.value = (val !== null && val !== undefined) ? val : '';
            } else if (f.type === 'float' || f.type === 'int') {
                el.value = val;
                // 同步 range 显示值
                const displayId = RANGE_DISPLAYS[f.id];
                if (displayId) {
                    const label = $(displayId);
                    if (label) label.textContent = parseFloat(val).toFixed(2);
                }
            } else {
                el.value = val ?? '';
            }
        }
    } catch (err) {
        console.error('Failed to load settings:', err);
    }
}

function closeSettings() {
    $('settings-modal').classList.add('hidden');
}

async function saveSettings() {
    const payload = {};
    for (const f of SETTING_FIELDS) {
        const el = $(f.id);
        if (!el) continue;
        if (f.type === 'stop') {
            const raw = el.value.trim();
            payload[f.key] = raw ? raw.split(',').map(s => s.trim()).filter(Boolean) : [];
        } else if (f.type === 'seed') {
            const raw = el.value.trim();
            payload[f.key] = raw !== '' ? parseInt(raw, 10) : null;
        } else if (f.type === 'int') {
            payload[f.key] = parseInt(el.value, 10) || 0;
        } else if (f.type === 'float') {
            payload[f.key] = parseFloat(el.value) ?? 0;
        } else {
            payload[f.key] = el.value;
        }
    }
    if (!payload.model || !payload.model.trim()) {
        alert('请填写模型名称');
        return;
    }
    try {
        await authFetch('/api/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        closeSettings();
        // 同步思考控件 UI（等级变了但开关状态不变）
        if (payload.thinking_level) {
            updateThinkingUI(payload.thinking_level, thinkingEnabled);
        }
    } catch (err) {
        console.error('Failed to save settings:', err);
        alert('保存失败');
    }
}

async function resetSettings() {
    if (!confirm('恢复默认设置？这将覆盖所有参数。')) return;
    try {
        const resp = await authFetch('/api/settings');
        const data = await resp.json();
        for (const f of SETTING_FIELDS) {
            const el = $(f.id);
            if (!el) continue;
            if (f.type === 'stop') {
                el.value = '';
            } else if (f.type === 'seed') {
                el.value = '';
            } else if (f.type === 'float' || f.type === 'int') {
                const defaults = { temperature: 0.8, top_p: 0.9, max_tokens: 2048, frequency_penalty: 0, presence_penalty: 0 };
                el.value = defaults[f.key] ?? 0;
                const displayId = RANGE_DISPLAYS[f.id];
                if (displayId) {
                    const label = $(displayId);
                    if (label) label.textContent = parseFloat(el.value).toFixed(2);
                }
            } else {
                el.value = '';
            }
        }
        // 特殊：model 和 prompt 用 default
        $('setting-model').value = 'deepseek-chat';
    } catch {
        // fallback
        $('setting-temperature').value = 0.8;
        $('setting-top_p').value = 0.9;
        $('setting-max_tokens').value = 2048;
        $('setting-model').value = 'deepseek-chat';
    }
}

// ============================================================
// 会话列表管理
// ============================================================
async function loadChatList() {
    try {
        const resp = await authFetch('/api/sessions');
        const data = await resp.json();
        renderChatList(data.sessions || []);
    } catch (err) {
        console.error('Failed to load sessions:', err);
    }
}

function renderChatList(sessions) {
    DOM.chatList.innerHTML = '';

    if (sessions.length === 0) {
        return;
    }

    for (const s of sessions) {
        const el = document.createElement('div');
        el.className = 'chat-item' + (s.id === sessionId ? ' active' : '');
        el.dataset.id = s.id;
        el.innerHTML = `
            <span class="chat-title" title="${esc(s.title)}">${esc(s.title)}</span>
            <span class="chat-meta">
                <span class="chat-time">${fmtDate(s.updated_at)}</span>
                <span class="chat-actions">
                    <button class="chat-action-btn rename-btn" title="重命名">✏️</button>
                    <button class="chat-action-btn delete-btn" title="删除">🗑️</button>
                </span>
            </span>
        `;

        // 点击切换会话
        el.querySelector('.chat-title')?.addEventListener('click', () => switchToChat(s.id));
        el.addEventListener('click', (e) => {
            // 如果点击的是操作按钮，不切换
            if (e.target.closest('.chat-action-btn')) return;
            switchToChat(s.id);
        });

        // 重命名
        el.querySelector('.rename-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            renameChat(s.id, s.title);
        });

        // 删除
        el.querySelector('.delete-btn')?.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteChat(s.id);
        });

        DOM.chatList.appendChild(el);
    }
}

async function switchToChat(sid) {
    if (sid === sessionId) return;

    if (streamAbort) streamAbort.abort();
    cancelTimer();
    bufferMsgs = [];

    try {
        const resp = await authFetch(`/api/sessions/${sid}`);
        if (!resp.ok) throw new Error('Not found');
        const data = await resp.json();

        DOM.messages.innerHTML = '';
        removeThinkBubble();
        removeStatusMsg();

        // 优先用结构化 messages，兼容旧数据 fallback 到 history
        const msgs = data.messages || [];
        if (msgs.length > 0) {
            for (const msg of msgs) {
                if (msg.role === 'user') {
                    renderMsg('user', msg.content);
                } else if (msg.role === 'assistant') {
                    if (msg.type === 'thinking') {
                        addThinkBubbleFromHistory(msg.content);
                    } else {
                        // type === 'para' 或未指定
                        renderMsg('ai', msg.content);
                    }
                }
            }
        } else {
            // 旧数据兼容：从 history 解析原始标签
            for (const msg of data.history || []) {
                if (msg.role === 'user') {
                    renderMsg('user', msg.content);
                } else if (msg.role === 'assistant') {
                    renderParsedAI(msg.content);
                }
            }
        }

        sessionId = sid;
        setState(STATE.IDLE);
        await loadChatList();
        scrollBottom();
    } catch (err) {
        console.error('Failed to switch chat:', err);
    }
}

/** 从历史记录渲染已解析的 AI 消息 */
function renderParsedAI(rawText) {
    // 提取 thinking
    const thinkMatch = rawText.match(/<thinking>([\s\S]*?)<\/thinking>/);
    if (thinkMatch && thinkMatch[1].trim()) {
        addThinkBubbleFromHistory(thinkMatch[1].trim());
    }
    // 提取所有 para
    const paraRegex = /<para>([\s\S]*?)<\/para>/g;
    let m;
    while ((m = paraRegex.exec(rawText)) !== null) {
        if (m[1].trim()) {
            renderMsg('ai', m[1].trim());
        }
    }
    // 如果没有匹配到任何标签，显示纯文本
    if (!thinkMatch && !paraRegex.test(rawText)) {
        const clean = rawText.replace(/<\/?thinking>/g, '').replace(/<\/?para>/g, '').trim();
        if (clean) renderMsg('ai', clean);
    }
}

/** 插入一个折叠的思考气泡（用于历史加载） */
function addThinkBubbleFromHistory(text) {
    const el = document.createElement('div');
    el.className = 'thinking-bubble';
    el.innerHTML = `
        <div class="thinking-header" onclick="toggleThinkBubble(this.parentElement)">
            🧠 AI思考过程 <span class="arrow">▶</span>
        </div>
        <div class="thinking-body">${esc(text)}</div>
    `;
    DOM.messages.appendChild(el);
}

async function newChat() {
    if (streamAbort) streamAbort.abort();
    cancelTimer();
    bufferMsgs = [];

    // 创建新会话
    try {
        const resp = await authFetch('/api/sessions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: '新对话' }),
        });
        const data = await resp.json();
        sessionId = data.session_id;
    } catch {
        sessionId = '';
    }

    // 清空聊天区
    DOM.messages.innerHTML = `<div class="welcome-msg">
        <h2>🦞 RealChat</h2>
        <p>像真人聊天一样，分条发送，AI 耐心倾听</p>
    </div>`;
    removeThinkBubble();
    removeStatusMsg();
    setState(STATE.IDLE);
    await loadChatList();
    DOM.messageInput.focus();
}

async function renameChat(sid, oldTitle) {
    const newTitle = prompt('新标题:', oldTitle);
    if (!newTitle || newTitle.trim() === '' || newTitle.trim() === oldTitle) return;

    try {
        await authFetch(`/api/sessions/${sid}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle.trim() }),
        });
        await loadChatList();
    } catch (err) {
        console.error('Rename failed:', err);
    }
}

async function deleteChat(sid) {
    if (!confirm('确定删除这个对话？')) return;

    try {
        await authFetch(`/api/sessions/${sid}`, { method: 'DELETE' });

        if (sid === sessionId) {
            // 删除的是当前会话 → 新建一个空会话
            sessionId = '';
            DOM.messages.innerHTML = `<div class="welcome-msg">
                <h2>🦞 RealChat</h2>
                <p>像真人聊天一样，分条发送，AI 耐心倾听</p>
            </div>`;
            removeThinkBubble();
            removeStatusMsg();
            setState(STATE.IDLE);
        }
        await loadChatList();
    } catch (err) {
        console.error('Delete failed:', err);
    }
}

// ============================================================
// 消息渲染
// ============================================================
function renderMsg(role, text) {
    const el = document.createElement('div');
    el.className = `message ${role}`;
    el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
    DOM.messages.appendChild(el);
}

function addUserMsg(text) {
    renderMsg('user', text);
    scrollBottom();
}

function addAIMsg(text) {
    renderMsg('ai', text);
    scrollBottom();
}

// ============================================================
// 状态消息 & 思考气泡
// ============================================================
function addStatusMsg(text) {
    removeStatusMsg();
    const el = document.createElement('div');
    el.className = 'message ai status-msg';
    el.id = 'status-msg';
    el.innerHTML = `<div class="bubble">${esc(text)}</div>`;
    DOM.messages.appendChild(el);
    statusMsg = el;
    scrollBottom();
}

function removeStatusMsg() {
    if (statusMsg) { statusMsg.remove(); statusMsg = null; }
}

function createThinkBubble() {
    if (thinkBubble) return;
    const el = document.createElement('div');
    el.className = 'thinking-bubble';
    el.id = 'thinking-bubble';
    el.innerHTML = `
        <div class="thinking-header" onclick="toggleThinkBubble(this.parentElement)">
            🧠 AI思考过程 <span class="arrow">▶</span>
        </div>
        <div class="thinking-body"></div>
    `;
    DOM.messages.appendChild(el);
    thinkBubble = el;
    thinkBody = el.querySelector('.thinking-body');
    scrollBottom();
}

function appendThinkChar(ch) {
    if (!thinkBubble) createThinkBubble();
    thinkBody.textContent += ch;
    scrollBottom();
}

window.toggleThinkBubble = function(bubble) {
    bubble.classList.toggle('expanded');
};

function removeThinkBubble() {
    if (thinkBubble) { thinkBubble.remove(); thinkBubble = null; thinkBody = null; }
}

// ============================================================
// 状态切换
// ============================================================
function setState(s) {
    appState = s;
    const input = DOM.messageInput;
    const btn   = DOM.sendBtn;
    const indic = DOM.bufferIndicator;
    const st    = DOM.statusText;

    switch (s) {
        case STATE.IDLE:
            btn.disabled = false; input.disabled = false; input.focus();
            st.textContent = '就绪';
            indic.classList.add('hidden');
            removeStatusMsg();
            break;
        case STATE.BUFFERING:
            btn.disabled = false; input.disabled = false;
            st.textContent = '等待中…';
            indic.classList.remove('hidden');
            DOM.bufferCount.textContent = bufferMsgs.length;
            addStatusMsg('AI思考中…');
            break;
        case STATE.STREAMING:
            btn.disabled = true; input.disabled = true;
            st.textContent = 'AI 回复中…';
            indic.classList.remove('hidden');
            DOM.bufferCount.textContent = bufferMsgs.length + ' → 已发送';
            break;
    }
}

// ============================================================
// 缓冲计时器
// ============================================================
function resetTimer() {
    clearTimeout(bufferTimer);
    bufferTimer = setTimeout(onTimerFire, BUFFER_TIMEOUT_MS);
    setState(STATE.BUFFERING);
}
function cancelTimer() {
    clearTimeout(bufferTimer);
    bufferTimer = null;
}

async function onTimerFire() {
    if (bufferMsgs.length === 0) { setState(STATE.IDLE); return; }
    const userInput = bufferMsgs.join('\n');
    const segments = [...bufferMsgs];  // 保存副本
    bufferMsgs = [];
    setState(STATE.STREAMING);
    await callStreamAPI(userInput, segments);
}

// ============================================================
// SSE 流式调用
// ============================================================
async function callStreamAPI(userInput, userSegments = null) {
    // userSegments: 独立消息数组，用于结构化存储
    const segments = userSegments || [userInput];
    removeThinkBubble();
    removeStatusMsg();
    let thinkingStarted = false;
    addStatusMsg('AI思考中…');

    streamAbort = new AbortController();

    try {
        const resp = await authFetch('/api/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_input: userInput,
                user_segments: segments,
                session_id: sessionId,
            }),
            signal: streamAbort.signal,
        });
        if (!resp.ok) {
            const err = await resp.text();
            throw new Error(`HTTP ${resp.status}: ${err}`);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let leftover = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            leftover += decoder.decode(value, { stream: true });
            const parts = leftover.split('\n\n');
            leftover = parts.pop();
            for (const p of parts) {
                const ev = parseSSE(p);
                if (ev) {
                    if (ev.type === 'thinking_char' && !thinkingStarted) {
                        removeStatusMsg();
                        createThinkBubble();
                        thinkingStarted = true;
                    }
                    handleSSEEvent(ev);
                }
            }
        }
        if (leftover.trim()) {
            const ev = parseSSE(leftover);
            if (ev) {
                if (ev.type === 'thinking_char' && !thinkingStarted) {
                    removeStatusMsg();
                    createThinkBubble();
                    thinkingStarted = true;
                }
                handleSSEEvent(ev);
            }
        }

    } catch (err) {
        if (err.name !== 'AbortError') {
            console.error('Stream error:', err);
            removeStatusMsg();
            addAIMsg('⚠️ ' + err.message);
        }
    } finally {
        streamAbort = null;
        removeStatusMsg();
        setState(STATE.IDLE);
        // 刷新侧边栏列表（标题可能已自动更新）
        await loadChatList();
    }
}

function parseSSE(raw) {
    let evType = '', dataStr = '';
    for (const line of raw.split('\n')) {
        if (line.startsWith('event: ')) evType = line.slice(7).trim();
        else if (line.startsWith('data: ')) dataStr = line.slice(6).trim();
    }
    if (!evType || !dataStr) return null;
    try { return { type: evType, data: JSON.parse(dataStr) }; } catch { return null; }
}

function handleSSEEvent(ev) {
    switch (ev.type) {
        case 'thinking_char':
            appendThinkChar(ev.data.char || '');
            break;
        case 'thinking_done':
            break;
        case 'para':
            if (ev.data.text?.trim()) addAIMsg(ev.data.text.trim());
            break;
        case 'done':
            if (ev.data.session_id && !sessionId) {
                sessionId = ev.data.session_id;
            }
            console.log('AI done,', ev.data.ai_full_text?.length || 0, 'chars');
            break;
        case 'error':
            removeStatusMsg();
            addAIMsg('⚠️ ' + (ev.data.message || '未知错误'));
            break;
    }
}

// ============================================================
// 用户输入
// ============================================================
function sendMessage() {
    const input = DOM.messageInput;
    const text = input.value.trim();
    if (!text) return;

    // 隐藏欢迎页
    const welcome = DOM.messages.querySelector('.welcome-msg');
    if (welcome) welcome.remove();

    if (text === '/done') {
        input.value = '';
        triggerNow();
        return;
    }

    addUserMsg(text);
    input.value = '';
    input.focus();

    if (bufferMsgs.length >= MAX_BUFFER_ITEMS) {
        cancelTimer();
        const old = bufferMsgs.join('\n');
        const oldSegments = [...bufferMsgs];
        bufferMsgs = [text];
        setState(STATE.STREAMING);
        callStreamAPI(old, oldSegments).then(() => {
            if (bufferMsgs.length) resetTimer();
        });
        return;
    }

    bufferMsgs.push(text);
    resetTimer();
}

function triggerNow() {
    cancelTimer();
    if (bufferMsgs.length === 0) { setState(STATE.IDLE); return; }
    const userInput = bufferMsgs.join('\n');
    const segments = [...bufferMsgs];
    bufferMsgs = [];
    setState(STATE.STREAMING);
    callStreamAPI(userInput, segments);
}

function onInputActivity() {
    if (appState === STATE.BUFFERING && DOM.messageInput.value !== '') {
        cancelTimer();
        bufferTimer = setTimeout(onTimerFire, BUFFER_TIMEOUT_MS);
    }
}

async function clearCurrentChat() {
    if (streamAbort) streamAbort.abort();
    cancelTimer();
    bufferMsgs = [];
    removeStatusMsg();
    removeThinkBubble();

    if (sessionId) {
        try {
            await authFetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
        } catch {}
    }

    sessionId = '';
    DOM.messages.innerHTML = `<div class="welcome-msg">
        <h2>🦞 RealChat</h2>
        <p>像真人聊天一样，分条发送，AI 耐心倾听</p>
    </div>`;
    setState(STATE.IDLE);
    await loadChatList();
    DOM.messageInput.focus();
}

// ============================================================
// 事件绑定
// ============================================================

// 登录事件
$('auth-login-btn')?.addEventListener('click', doLogin);
$('auth-token-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); doLogin(); }
});

DOM.sendBtn.addEventListener('click', sendMessage);
DOM.messageInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});
DOM.messageInput.addEventListener('input', onInputActivity);
DOM.clearBtn.addEventListener('click', clearCurrentChat);
DOM.themeBtn.addEventListener('click', toggleTheme);
DOM.newChatBtn.addEventListener('click', newChat);
DOM.toggleSidebarBtn.addEventListener('click', toggleSidebar);
document.getElementById('sidebar-toggler-fab')?.addEventListener('click', toggleSidebar);

// 设置面板事件
$('settings-btn')?.addEventListener('click', openSettings);
$('modal-close-btn')?.addEventListener('click', closeSettings);
$('setting-save-btn')?.addEventListener('click', saveSettings);
$('setting-reset-btn')?.addEventListener('click', resetSettings);

// 深度思考开关事件
$('think-toggle-btn')?.addEventListener('click', toggleThinking);
// 点击遮罩关闭
$('settings-modal')?.addEventListener('click', (e) => {
    if (e.target === $('settings-modal')) closeSettings();
});
// ESC 关闭
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('settings-modal').classList.contains('hidden')) {
        closeSettings();
    }
});

document.querySelector('.done-cmd')?.addEventListener('click', () => {
    DOM.messageInput.value = '/done';
    sendMessage();
});

// ============================================================
// Init
// ============================================================
async function initApp() {
    restoreSidebar();
    restoreTheme();
    bindRangeDisplays();
    await loadThinkingLevel();
    await loadChatList();
    setState(STATE.IDLE);
    DOM.messageInput.focus();
    console.log('🦞 RealChat v2 ready');
}

async function init() {
    restoreSidebar();
    restoreTheme();
    await checkAuthStatus();
    if (!authRequired || authToken) {
        await initApp();
    }
}

init();
