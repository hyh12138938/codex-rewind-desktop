/* Codex Rewind Desktop: native assistant action-row integration.
 * Reads committed message identity; never assigns a turn by DOM ordering.
 * Host lifecycle and file operations are provided by a restricted CDP binding.
 */
(() => {
  'use strict';
  window.__codexRewindCleanup?.();
  const MARK = 'data-codex-rewind';
  const pending = new Map();
  const replayed = new WeakSet();
  const handlers = [];
  const diagnostics = {version: '0.2.0', buttons: 0, unresolved: 0, tracked: false, busy: false, status: 'starting'};
  window.__codexRewindDiagnostics = diagnostics;
  let sequence = 0, disposed = false, refreshTimer = 0, syncTimer = 0;
  let statusCache = {operations: {}}, statusAt = 0, submissionBusy = false;
  let dialog = null, lastSyncThread = '', lastSyncAt = 0, interval = 0;

  function call(method, args = {}) {
    if (disposed) return Promise.reject(new Error('撤回组件已停止。'));
    if (typeof window.__codexRewindNative !== 'function') return Promise.reject(new Error('撤回服务尚未连接。'));
    const id = String(++sequence);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('撤回请求超时，请检查恢复记录。')); }, 120000);
      pending.set(id, {resolve, reject, timer});
      try { window.__codexRewindNative(JSON.stringify({id, method, args})); }
      catch (error) {clearTimeout(timer); pending.delete(id); reject(error);}
    });
  }
  window.__codexRewindResolve = (id, result, error) => {
    const request = pending.get(String(id));
    if (!request) return;
    clearTimeout(request.timer); pending.delete(String(id));
    if (error) request.reject(new Error(typeof error === 'string' ? error : error.message || JSON.stringify(error)));
    else request.resolve(result);
  };

  function element(tag, text, attrs = {}) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }
  function listen(target, type, listener, options) {
    target.addEventListener(type, listener, options);
    handlers.push(() => target.removeEventListener(type, listener, options));
  }
  function fiberOf(node) {
    for (let current = node, depth = 0; current && depth < 6; current = current.parentElement, depth++) {
      const key = Object.getOwnPropertyNames(current).find(name => name.startsWith('__reactFiber$') || name.startsWith('__reactInternalInstance$'));
      if (key) return current[key];
    }
    return null;
  }
  function identity(host, row) {
    const conversation = host.getAttribute('data-response-annotation-conversation');
    const key = host.closest('[data-content-search-unit-key]')?.getAttribute('data-content-search-unit-key') || '';
    const domTurn = key.split(':')[0];
    const candidates = new Map();
    const nativeButtons = [...row.querySelectorAll(`button:not([${MARK}])`)];
    for (const button of nativeButtons) {
      let fiber = fiberOf(button);
      for (let depth = 0; fiber && depth < 36; depth++, fiber = fiber.return) {
        const props = fiber.memoizedProps;
        if (!props || typeof props !== 'object') continue;
        const thread = props.threadId || props.conversationId;
        const turn = props.turnId;
        if (typeof thread !== 'string' || typeof turn !== 'string' || typeof props.onFork !== 'function') continue;
        // This exact component owns the native copy/more/fork controls.
        if (props.hostId && props.hostId !== 'local') return null;
        if ((conversation && thread !== conversation) || (domTurn && turn !== domTurn)) return null;
        candidates.set(thread + '\n' + turn, {thread_id: thread, turn_id: turn});
        break;
      }
    }
    return candidates.size === 1 ? [...candidates.values()][0] : null;
  }
  function messageHosts() {
    return [...document.querySelectorAll('[data-response-annotation-target]')].filter(host =>
      host.checkVisibility({checkVisibilityCSS: true}) &&
      host.querySelector(':scope > [data-conversation-role="assistant"]') && !host.closest(`[${MARK}="dialog"]`));
  }
  function currentThread() {
    const ids = new Set();
    for (const host of messageHosts()) {
      const row = host.querySelector(':scope > .turn-action-controls');
      if (!row) continue;
      const value = identity(host, row);
      if (value) ids.add(value.thread_id);
    }
    return ids.size === 1 ? [...ids][0] : null;
  }
  function redoFor(thread) {
    return Object.values(statusCache.operations || {}).filter(op => op.branch === thread && op.status === 'rewound').at(-1);
  }
  function icon(redo = false) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [name, value] of Object.entries({width: '20', height: '20', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.6', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true'})) svg.setAttribute(name, value);
    if (redo) svg.style.transform = 'scaleX(-1)';
    const path = document.createElementNS(svg.namespaceURI, 'path');
    path.setAttribute('d', 'M9 5 4 10l5 5M4 10h10a6 6 0 0 1 0 12');
    svg.append(path);
    return svg;
  }
  function openDialog(title = '回退到这条回复') {
    dialog?.remove();
    const modal = element('dialog', null, {[MARK]: 'dialog', 'aria-label': title});
    modal.style.cssText = 'position:fixed;inset:0;margin:auto;width:min(560px,calc(100vw - 32px));max-height:80vh;overflow:auto;border:1px solid #7776;border-radius:14px;padding:24px;background:var(--color-token-main-surface-primary,Canvas);color:var(--color-token-text-primary,CanvasText);font:14px/1.6 system-ui;box-shadow:0 16px 60px #0003';
    const heading = element('h2', title); heading.style.cssText = 'font-size:18px;margin:0 0 8px';
    const description = element('p', '保留这条回复及之前的聊天，回退之后的轮次。');
    const content = element('pre', '正在读取回退范围…'); content.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.7 ui-monospace,monospace;max-height:36vh;overflow:auto';
    const message = element('p', '', {'role': 'status'}); message.style.whiteSpace = 'pre-wrap';
    const controls = element('div'); controls.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;margin-top:18px';
    const close = control('取消', () => modal.close()); controls.append(close);
    modal.append(heading, description, content, message, controls);
    modal.addEventListener('close', () => { modal.remove(); if (dialog === modal) dialog = null; });
    document.body.append(modal); modal.showModal(); dialog = modal;
    return {modal, heading, description, content, message, controls, close};
  }
  function control(text, action) {
    const button = element('button', text, {type: 'button'});
    button.style.cssText = 'padding:7px 12px;border:1px solid #8886;border-radius:7px;background:transparent;color:inherit;cursor:pointer;font:inherit';
    button.addEventListener('click', () => action());
    return button;
  }
  function report(error) {
    diagnostics.status = 'error';
    const view = openDialog('撤回操作提示');
    view.description.textContent = '当前操作尚未完成。'; view.content.textContent = String(error.message || error);
  }
  async function showPreview(context, operation = null, files = true, view = null) {
    view ||= openDialog(operation ? '撤销回退' : '回退到这条回复');
    view.heading.textContent = operation ? '撤销回退' : '回退到这条回复';
    view.description.textContent = operation ? '恢复回退前的文件，并返回原来的完整聊天。' : '保留这条回复及之前的聊天，回退之后的轮次。';
    for (const child of [...view.controls.children]) if (child !== view.close) child.remove();
    view.content.textContent = '正在读取回退范围…'; view.message.textContent = '';
    try {
      const preview = await call(operation ? 'preview' : 'native_preview', operation ? {operation} : {...context, files});
      if (disposed || !view.modal.isConnected) return;
      if (preview.unavailableFiles) {
        view.content.textContent = preview.reason || '这条回复尚无可恢复的文件快照。';
        view.message.textContent = '可仅回退聊天，当前文件保持原样。';
        if (!operation) view.controls.append(control('仅回退聊天', () => showPreview(context, null, false, view)));
        return;
      }
      if (typeof preview.token !== 'string' || !preview.token) throw new Error('未收到有效预览令牌，请重新预览。');
      const changes = preview.changes || [];
      const conflicts = preview.conflicts || [];
      view.content.textContent = (preview.files ? '聊天 + 文件\n\n' : '仅聊天\n\n') + (changes.length ? changes.map(c => `${({added: '恢复', deleted: '删除', modified: '还原'})[c.status] || c.status}  ${c.path}`).join('\n') : '文件保持当前状态。');
      if (conflicts.length) view.message.textContent = '发现额外修改，请先处理冲突：\n' + conflicts.join('\n');
      if (!operation && files) view.controls.append(control('仅回退聊天', () => showPreview(context, null, false, view)));
      const confirm = control(operation ? '确认撤销回退' : '确认回退', async () => {
        confirm.disabled = true; view.message.textContent = '正在恢复，请稍候…';
        try {
          const result = await call('apply', {token: preview.token});
          statusAt = 0;
          view.content.textContent = operation ? '已恢复回退前的聊天和文件状态。' : '已创建回退分支，原聊天完整保留。';
          view.message.textContent = '正在打开对应聊天…';
          for (const child of [...view.controls.children]) if (child !== view.close) child.remove();
          if (!operation && result.operation?.id) view.controls.append(control('撤销这次回退', () => showPreview(null, result.operation.id, true, view)));
          view.controls.append(control('打开对应聊天', () => navigate(result.activeThread)));
          navigate(result.activeThread);
          diagnostics.status = operation ? 'redone' : 'rewound';
          view.message.textContent = '操作完成。若聊天尚未切换，可点击“打开对应聊天”。';
          scheduleRefresh();
        } catch (error) {view.message.textContent = String(error.message || error); confirm.disabled = false;}
      });
      confirm.disabled = conflicts.length > 0;
      view.controls.append(confirm);
    } catch (error) {
      view.content.textContent = String(error.message || error);
      if (!operation && files) view.controls.append(control('仅回退聊天', () => showPreview(context, null, false, view)));
    }
  }
  function navigate(thread) {
    if (typeof thread !== 'string' || !/^[0-9a-f-]{36}$/i.test(thread)) throw new Error('目标聊天ID无效。');
    window.dispatchEvent(new MessageEvent('message', {data: {type: 'navigate-to-route', path: '/local/' + thread}}));
  }
  async function updateStatus() {
    if (Date.now() - statusAt < 4000) return;
    statusAt = Date.now();
    try {statusCache = await call('status');} catch {statusAt = 0;}
  }
  function refresh() {
    if (disposed) return;
    let count = 0, unresolved = 0;
    for (const host of messageHosts()) {
      const row = host.querySelector(':scope > .turn-action-controls');
      const parent = row?.firstElementChild;
      if (!parent) continue;
      const info = identity(host, row);
      let button = parent.querySelector(`:scope > button[${MARK}="action"]`);
      if (!info) {button?.remove(); unresolved++; continue;}
      const redo = redoFor(info.thread_id);
      if (!button) {
        const native = parent.querySelector(`button:not([${MARK}])`);
        if (!native) continue;
        button = element('button', null, {type: 'button', [MARK]: 'action'});
        button.className = native.className;
        button.style.flexShrink = '0';
        button.addEventListener('click', event => {
          event.preventDefault(); event.stopPropagation();
          const now = identity(host, row);
          if (!now) return report(new Error('当前回复身份发生变化，请重新打开聊天后重试。'));
          const currentRedo = redoFor(now.thread_id);
          showPreview(now, currentRedo?.id || null);
        });
        // Place immediately before the existing native more-menu button.
        const more = [...parent.querySelectorAll(`button:not([${MARK}])`)].find(b =>
          /更多|more actions|more options/i.test(b.getAttribute('aria-label') || '') || b.getAttribute('aria-haspopup') === 'menu');
        parent.insertBefore(button, more || null);
      }
      const label = redo ? '撤销回退' : '回退到这条回复';
      if (button.getAttribute('aria-label') !== label) {
        button.setAttribute('aria-label', label); button.title = label;
        button.replaceChildren(icon(!!redo));
      }
      count++;
    }
    diagnostics.buttons = count; diagnostics.unresolved = unresolved;
    if (diagnostics.status === 'starting') diagnostics.status = 'ready';
    updateStatus().then(() => {if (!disposed) refreshButtonsLabels();});
    scheduleSync();
  }
  function refreshButtonsLabels() {
    for (const button of document.querySelectorAll(`button[${MARK}="action"]`)) {
      const host = button.closest('[data-response-annotation-target]');
      const info = host && identity(host, button.closest('.turn-action-controls'));
      if (!info) continue;
      const redo = !!redoFor(info.thread_id), label = redo ? '撤销回退' : '回退到这条回复';
      if (button.getAttribute('aria-label') !== label) {button.setAttribute('aria-label', label); button.title = label; button.replaceChildren(icon(redo));}
    }
  }
  function scheduleRefresh() {clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 150);}
  async function sync(thread, strict = false) {
    if (!thread) return;
    if (!strict && (diagnostics.busy || (thread === lastSyncThread && Date.now() - lastSyncAt < 2000))) return;
    diagnostics.busy = true;
    try {
      const result = await call('auto_sync', {thread_id: thread});
      diagnostics.tracked = result.tracked !== false;
      lastSyncThread = thread; lastSyncAt = Date.now();
      // Steering belongs to the native active turn. Let the original submission
      // proceed without pretending that an intermediate snapshot was captured.
      if (strict && result.tracked === false && result.reason === 'active') diagnostics.status = 'active';
      return result;
    } finally {diagnostics.busy = false;}
  }
  function scheduleSync() {
    clearTimeout(syncTimer);
    syncTimer = setTimeout(() => {const thread = currentThread(); if (thread) sync(thread).catch(() => {});}, 650);
  }
  function isComposer(target) {
    const editable = target instanceof Element && target.closest('[contenteditable="true"],textarea');
    if (!editable || editable.closest(`[${MARK}="dialog"]`)) return null;
    const label = editable.getAttribute('aria-label') || editable.getAttribute('data-placeholder') || editable.getAttribute('placeholder') || '';
    return /Codex|一起工作|消息|message|prompt/i.test(label) ? editable : null;
  }
  async function intercept(event, editable, target) {
    if (replayed.has(event) || event.defaultPrevented || submissionBusy || disposed) return;
    const thread = currentThread();
    if (!thread) return;
    event.preventDefault(); event.stopImmediatePropagation();
    submissionBusy = true;
    const readDraft = () => editable ? ('value' in editable ? editable.value : editable.textContent || '') : '';
    const draft = readDraft();
    try {
      await sync(thread, true);
      if (!target.isConnected || currentThread() !== thread || readDraft() !== draft) {
        throw new Error('保存快照期间输入内容或聊天已变化，请再次发送。');
      }
      let replay;
      if (event.type === 'keydown') replay = new KeyboardEvent('keydown', {key: event.key, code: event.code, bubbles: true, cancelable: true, composed: true});
      else replay = new MouseEvent('click', {bubbles: true, cancelable: true, composed: true, button: 0});
      replayed.add(replay); target.dispatchEvent(replay);
    } catch (error) {report(error);}
    finally {submissionBusy = false;}
  }
  listen(document, 'keydown', event => {
    if (replayed.has(event) || event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return;
    const editable = isComposer(event.target);
    if (editable) intercept(event, editable, editable);
  }, true);
  listen(document, 'click', event => {
    if (replayed.has(event) || !(event.target instanceof Element)) return;
    const button = event.target.closest('button');
    if (!button || button.disabled || button.closest(`[${MARK}="dialog"]`)) return;
    const label = (button.getAttribute('aria-label') || '').trim();
    if (!/^(发送|发送消息|提交|Send|Send message|Submit)$/i.test(label)) return;
    const editables = [...document.querySelectorAll('[contenteditable="true"],textarea')].filter(isComposer);
    if (editables.length === 1) intercept(event, editables[0], button);
  }, true);
  listen(window, 'message', () => scheduleRefresh());
  const observer = new MutationObserver(records => {
    if (records.every(record => record.target instanceof Element && record.target.closest(`[${MARK}]`))) return;
    scheduleRefresh();
  });
  function initialize() {
    if (disposed || !document.body) return;
    observer.observe(document.body, {childList: true, subtree: true});
    interval = setInterval(scheduleRefresh, 3000);
    refresh();
  }
  window.__codexRewindCleanup = () => {
    disposed = true; observer.disconnect(); clearInterval(interval); clearTimeout(refreshTimer); clearTimeout(syncTimer);
    handlers.forEach(remove => remove());
    for (const item of document.querySelectorAll(`[${MARK}]`)) item.remove();
    for (const request of pending.values()) {clearTimeout(request.timer); request.reject(new Error('撤回组件重新加载。'));}
    pending.clear();
    diagnostics.status = 'stopped';
  };
  if (document.body) initialize();
  else listen(document, 'DOMContentLoaded', initialize, {once: true});
})();
