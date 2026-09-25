/* memo-role 前端共享工具：请求封装、提示、确认框、格式化。
   纯原生实现，不依赖任何构建工具或外部资源；统一挂在 window.MR 命名空间下。 */
(function (global) {
  'use strict';

  var MR = {};

  //: 已打开的弹窗（后开的在后）；Esc 只关最上层，避免将来弹窗套弹窗时一次全关
  var openModals = [];

  // ----------------------------------------------------------------
  // 请求：统一 JSON 收发与错误处理
  // ----------------------------------------------------------------
  /** 读取响应体：优先按 JSON 解析，失败则退回纯文本。 */
  function readBody(res) {
    var ct = res.headers.get('content-type') || '';
    if (ct.indexOf('application/json') !== -1) {
      return res.json().catch(function () { return null; });
    }
    return res.text().then(function (text) {
      if (!text) return null;
      try { return JSON.parse(text); } catch (e) { return { detail: text }; }
    });
  }

  /**
   * 发送请求并返回解析后的数据。
   * 非 2xx 时抛出 Error，message 已翻成人能看懂的一句（见 describeError）。
   */
  async function request(method, url, body) {
    var opts = { method: method, headers: {} };
    if (body instanceof FormData) {
      opts.body = body; // 交给浏览器自动带 multipart 边界
    } else if (body !== undefined && body !== null) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }

    var res;
    try {
      res = await fetch(url, opts);
    } catch (err) {
      throw new Error('网络请求失败，请确认服务是否在运行');
    }

    var data = await readBody(res);
    if (!res.ok) {
      throw new Error(describeError(res, data));
    }
    return data;
  }

  /**
   * 把失败响应翻成一句人能看懂的话。
   *
   * `detail` 为「Not Found」时特别注意：这是 FastAPI 在**路由本身不存在**时的默认
   * 回复，和我们自己抛的 404（会写成「模型不存在」这类中文说明）不是一回事。
   * 页面是静态文件、每次都现取，接口却是服务启动时就注册好的 —— 所以「新前端配旧后端」
   * 时会出现页面能开、点什么都报 Not Found。这种情况不解释清楚，用户只会以为是坏了。
   */
  function describeError(res, data) {
    var detail = data && (data.detail || data.message);
    if (res.status === 404 && (!detail || detail === 'Not Found')) {
      return '接口不存在（HTTP 404）：正在运行的服务比页面代码旧。'
        + '请重启服务让新代码生效（安卓 App：「首页 → 停止服务 → 启动服务」；'
        + '在手机上升级过的话，先点一次「设置 → 更新代码」）。';
    }
    return detail || ('请求失败（HTTP ' + res.status + '）');
  }

  MR.get = function (url) { return request('GET', url); };
  MR.post = function (url, body) { return request('POST', url, body === undefined ? {} : body); };
  MR.put = function (url, body) { return request('PUT', url, body === undefined ? {} : body); };
  MR.patch = function (url, body) { return request('PATCH', url, body === undefined ? {} : body); };
  MR.del = function (url) { return request('DELETE', url); };
  MR.upload = function (url, formData) { return request('POST', url, formData); };

  /** 拼接查询串：跳过 null / undefined / 空串。 */
  MR.qs = function (params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value === null || value === undefined || value === '') return;
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
    });
    return parts.length ? '?' + parts.join('&') : '';
  };

  // ----------------------------------------------------------------
  // 提示与确认
  // ----------------------------------------------------------------
  /** 轻量瞬时提示；type ∈ info | success | error。 */
  MR.toast = function (message, type) {
    var wrap = document.querySelector('.toast-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'toast-wrap';
      document.body.appendChild(wrap);
    }
    var el = document.createElement('div');
    el.className = 'toast toast-' + (type || 'info');
    el.textContent = String(message === undefined || message === null ? '' : message);
    wrap.appendChild(el);
    setTimeout(function () { el.remove(); }, 3600);
  };

  /** 通用弹窗：返回 mask 元素（带 .close() 方法）。bodyHtml 由调用方保证安全。 */
  MR.openModal = function (opts) {
    opts = opts || {};
    var mask = document.createElement('div');
    mask.className = 'modal-mask';
    var box = document.createElement('div');
    box.className = 'modal';
    if (opts.width) box.style.maxWidth = opts.width;
    box.innerHTML =
      '<div class="modal-head"><span class="modal-title">' + MR.escape(opts.title || '') + '</span>' +
      '<button class="btn btn-sm btn-ghost" data-close="1" title="关闭">✕</button></div>' +
      '<div class="modal-body">' + (opts.bodyHtml || '') + '</div>' +
      (opts.footHtml ? '<div class="modal-foot">' + opts.footHtml + '</div>' : '');
    mask.appendChild(box);

    var closed = false;
    function onKeydown(e) {
      if (e.key !== 'Escape') return;
      if (openModals[openModals.length - 1] !== mask) return; // 只关最上层
      e.preventDefault();
      mask.close();
    }
    mask.close = function () {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKeydown);
      var idx = openModals.indexOf(mask);
      if (idx !== -1) openModals.splice(idx, 1);
      mask.remove();
      if (typeof opts.onClose === 'function') opts.onClose();
    };
    mask.addEventListener('click', function (e) {
      if (e.target === mask) { mask.close(); return; }
      var attr = e.target.getAttribute && e.target.getAttribute('data-close');
      if (attr) mask.close();
    });
    document.body.appendChild(mask);
    document.addEventListener('keydown', onKeydown);
    openModals.push(mask);
    // 打开后聚焦首个输入控件，键盘用户不必先 Tab 一圈
    var first = box.querySelector('input, textarea, select');
    if (first) first.focus();
    return mask;
  };

  /** 基于 promise 的确认框；点 ✕ / Esc / 点遮罩关闭都按「取消」处理。 */
  MR.confirm = function (message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var settled = false;
      function settle(value) {
        if (settled) return;
        settled = true;
        resolve(value);
      }
      var mask = MR.openModal({
        title: opts.title || '请确认',
        width: '420px',
        bodyHtml: '<div class="confirm-text"></div>',
        footHtml:
          '<button class="btn btn-ghost" data-act="cancel">取消</button>' +
          '<button class="btn ' + (opts.danger ? 'btn-danger' : 'btn-primary') + '" data-act="ok">确定</button>',
        onClose: function () { settle(false); }
      });
      mask.querySelector('.confirm-text').textContent = String(message || '');
      mask.addEventListener('click', function (e) {
        var act = e.target.getAttribute && e.target.getAttribute('data-act');
        if (act === 'ok') { settle(true); mask.close(); }
        else if (act === 'cancel') { settle(false); mask.close(); }
      });
    });
  };

  // ----------------------------------------------------------------
  // 文本与格式化
  // ----------------------------------------------------------------
  /** HTML 转义：所有插入 innerHTML 的服务端数据都必须先过这里。 */
  MR.escape = function (text) {
    if (text === undefined || text === null) return '';
    return String(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  };

  /** 字节数 → 人类可读。 */
  MR.fmtBytes = function (n) {
    n = Number(n) || 0;
    if (n < 1024) return n + ' B';
    var units = ['KB', 'MB', 'GB', 'TB'];
    var i = -1;
    do { n = n / 1024; i += 1; } while (n >= 1024 && i < units.length - 1);
    return n.toFixed(n >= 100 ? 0 : 1) + ' ' + units[i];
  };

  /** epoch 秒（可为浮点）→ 本地时间字符串。 */
  MR.fmtTime = function (seconds) {
    var sec = Number(seconds);
    if (!sec && sec !== 0) return '-';
    var d = new Date(sec * 1000);
    if (isNaN(d.getTime()) || sec <= 0) return '-';
    var p = function (x) { return String(x).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
      ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  };

  /** 秒数 → “1 天 2 小时 3 分”。 */
  MR.fmtDuration = function (seconds) {
    var s = Math.max(0, Math.floor(Number(seconds) || 0));
    var d = Math.floor(s / 86400); s -= d * 86400;
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60);
    var parts = [];
    if (d) parts.push(d + ' 天');
    if (h) parts.push(h + ' 小时');
    parts.push(m + ' 分');
    return parts.join(' ');
  };

  /** 页面顶部的跨页导航链接。 */
  MR.headerNav = function (active) {
    var items = [['/', '聊天'], ['/admin', '管理'], ['/files', '文件']];
    return items.map(function (item) {
      var cls = 'nav-link' + (item[0] === active ? ' active' : '');
      return '<a class="' + cls + '" href="' + item[0] + '">' + MR.escape(item[1]) + '</a>';
    }).join('');
  };

  global.MR = MR;
})(window);