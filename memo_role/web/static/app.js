/* 聊天页逻辑：会话列表、气泡渲染、SSE 流式接收。
   纯原生实现，无框架、无构建步骤。 */
(function () {
  'use strict';

  var LS_KEY = 'memo-role:last-session';

  var state = {
    sessions: [],
    current: null,
    personas: [],
    defaultPersona: '',
    models: [],
    openaiStyle: false,
    defaultModel: '',
    commands: [],
    prefix: '/',
    helpText: '',
    streaming: false,
    controller: null,
    stuck: true // 用户是否停在底部（决定新内容是否自动滚动）
  };

  var els = {};

  document.addEventListener('DOMContentLoaded', boot);

  // ----------------------------------------------------------------
  // 启动
  // ----------------------------------------------------------------
  async function boot() {
    els.sidebar = document.getElementById('sidebar');
    els.scrim = document.getElementById('scrim');
    els.sessionList = document.getElementById('session-list');
    els.messages = document.getElementById('messages');
    els.title = document.getElementById('chat-title');
    els.badges = document.getElementById('chat-badges');
    els.personaSelect = document.getElementById('persona-select');
    els.modelSelect = document.getElementById('model-select');
    els.input = document.getElementById('input');
    els.sendBtn = document.getElementById('btn-send');
    els.stopBtn = document.getElementById('btn-stop');
    els.resetBtn = document.getElementById('btn-reset');
    els.chips = document.getElementById('command-chips');
    document.getElementById('nav').innerHTML = MR.headerNav('/');

    bindEvents();
    syncPlaceholder();
    renderEmpty();

    // 基础数据并行加载；任一失败只提示，不阻塞页面
    await Promise.all([loadPersonas(), loadModels(), loadCommands()]);
    await loadSessions();
    restoreLast();
  }

  /**
   * 窄屏下输入框里的完整提示会折成两行、把底栏整体撑高，
   * 这里换成一行版；桌面端保留 Enter / Shift+Enter 的说明。
   */
  function syncPlaceholder() {
    var narrow = window.matchMedia('(max-width: 720px)');
    var apply = function () {
      els.input.placeholder = narrow.matches
        ? '说点什么…'
        : '说点什么…（Enter 发送，Shift+Enter 换行）';
    };
    apply();
    if (narrow.addEventListener) narrow.addEventListener('change', apply);
  }

  function bindEvents() {
    document.getElementById('new-session').addEventListener('click', newSession);
    els.resetBtn.addEventListener('click', resetSession);
    els.sendBtn.addEventListener('click', onSend);
    els.stopBtn.addEventListener('click', stopStream);
    els.input.addEventListener('input', autoGrow);
    els.input.addEventListener('keydown', onInputKey);
    els.messages.addEventListener('scroll', onMessagesScroll);
    els.sessionList.addEventListener('click', onSessionListClick);
    els.chips.addEventListener('click', onChipClick);
    els.personaSelect.addEventListener('change', function () {
      switchBinding('persona', els.personaSelect.value);
    });
    els.modelSelect.addEventListener('change', function () {
      switchBinding('model', els.modelSelect.value);
    });
    document.getElementById('sidebar-open').addEventListener('click', openSidebar);
    document.getElementById('sidebar-close').addEventListener('click', closeSidebar);
    els.scrim.addEventListener('click', closeSidebar);
  }

  // ----------------------------------------------------------------
  // 数据加载
  // ----------------------------------------------------------------
  async function loadPersonas() {
    try {
      var data = await MR.get('/api/personas');
      state.personas = data.personas || [];
      state.defaultPersona = data.default || '';
      renderSelects();
    } catch (err) {
      MR.toast('人设加载失败：' + err.message, 'error');
    }
  }

  async function loadModels() {
    try {
      var data = await MR.get('/api/models');
      state.models = data.models || [];
      state.openaiStyle = !!data.openai_style;
      state.defaultModel = data.default_model || '';
      renderSelects();
      renderModelWarn();
    } catch (err) {
      MR.toast('模型列表加载失败：' + err.message, 'error');
    }
  }

  /**
   * 没有任何可用模型时先给出提示。
   * 否则用户第一条消息只会看到一句「模型文件不存在」，不知道该去哪配。
   */
  function renderModelWarn() {
    var el = document.getElementById('model-warn');
    if (!el) return;
    // 接口模式下模型名可任意指定，不存在「本地没有模型」的问题
    if (state.openaiStyle || !state.models.length) { el.hidden = true; return; }
    var ready = state.models.filter(function (m) { return m.downloaded; });
    if (ready.length) { el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = '还没有可用模型，<b>真实聊天会失败</b>（指令、人设、记忆、文件管理照常）。' +
      '请把 GGUF 放进模型目录，再到<a href="/admin">管理后台 · 模型</a>设为默认。';
  }

  async function loadCommands() {
    try {
      var data = await MR.get('/api/dialogue/commands');
      state.commands = data.commands || [];
      state.prefix = data.prefix || '/';
      state.helpText = data.help || '';
      renderChips();
    } catch (err) {
      els.chips.innerHTML = '';
    }
  }

  async function loadSessions() {
    try {
      var data = await MR.get('/api/dialogue/sessions' + MR.qs({ limit: 50, offset: 0 }));
      state.sessions = data.sessions || [];
      renderSessionList();
    } catch (err) {
      state.sessions = [];
      renderSessionList();
      MR.toast('会话列表加载失败：' + err.message, 'error');
    }
  }

  function restoreLast() {
    var last = null;
    try { last = localStorage.getItem(LS_KEY); } catch (e) { last = null; }
    if (last && state.sessions.some(function (s) { return s.id === last; })) {
      selectSession(last);
    } else if (state.sessions.length) {
      selectSession(state.sessions[0].id);
    } else {
      renderEmpty();
    }
  }

  // ----------------------------------------------------------------
  // 会话操作
  // ----------------------------------------------------------------
  async function newSession() {
    try {
      var data = await MR.post('/api/dialogue/sessions', {
        session_id: null, title: '', persona_id: '', model_id: ''
      });
      await loadSessions();
      await selectSession(data.session.id);
      MR.toast('已新建会话', 'success');
      els.input.focus();
    } catch (err) {
      MR.toast(err.message, 'error');
    }
  }

  async function selectSession(id) {
    try {
      var data = await MR.get('/api/dialogue/sessions/' + encodeURIComponent(id) + MR.qs({ message_limit: 100 }));
      state.current = data.session;
      try { localStorage.setItem(LS_KEY, id); } catch (e) { /* 忽略隐私模式下的写入失败 */ }
      renderSessionList();
      renderHeader();
      renderMessages(data.messages || []);
      closeSidebar();
    } catch (err) {
      MR.toast('会话打开失败：' + err.message, 'error');
    }
  }

  async function deleteSession(id) {
    var ok = await MR.confirm('确定删除会话「' + id + '」及其消息？长期记忆不会受影响。', {
      danger: true, title: '删除会话'
    });
    if (!ok) return;
    try {
      await MR.del('/api/dialogue/sessions/' + encodeURIComponent(id));
      if (state.current && state.current.id === id) {
        state.current = null;
        try { localStorage.removeItem(LS_KEY); } catch (e) { /* 忽略 */ }
      }
      await loadSessions();
      if (state.current) {
        renderHeader();
        renderMessages([]);
      } else if (state.sessions.length) {
        await selectSession(state.sessions[0].id);
      } else {
        renderEmpty();
      }
      MR.toast('已删除会话', 'success');
    } catch (err) {
      MR.toast(err.message, 'error');
    }
  }

  /** 重命名会话（标题只影响列表展示，不动消息与记忆）。 */
  async function renameSession(id) {
    var target = state.sessions.filter(function (s) { return s.id === id; })[0] || { id: id };
    var mask = MR.openModal({
      title: '重命名会话',
      width: '420px',
      bodyHtml: '<div class="field"><label>标题（最长 60 字）</label>' +
        '<input id="rn-title" maxlength="60" placeholder="给这个会话起个名字"></div>' +
        '<div class="hint mono">' + MR.escape(id) + '</div>',
      footHtml: '<button class="btn btn-ghost" data-close="1">取消</button>' +
        '<button class="btn btn-primary" id="rn-save">保存</button>'
    });
    var input = mask.querySelector('#rn-title');
    input.value = target.title || '';
    input.select();

    async function save() {
      var title = input.value.trim();
      if (!title) { MR.toast('标题不能为空', 'error'); return; }
      try {
        var data = await MR.patch('/api/dialogue/sessions/' + encodeURIComponent(id), { title: title });
        if (state.current && state.current.id === id) {
          state.current = data.session;
          renderHeader();
        }
        mask.close();
        await loadSessions();
        MR.toast('已重命名', 'success');
      } catch (err) {
        MR.toast(err.message, 'error');
      }
    }

    mask.querySelector('#rn-save').addEventListener('click', save);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); save(); }
    });
    input.focus();
  }

  async function resetSession() {
    if (!state.current) return;
    var ok = await MR.confirm('清空当前会话的上下文消息？长期记忆会保留。', { title: '清空上下文' });
    if (!ok) return;
    try {
      var data = await MR.post('/api/dialogue/sessions/' + encodeURIComponent(state.current.id) + '/reset', {});
      MR.toast('已清空 ' + (Number(data.removed) || 0) + ' 条上下文消息', 'success');
      await selectSession(state.current.id);
      await loadSessions();
    } catch (err) {
      MR.toast(err.message, 'error');
    }
  }

  /** 用内置指令切换会话绑定；切换后刷新会话元信息（不重载消息，保留已渲染气泡）。 */
  async function switchBinding(kind, value) {
    if (!state.current) return;
    if (!value) {
      MR.toast('「跟随默认」只在新建会话时生效；当前会话请选择具体的' +
        (kind === 'persona' ? '人设' : '模型'), 'info');
      renderSelects();
      return;
    }
    var cmd = state.prefix + kind + ' ' + value;
    var ok = await sendMessage(cmd, { showBubbles: false });
    if (!ok) renderSelects();
  }

  // ----------------------------------------------------------------
  // 渲染
  // ----------------------------------------------------------------
  function renderSessionList() {
    if (!state.sessions.length) {
      els.sessionList.innerHTML = '<div class="empty small">还没有会话</div>';
      return;
    }
    els.sessionList.innerHTML = state.sessions.map(function (s) {
      var active = state.current && state.current.id === s.id ? ' active' : '';
      var meta = [];
      if (s.persona_id) meta.push('人设 ' + s.persona_id);
      if (s.model_id) meta.push('模型 ' + s.model_id);
      return '<div class="session-item' + active + '" data-id="' + MR.escape(s.id) + '">' +
        '<div class="session-main">' +
          '<div class="session-title">' + MR.escape(s.title || s.id) + '</div>' +
          '<div class="session-meta">' + MR.escape(meta.join(' · ') || '默认人设 · 默认模型') +
            ' · ' + (Number(s.message_count) || 0) + ' 条</div>' +
        '</div>' +
        '<div class="session-acts">' +
          '<button class="btn btn-sm btn-ghost session-act" data-rename="' + MR.escape(s.id) + '" title="重命名会话">✎</button>' +
          '<button class="btn btn-sm btn-ghost session-act" data-del="' + MR.escape(s.id) + '" title="删除会话">✕</button>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  function renderHeader() {
    var s = state.current;
    els.title.textContent = s ? (s.title || s.id) : '未选择会话';
    if (!s) {
      els.badges.innerHTML = '';
      renderSelects();
      return;
    }
    els.badges.innerHTML =
      '<span class="badge badge-accent">人设：' + MR.escape(s.persona_id ? personaLabel(s.persona_id) : '默认') + '</span>' +
      '<span class="badge">模型：' + MR.escape(s.model_id || '默认') + '</span>';
    renderSelects();
  }

  function renderSelects() {
    var s = state.current;
    els.personaSelect.innerHTML = '<option value="">跟随默认</option>' +
      state.personas.map(function (p) {
        return '<option value="' + MR.escape(p.id) + '">' + MR.escape(p.name + ' (' + p.id + ')') + '</option>';
      }).join('');
    els.modelSelect.innerHTML = state.models.map(function (m) {
      var label = m.id + ' — ' + (m.name || '') + (m.downloaded ? '' : '（未下载）');
      return '<option value="' + MR.escape(m.id) + '">' + MR.escape(label) + '</option>';
    }).join('');

    if (s) {
      // 会话绑定的值可能不在清单里（例如人设已删除），补一个占位选项避免下拉框错乱
      if (s.persona_id && !state.personas.some(function (p) { return p.id === s.persona_id; })) {
        els.personaSelect.insertAdjacentHTML('beforeend',
          '<option value="' + MR.escape(s.persona_id) + '">' + MR.escape(s.persona_id) + '</option>');
      }
      els.personaSelect.value = s.persona_id || '';
      if (s.model_id && !state.models.some(function (m) { return m.id === s.model_id; })) {
        els.modelSelect.insertAdjacentHTML('beforeend',
          '<option value="' + MR.escape(s.model_id) + '">' + MR.escape(s.model_id) + '</option>');
      }
      els.modelSelect.value = s.model_id || '';
    }
    updateComposer();
  }

  function renderMessages(list) {
    if (!state.current) { renderEmpty(); return; }
    if (!list.length) {
      els.messages.innerHTML = '<div class="empty">还没有消息，说点什么开始吧。<br>' +
        '点下方指令按钮可查看帮助。</div>';
      state.stuck = true;
      return;
    }
    els.messages.innerHTML = list.map(bubbleHTML).join('');
    scrollToBottom(true);
  }

  function renderEmpty() {
    els.messages.innerHTML = '<div class="empty">还没有会话。<br><br>' +
      '<button class="btn btn-primary" id="empty-new">＋ 新建会话</button></div>';
    var btn = document.getElementById('empty-new');
    if (btn) btn.addEventListener('click', newSession);
    els.title.textContent = '未选择会话';
    els.badges.innerHTML = '';
    renderSelects();
  }

  /**
   * 指令条只显示命令本身（/persona），参数放进 title 与点击后的输入框：
   * 完整的 `/persona [人设id|list]` 在窄屏上会折成两三行，把消息区挤没。
   */
  function commandHead(usage) {
    return String(usage || '').split(/[\s\[]+/)[0];
  }

  function renderChips() {
    var html = state.commands.map(function (c) {
      var head = commandHead(c.usage);
      var tip = c.usage + (c.description ? '  ' + c.description : '');
      return '<button class="chip" data-usage="' + MR.escape(c.usage) + '" title="' +
        MR.escape(tip) + '">' + MR.escape(head) + '</button>';
    });
    html.push('<button class="chip chip-help" data-help="1" title="查看全部指令">帮助</button>');
    els.chips.innerHTML = html.join('');
  }

  // ----------------------------------------------------------------
  // 气泡
  // ----------------------------------------------------------------
  function currentPersona() {
    var id = (state.current && state.current.persona_id) || state.defaultPersona || '';
    if (!id) return null;
    return state.personas.filter(function (p) { return p.id === id; })[0] || null;
  }

  function personaLabel(id) {
    var card = state.personas.filter(function (p) { return p.id === id; })[0];
    return card ? (card.name + ' (' + id + ')') : id;
  }

  function assistantName() {
    var p = currentPersona();
    return p ? p.name : '助手';
  }

  function assistantAvatar() {
    var p = currentPersona();
    return p && p.avatar ? p.avatar : '🤖';
  }

  /** 头像：emoji 直接显示，路径 / data URI 用 img。 */
  function avatarHTML(avatar, isUser) {
    var cls = 'avatar' + (isUser ? ' avatar-user' : '');
    var value = avatar || '🙂';
    if (/^(https?:|data:|\/)/.test(String(value))) {
      return '<span class="' + cls + '"><img src="' + MR.escape(value) + '" alt=""></span>';
    }
    return '<span class="' + cls + '">' + MR.escape(value) + '</span>';
  }

  function bubbleHTML(m) {
    var isUser = m.role === 'user';
    var who = isUser ? (m.speaker_name || '我') : assistantName();
    return '<div class="bubble-row ' + (isUser ? 'user' : 'assistant') + '">' +
      avatarHTML(isUser ? '🙂' : assistantAvatar(), isUser) +
      '<div class="bubble ' + (isUser ? 'bubble-user' : 'bubble-assistant') + '">' +
        '<div class="bubble-head"><span class="who">' + MR.escape(who) + '</span>' +
        (m.created_at ? '<time>' + MR.escape(MR.fmtTime(m.created_at)) + '</time>' : '') +
        '</div>' +
        '<div class="bubble-body">' + MR.escape(m.content || '') + '</div>' +
      '</div>' +
    '</div>';
  }

  function hideEmpty() {
    var first = els.messages.firstElementChild;
    if (els.messages.children.length === 1 && first && first.classList.contains('empty')) {
      els.messages.innerHTML = '';
    }
  }

  function appendBubble(html) {
    hideEmpty();
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    var node = tmp.firstElementChild;
    els.messages.appendChild(node);
    return node;
  }

  function appendStreamBubble() {
    var el = appendBubble(bubbleHTML({ role: 'assistant', content: '', created_at: Date.now() / 1000 }));
    el.querySelector('.bubble-body').innerHTML = '<span class="typing">…</span>';
    el.classList.add('streaming');
    return el;
  }

  function finalizeBubble(el, text, evt) {
    el.classList.remove('streaming');
    var body = el.querySelector('.bubble');
    el.querySelector('.bubble-body').textContent = text || '（本次未回复）';
    if (evt && evt.is_command) {
      body.classList.add('bubble-command');
      var head = el.querySelector('.bubble-head');
      if (head) head.insertAdjacentHTML('beforeend', '<span class="tag">指令</span>');
    }
    if (evt && (evt.skipped || !evt.reply)) body.classList.add('bubble-muted');
    scrollToBottom();
  }

  // ----------------------------------------------------------------
  // 发送 / 流式接收
  // ----------------------------------------------------------------
  async function onSend() {
    var text = els.input.value.trim();
    if (!text) return;
    els.input.value = '';
    autoGrow();
    var ok = await sendMessage(text, { showBubbles: true });
    if (!ok && !els.input.value) { // 失败时把内容还原回输入框，避免用户丢字
      els.input.value = text;
      autoGrow();
    }
  }

  function stopStream() {
    if (state.controller) state.controller.abort();
  }

  /**
   * 发送消息并消费 SSE。
   * :param opts.showBubbles 是否在消息区渲染气泡（下拉切换指令时为 false）
   * :return 收到 done 事件返回 true
   */
  async function sendMessage(text, opts) {
    opts = opts || {};
    var showBubbles = opts.showBubbles !== false;
    if (!state.current) { MR.toast('请先新建或选择一个会话', 'error'); return false; }
    if (state.streaming) return false;

    var sessionId = state.current.id;
    var streamEl = null;
    if (showBubbles) {
      appendBubble(bubbleHTML({ role: 'user', content: text, speaker_name: '我', created_at: Date.now() / 1000 }));
      streamEl = appendStreamBubble();
      scrollToBottom();
    }

    state.streaming = true;
    state.controller = new AbortController();
    updateComposer();

    var acc = '';
    var done = false;
    try {
      var res = await fetch('/api/dialogue/sessions/' + encodeURIComponent(sessionId) + '/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text, speaker_name: '我', persona_id: '', model_id: '' }),
        signal: state.controller.signal
      });
      if (!res.ok) throw new Error(await errorDetail(res));

      await readSSE(res, {
        delta: function (evt) {
          acc += (evt.text || '');
          if (streamEl) streamEl.querySelector('.bubble-body').textContent = acc || '…';
          scrollToBottom();
        },
        done: function (evt) {
          var reply = evt.reply || acc;
          if (streamEl) finalizeBubble(streamEl, reply, evt);
          handleDone(evt, reply, showBubbles);
          done = true;
        },
        error: function (evt) {
          throw new Error(evt.detail || '生成失败');
        }
      });
    } catch (err) {
      if (err && err.name === 'AbortError') {
        if (streamEl) finalizeBubble(streamEl, acc || '（已停止）', { skipped: !acc });
        MR.toast('已停止生成', 'info');
      } else {
        var msg = (err && err.message) ? err.message : '请求失败';
        if (streamEl) {
          // 也要摘掉 streaming：这条路没走 finalizeBubble，否则失败的气泡
          // 会一直带着「生成中」的标记（一旦将来给该状态加样式就会误导用户）
          streamEl.classList.remove('streaming');
          streamEl.querySelector('.bubble-body').textContent = msg;
          streamEl.querySelector('.bubble').classList.add('bubble-error');
          scrollToBottom();
        } else {
          MR.toast(msg, 'error');
        }
      }
    } finally {
      state.streaming = false;
      state.controller = null;
      updateComposer();
    }
    return done;
  }

  function handleDone(evt, reply, showBubbles) {
    if (!showBubbles && reply) MR.toast(reply, 'success');
    var memories = evt.new_memories || [];
    memories.slice(0, 3).forEach(function (m) {
      MR.toast('记住了：' + (m.content || ''), 'info');
    });
    if (memories.length > 3) MR.toast('另外新增了 ' + (memories.length - 3) + ' 条记忆', 'info');
    // 刷新会话绑定（badge / 下拉）与会话列表的条数
    refreshSessionMeta();
    loadSessions();
  }

  async function refreshSessionMeta() {
    if (!state.current) return;
    try {
      var data = await MR.get('/api/dialogue/sessions/' + encodeURIComponent(state.current.id) + MR.qs({ message_limit: 1 }));
      state.current = data.session;
      renderHeader();
    } catch (err) {
      /* 元信息刷新失败不影响已渲染的对话，忽略 */
    }
  }

  async function errorDetail(res) {
    try {
      var data = await res.json();
      if (data && (data.detail || data.message)) return data.detail || data.message;
    } catch (e) { /* 非 JSON 响应 */ }
    return '请求失败（HTTP ' + res.status + '）';
  }

  /**
   * 读取 SSE 流。
   * 缓冲区按 \n\n 切块；每块里找 event: 与 data: 行，多行 data 用 \n 连接；
   * 忽略注释行（: 开头）与没有 event 的块。
   */
  async function readSSE(res, handlers) {
    var reader = res.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buffer = '';

    function dispatch(block) {
      var event = '';
      var dataLines = [];
      block.split('\n').forEach(function (line) {
        if (!line || line.charAt(0) === ':') return; // 注释行（如 ": connected"）
        if (line.indexOf('event:') === 0) {
          event = line.slice(6).trim();
        } else if (line.indexOf('data:') === 0) {
          var value = line.slice(5);
          dataLines.push(value.charAt(0) === ' ' ? value.slice(1) : value);
        }
      });
      if (!event) return; // 没有事件名，丢弃
      var payload = {};
      if (dataLines.length) {
        try { payload = JSON.parse(dataLines.join('\n')); } catch (e) { return; }
      }
      var fn = handlers[event];
      if (!fn) return;
      try {
        fn(payload);
      } catch (err) {
        // 处理器抛错（如 error 事件）时取消读取，把错误交给外层 catch
        try { reader.cancel(); } catch (e) { /* 忽略 */ }
        throw err;
      }
    }

    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      var idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        dispatch(buffer.slice(0, idx));
        buffer = buffer.slice(idx + 2);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) dispatch(buffer);
  }

  // ----------------------------------------------------------------
  // 输入区
  // ----------------------------------------------------------------
  function updateComposer() {
    var has = !!state.current;
    els.sendBtn.disabled = !has || state.streaming;
    els.sendBtn.textContent = state.streaming ? '生成中…' : '发送';
    els.stopBtn.hidden = !state.streaming;
    els.input.disabled = !has;
    els.personaSelect.disabled = !has || state.streaming;
    els.modelSelect.disabled = !has || state.streaming;
    els.resetBtn.disabled = !has;
  }

  function autoGrow() {
    els.input.style.height = 'auto';
    els.input.style.height = Math.min(els.input.scrollHeight, 160) + 'px';
  }

  function onInputKey(e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      onSend();
    }
  }

  function scrollToBottom(force) {
    if (!force && !state.stuck) return;
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function onMessagesScroll() {
    var el = els.messages;
    state.stuck = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  }

  // ----------------------------------------------------------------
  // 会话列表 / 指令点击（事件委托）
  // ----------------------------------------------------------------
  function onSessionListClick(e) {
    var renameBtn = e.target.closest('[data-rename]');
    if (renameBtn) {
      e.stopPropagation();
      renameSession(renameBtn.getAttribute('data-rename'));
      return;
    }
    var delBtn = e.target.closest('[data-del]');
    if (delBtn) {
      e.stopPropagation();
      deleteSession(delBtn.getAttribute('data-del'));
      return;
    }
    var item = e.target.closest('.session-item');
    if (item) selectSession(item.getAttribute('data-id'));
  }

  function onChipClick(e) {
    if (e.target.closest('[data-help]')) { showHelp(); return; }
    var chip = e.target.closest('[data-usage]');
    if (!chip) return;
    var usage = chip.getAttribute('data-usage') || '';
    var head = commandHead(usage);
    if (head === usage.trim()) {
      sendMessage(usage.trim(), { showBubbles: true }); // 无参数指令直接发送
    } else {
      els.input.value = head + ' ';
      els.input.focus();
      autoGrow();
    }
  }

  function showHelp() {
    var text = state.helpText ||
      state.commands.map(function (c) { return c.usage + '  ' + c.description; }).join('\n') ||
      '暂无帮助信息';
    var mask = MR.openModal({
      title: '可用指令',
      width: '560px',
      bodyHtml: '<pre class="mono help-pre"></pre>',
      footHtml: '<button class="btn btn-primary" data-close="1">知道了</button>'
    });
    mask.querySelector('.help-pre').textContent = text;
  }

  // ----------------------------------------------------------------
  // 侧栏抽屉
  // ----------------------------------------------------------------
  function openSidebar() {
    els.sidebar.classList.add('open');
    els.scrim.classList.add('show');
  }

  function closeSidebar() {
    els.sidebar.classList.remove('open');
    els.scrim.classList.remove('show');
  }
})();