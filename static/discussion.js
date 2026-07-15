/**
 * RealChat — Discussion Mode
 * 多智能体群聊讨论前端
 */

const $ = id => document.getElementById(id);

let selectedAgents = new Set();
let roomId = '';
let isDiscussing = false;

// ============================================================
// Init
// ============================================================

async function init() {
    await restoreTheme();
    await loadAgents();
    bindEvents();
}

function restoreTheme() {
    const saved = localStorage.getItem('realchat_theme');
    if (saved === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
    }
}

// ============================================================
// Agent 加载 & 选择
// ============================================================

async function loadAgents() {
    try {
        const token = sessionStorage.getItem('realchat_token') || '';
        const headers = token ? { 'Authorization': 'Bearer ' + token } : {};
        const resp = await fetch('/api/discussion/agents', { headers });
        const data = await resp.json();
        renderAgentChips(data.agents || []);
    } catch (err) {
        console.error('Failed to load agents:', err);
    }
}

function renderAgentChips(agents) {
    const container = $('agent-selector');
    selectedAgents.clear();
    agents.forEach((a, i) => {
        const chip = document.createElement('div');
        chip.className = 'agent-chip' + (i < 3 ? ' selected' : '');
        chip.style.setProperty('--chip-color', a.color);
        chip.dataset.name = a.name;
        chip.innerHTML = `<span class="chip-emoji">${a.emoji}</span> ${a.name}`;
        chip.addEventListener('click', () => toggleAgent(chip, a.name));
        container.appendChild(chip);
        if (i < 3) selectedAgents.add(a.name);
    });
    updateStartBtn();
}

function toggleAgent(chip, name) {
    if (selectedAgents.has(name)) {
        selectedAgents.delete(name);
        chip.classList.remove('selected');
    } else {
        if (selectedAgents.size >= 5) {
            alert('最多选择 5 个角色');
            return;
        }
        selectedAgents.add(name);
        chip.classList.add('selected');
    }
    updateStartBtn();
}

function updateStartBtn() {
    const btn = $('start-btn');
    const topic = $('topic-input').value.trim();
    btn.disabled = !topic || selectedAgents.size < 2;
}

// ============================================================
// Events
// ============================================================

function bindEvents() {
    $('topic-input').addEventListener('input', updateStartBtn);
    $('topic-input').addEventListener('keydown', e => {
        if (e.key === 'Enter' && !$('start-btn').disabled) startDiscussion();
    });
    $('start-btn').addEventListener('click', startDiscussion);
    $('interrupt-btn').addEventListener('click', sendInterrupt);
    $('interrupt-input').addEventListener('keydown', e => {
        if (e.key === 'Enter') sendInterrupt();
    });
    $('abort-btn').addEventListener('click', abortDiscussion);
}

// ============================================================
// Discussion Flow
// ============================================================

async function startDiscussion() {
    const topic = $('topic-input').value.trim();
    if (!topic || selectedAgents.size < 2) return;

    const maxRounds = parseInt($('max-rounds-select').value);
    const agentNames = [...selectedAgents];

    try {
        const token = sessionStorage.getItem('realchat_token') || '';
        const headers = { 'Content-Type': 'application/json' };
        if (token) headers['Authorization'] = 'Bearer ' + token;

        const resp = await fetch('/api/discussion/start', {
            method: 'POST',
            headers,
            body: JSON.stringify({
                topic,
                agents: agentNames,
                max_rounds: maxRounds,
            }),
        });
        if (!resp.ok) {
            const err = await resp.json();
            alert(err.detail || '启动失败');
            return;
        }
        const data = await resp.json();
        roomId = data.room_id;

        // Switch to discussion mode view
        enterDiscussionMode(data);
    } catch (err) {
        console.error('Failed to start discussion:', err);
        alert('启动讨论失败');
    }
}

function enterDiscussionMode(data) {
    // Hide setup, show discussion UI
    $('setup-form').style.display = 'none';
    $('status-bar-discussion').classList.remove('hidden');
    $('input-area-discussion').classList.remove('hidden');
    $('abort-btn').classList.remove('hidden');
    $('interrupt-input').disabled = true;
    $('interrupt-btn').disabled = true;

    // Clear welcome message
    $('discussion-messages').innerHTML = '';

    // Show topic
    addSystemNote(`📋 讨论主题：<b>${escHtml(data.topic)}</b>`, '');
    addSystemNote(`参与角色：${data.agents.map(a => a.emoji + a.name).join('、')}`, '');

    isDiscussing = true;

    // Connect SSE stream
    connectStream();
}

function connectStream() {
    const token = sessionStorage.getItem('realchat_token') || '';
    const headers = {};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const url = `/api/discussion/stream?room_id=${roomId}`;

    fetch(url, { headers })
        .then(resp => {
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';

            function pump() {
                reader.read().then(({ done, value }) => {
                    if (done) {
                        isDiscussing = false;
                        resetDiscussionUI();
                        return;
                    }
                    buf += decoder.decode(value, { stream: true });
                    const lines = buf.split('\n');
                    buf = lines.pop() || '';
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            try {
                                const data = JSON.parse(line.slice(6));
                                handleDiscussionEvent(data);
                            } catch (e) {}
                        }
                    }
                    pump();
                }).catch(() => {
                    isDiscussing = false;
                    resetDiscussionUI();
                });
            }
            pump();
        })
        .catch(() => {
            isDiscussing = false;
            resetDiscussionUI();
            addSystemNote('⚠️ 连接讨论流失败', '');
        });
}

function handleDiscussionEvent(data) {
    switch (data.type) {
        case 'speaker_start':
            handleSpeakerStart(data);
            break;
        case 'chunk':
            handleChunk(data);
            break;
        case 'speaker_done':
            handleSpeakerDone(data);
            break;
        case 'user_interject':
            handleUserInterject(data);
            break;
        case 'token_warning':
            handleTokenWarning(data);
            break;
        case 'system_note':
            addSystemNote(data.text, '');
            break;
        case 'summary_chunk':
            handleSummaryChunk(data);
            break;
        case 'discussion_done':
            handleDiscussionDone(data);
            break;
        case 'security_warning':
            handleSecurityWarning(data);
            break;
        case 'stream_end':
            isDiscussing = false;
            resetDiscussionUI();
            break;
        case 'error':
            addSystemNote('⚠️ ' + (data.message || '错误'), '');
            break;
    }
}

// ============================================================
// Event Handlers
// ============================================================

let currentSpeaker = null;
let currentBubble = null;
let currentThinking = null;

function handleSpeakerStart(data) {
    currentSpeaker = data;
    currentBubble = null;
    currentThinking = null;

    // Update status bar
    $('status-icon').textContent = data.emoji;
    $('status-text-discussion').textContent = `${data.emoji} ${data.agent} 正在发言...`;

    // Show thinking placeholder
    const msgs = $('discussion-messages');
    const thinkDiv = document.createElement('div');
    thinkDiv.className = 'agent-thinking';
    thinkDiv.innerHTML = `
        <div class="thinking-header" onclick="toggleDiscussionThink(this.parentElement)">
            🧠 ${data.agent} 思考中... <span class="arrow">▶</span>
        </div>
        <div class="thinking-body"></div>
    `;
    msgs.appendChild(thinkDiv);
    currentThinking = thinkDiv;
    scrollDiscussionBottom();
}

function handleChunk(data) {
    if (!currentSpeaker) return;
    // raw chunks from LLM - we accumulate and parse on speaker_done
}

function handleSpeakerDone(data) {
    if (!currentSpeaker) return;

    // Update status
    $('status-icon').textContent = '💬';
    $('status-text-discussion').textContent = '等待下一位发言者...';
    $('status-rounds').textContent = `第 ${data.round}/${data.total_rounds} 轮`;

    // Update thinking bubble if exists
    if (currentThinking && data.think_text) {
        const body = currentThinking.querySelector('.thinking-body');
        if (body) body.textContent = data.think_text;
        const header = currentThinking.querySelector('.thinking-header');
        if (header) header.innerHTML = `🧠 ${currentSpeaker.agent} 的思考 <span class="arrow">▶</span>`;
        if (!data.think_text.trim()) {
            currentThinking.remove();
            currentThinking = null;
        }
    }

    // Render speech paragraphs
    const msgs = $('discussion-messages');
    (data.paras || []).forEach(para => {
        if (!para.trim()) return;
        const msgDiv = document.createElement('div');
        msgDiv.className = 'agent-msg';
        msgDiv.innerHTML = `
            <div class="agent-msg-header" style="color:${currentSpeaker.color}">
                ${currentSpeaker.emoji} ${currentSpeaker.agent}
            </div>
            <div class="bubble" style="--agent-color:${currentSpeaker.color}">${escHtml(para)}</div>
        `;
        msgs.appendChild(msgDiv);
    });

    currentSpeaker = null;
    currentBubble = null;
    scrollDiscussionBottom();

    // Enable interrupt input after first speaker
    $('interrupt-input').disabled = false;
    $('interrupt-btn').disabled = false;
}

let summaryAccum = '';
let summaryBox = null;

function handleSummaryChunk(data) {
    if (!summaryBox) {
        const msgs = $('discussion-messages');
        summaryBox = document.createElement('div');
        summaryBox.className = 'summary-box';
        summaryBox.innerHTML = '<h3>📊 讨论总结</h3><div class="summary-text"></div>';
        msgs.appendChild(summaryBox);
    }
    summaryAccum += data.char;
    const textDiv = summaryBox.querySelector('.summary-text');
    if (textDiv) textDiv.textContent = summaryAccum;
    scrollDiscussionBottom();
}

function handleDiscussionDone(data) {
    isDiscussing = false;
    if (window._discussionEventSource) {
        window._discussionEventSource.close();
    }
    $('status-icon').textContent = '✅';
    $('status-text-discussion').textContent = '讨论结束';
    $('status-rounds').textContent = `共 ${data.rounds} 轮`;

    // If summary wasn't streamed, show it
    if (!summaryBox && data.summary) {
        const msgs = $('discussion-messages');
        summaryBox = document.createElement('div');
        summaryBox.className = 'summary-box';
        summaryBox.innerHTML = `<h3>📊 讨论总结</h3><div class="summary-text">${escHtml(data.summary)}</div>`;
        msgs.appendChild(summaryBox);
    }

    resetDiscussionUI();
}

function handleUserInterject(data) {
    const msgs = $('discussion-messages');
    const div = document.createElement('div');
    div.className = 'user-interject';
    div.innerHTML = `<span class="interject-badge">📨 你插嘴了：${escHtml(data.text)}</span>`;
    msgs.appendChild(div);
    if (data.note) {
        addSystemNote(data.note, 'separator');
    }
    scrollDiscussionBottom();
}

function handleTokenWarning(data) {
    addSystemNote(`⚠️ Token 即将耗尽 (${data.current}/${data.limit})，正在生成总结...`, '');
}

function handleSecurityWarning(data) {
    const msgs = $('discussion-messages');
    const div = document.createElement('div');
    div.className = 'security-warning';
    div.textContent = '🚨 ' + (data.message || '检测到异常指令，讨论已暂停');
    msgs.appendChild(div);
    isDiscussing = false;
    resetDiscussionUI();
}

// ============================================================
// User Interaction
// ============================================================

async function sendInterrupt() {
    const input = $('interrupt-input');
    const msg = input.value.trim();
    if (!msg || !roomId) return;

    // Show immediately in UI
    const msgs = $('discussion-messages');
    const div = document.createElement('div');
    div.className = 'user-interject';
    div.innerHTML = `<span class="interject-badge">💬 你：${escHtml(msg)}</span>`;
    msgs.appendChild(div);
    scrollDiscussionBottom();

    input.value = '';

    try {
        const token = sessionStorage.getItem('realchat_token') || '';
        const headers = { 'Content-Type': 'application/json' };
        if (token) headers['Authorization'] = 'Bearer ' + token;
        await fetch('/api/discussion/interrupt', {
            method: 'POST',
            headers,
            body: JSON.stringify({ room_id: roomId, message: msg }),
        });
    } catch (err) {
        console.error('Interrupt failed:', err);
    }
}

async function abortDiscussion() {
    if (!roomId) return;
    try {
        const token = sessionStorage.getItem('realchat_token') || '';
        const headers = { 'Content-Type': 'application/json' };
        if (token) headers['Authorization'] = 'Bearer ' + token;
        await fetch('/api/discussion/abort', {
            method: 'POST',
            headers,
            body: JSON.stringify({ room_id: roomId }),
        });
    } catch {}
    isDiscussing = false;
    if (window._discussionEventSource) {
        window._discussionEventSource.close();
    }
    resetDiscussionUI();
    addSystemNote('⏹ 讨论已中断', '');
}

function resetDiscussionUI() {
    $('interrupt-input').disabled = true;
    $('interrupt-btn').disabled = true;
    $('abort-btn').classList.add('hidden');
    $('status-rounds').textContent = '';
}

// ============================================================
// Helpers
// ============================================================

function addSystemNote(text, cls) {
    const msgs = $('discussion-messages');
    const div = document.createElement('div');
    div.className = 'system-note' + (cls ? ' ' + cls : '');
    div.innerHTML = text;
    msgs.appendChild(div);
    scrollDiscussionBottom();
}

function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}

function scrollDiscussionBottom() {
    const msgs = $('discussion-messages');
    setTimeout(() => { msgs.scrollTop = msgs.scrollHeight; }, 50);
}

// Global toggle for thinking bubbles
window.toggleDiscussionThink = function(el) {
    el.classList.toggle('expanded');
};

// ============================================================
// Start
// ============================================================

init();
