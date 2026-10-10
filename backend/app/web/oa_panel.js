/**
 * StaffDeck OA 代填面板（书签注入版）。
 *
 * 运行位置：用户浏览器里的 OA 页面（由书签把本文件以 <script> 注入）。
 * 因此：
 * - 配置 `window.__SD_OA_CONFIG__` 由后端拼在文件头部一起下发，面板不做任何跨域请求；
 * - 所有 DOM 操作都在 OA 页面自己的 origin 里，等价于用户手点；
 * - 填表动作沿用原 Playwright 版（已下线）验证过的实录操作序列（同样的
 *   field_library 语义：input / select / autocomplete / radio / file）。
 *
 * 不依赖任何构建工具，ES2017+ 语法即可（Chrome/Edge/Safari 现代版本都支持）。
 */
(function () {
  'use strict';

  var CFG = window.__SD_OA_CONFIG__ || {};
  var PANEL_CFG = CFG.panel || {};
  var TODO = '__TODO__';

  // 用当前脚本的真实 src 反推 StaffDeck 源站：书签在 OA 页里加载本文件，这个 origin 一定可达。
  var scriptEl = document.currentScript;
  var BASE = '';
  try {
    BASE = scriptEl && scriptEl.src ? new URL(scriptEl.src).origin : (window.__SD_OA_BASE__ || '');
  } catch (e) {
    BASE = window.__SD_OA_BASE__ || '';
  }

  if (window.__SD_OA_PANEL__) {
    window.__SD_OA_PANEL__.toggle();
    return;
  }

  // ───────────────────────── 通用工具 ─────────────────────────

  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

  function el(tag, style, text) {
    var node = document.createElement(tag);
    if (style) node.style.cssText = style;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function isVisible(node) {
    if (!node) return false;
    var r = node.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  }

  function visibleIn(root, selector) {
    var list = root.querySelectorAll(selector);
    for (var i = 0; i < list.length; i++) {
      if (isVisible(list[i])) return list[i];
    }
    return null;
  }

  /** 元素所在 window（OA 表单在 iframe 里，事件/异常要用它自己的 realm 构造）。 */
  function winOf(node) {
    return (node && node.ownerDocument && node.ownerDocument.defaultView) || window;
  }

  function clickEl(node) {
    if (!node) return;
    var W = winOf(node);
    var opts = { bubbles: true, cancelable: true, view: W, button: 0 };
    try { node.dispatchEvent(new W.MouseEvent('mousedown', opts)); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('mouseup', opts)); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('click', opts)); } catch (e) {}
  }

  /** 用元素自身原型上的 value setter 赋值，React 受控输入才会读到新值。
   *  不写 `node instanceof HTMLInputElement`：iframe 里的元素与顶层 window 不同 realm，
   *  instanceof 会恒为 false，进而静默退化成 `node.value=`（受控组件会回滚）。 */
  function setNative(node, value) {
    var proto = Object.getPrototypeOf(node);
    var desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
    if (desc && desc.set) desc.set.call(node, value);
    else node.value = value;
  }

  function fireEvent(node, type) {
    var W = winOf(node);
    node.dispatchEvent(new W.Event(type, { bubbles: true }));
  }

  function fireInput(node) {
    fireEvent(node, 'input');
    fireEvent(node, 'change');
  }

  function fireKey(node, type, key) {
    var W = winOf(node);
    try {
      node.dispatchEvent(new W.KeyboardEvent(type, { bubbles: true, cancelable: true, key: key }));
    } catch (e) {}
  }

  function fireHover(node) {
    var W = winOf(node);
    try { node.dispatchEvent(new W.MouseEvent('mouseover', { bubbles: true })); } catch (e) {}
    try { node.dispatchEvent(new W.MouseEvent('mouseenter', { bubbles: false })); } catch (e) {}
  }

  /** OA 表单在 static4form 的 iframe 里：把所有同源 document（含嵌套）收集起来，
   *  等价于 Playwright 侧的 _frame_with（主 frame 优先）。跨域 iframe 直接跳过。 */
  function collectDocs() {
    var docs = [document];
    var seen = [];
    seen.push(document);
    for (var i = 0; i < docs.length; i++) {
      var frames;
      try { frames = docs[i].querySelectorAll('iframe'); } catch (e) { continue; }
      for (var j = 0; j < frames.length; j++) {
        var child = null;
        try { child = frames[j].contentDocument; } catch (e) { child = null; }
        if (child && seen.indexOf(child) < 0) {
          seen.push(child);
          docs.push(child);
        }
      }
    }
    return docs;
  }

  /** 文本归一化：去掉所有空白，让 "保 存" 能匹配 innerText 里的 "保存"。 */
  function normText(value) {
    return String(value || '').replace(/\s+/g, '');
  }

  /**
   * 兼容 Playwright 特有的伪类（`button.ant-btn:has-text("保存")`）：
   * 浏览器原生 querySelectorAll 不认识 `:has-text()`，会直接抛 SyntaxError。
   * 做法：把 `:has-text("x")` 剥离成 CSS 前缀 + 文本过滤，逗号列表逐段处理。
   */
  function queryInDoc(doc, selector) {
    var out = [];
    var parts = String(selector).split(',');
    for (var i = 0; i < parts.length; i++) {
      var part = parts[i].trim();
      if (!part) continue;
      var needle = null;
      var cssPart = part;
      var match = part.match(/:has-text\(\s*(['"])([\s\S]*?)\1\s*\)/);
      if (!match) match = part.match(/:text\(\s*(['"])([\s\S]*?)\1\s*\)/);
      if (match) {
        needle = normText(match[2]);
        cssPart = part.replace(match[0], '').trim() || '*';
      }
      var nodes;
      try {
        nodes = doc.querySelectorAll(cssPart);
      } catch (e) {
        continue; // 还有其他不认识的伪类，跳过这一段
      }
      for (var j = 0; j < nodes.length; j++) {
        if (needle === null) { out.push(nodes[j]); continue; }
        var text = normText(nodes[j].innerText || nodes[j].textContent || nodes[j].value);
        if (text.indexOf(needle) >= 0) out.push(nodes[j]);
      }
    }
    return out;
  }

  /** 跨 frame 定位（主 frame 优先）：优先返回可见元素，没有可见的再返回首个匹配。 */
  function findEl(selector) {
    var docs = collectDocs();
    var fallback = null;
    for (var i = 0; i < docs.length; i++) {
      var list = queryInDoc(docs[i], selector);
      for (var j = 0; j < list.length; j++) {
        if (isVisible(list[j])) return list[j];
        if (!fallback) fallback = list[j];
      }
    }
    return fallback;
  }

  // ───────────────────────── 字段填写动作 ─────────────────────────

  function fillText(node, value, mode) {
    try { node.focus(); } catch (e) {}
    setNative(node, '');
    fireInput(node);
    if (mode === 'autocomplete') {
      // 逐字写入，触发受控组件的 input 同步，再等联想下拉
      var buf = '';
      for (var i = 0; i < value.length; i++) {
        buf += value[i];
        setNative(node, buf);
        fireEvent(node, 'input');
        fireKey(node, 'keyup', value[i]);
      }
    } else {
      setNative(node, value);
    }
    fireInput(node);
    try { node.blur(); } catch (e) {}
  }

  function visibleOptions(doc, keyword) {
    var found = [];
    var list;
    try {
      list = doc.querySelectorAll(
        '.ant-select-dropdown:not(.ant-select-dropdown-hidden) li.ant-select-dropdown-menu-item'
      );
    } catch (e) {
      return found;
    }
    for (var i = 0; i < list.length; i++) {
      var li = list[i];
      if (!isVisible(li)) continue;
      var text = (li.innerText || li.textContent || '').trim();
      if (!keyword || text.indexOf(keyword) >= 0) found.push(li);
    }
    return found;
  }

  /**
   * 从候选项里挑最匹配的：
   *  ① 文本完全一致（归一化空白后）；
   *  ② 以关键词开头（如「电子普通发票」优先于「增值税电子普通发票」）；
   *  ③ 退而求其次：包含关键词的最短项。
   * 之前的「取第一个包含项」会在「电子普通发票」这类关键词上误选「增值税电子普通发票」。
   */
  function pickOption(options, keyword) {
    var key = String(keyword || '').replace(/\s+/g, '');
    if (!key) return options[0];
    var prefix = null;
    var shortest = null;
    var shortestLen = Infinity;
    for (var i = 0; i < options.length; i++) {
      var text = (options[i].innerText || options[i].textContent || '').replace(/\s+/g, '');
      if (text === key) return options[i];
      if (!prefix && text.indexOf(key) === 0) prefix = options[i];
      if (text.length < shortestLen) { shortest = options[i]; shortestLen = text.length; }
    }
    return prefix || shortest;
  }

  /** 提交判据：容器内 hidden input 有值（渲染区文本会镜像搜索词，不能作依据）。 */
  function selectCommitted(container, value) {
    var hidden = container.querySelector("input[type='hidden']");
    if (hidden && (hidden.value || '').trim()) return true;
    if (!visibleIn(container, '.ant-select-search__field')) {
      var rendered = container.querySelector('.ant-select-selection__rendered');
      if (rendered && value && (rendered.innerText || '').indexOf(value) >= 0) return true;
    }
    return false;
  }

  function clearSelection(container, search) {
    // 悬停才出现清除图标；先派发 hover 让它可点（«×» icon 清掉 OA 预填的默认值）
    fireHover(container);
    var clearIcon = visibleIn(container, '.ant-select-selection__clear');
    if (clearIcon) clickEl(clearIcon);
    var hidden = container.querySelector("input[type='hidden']");
    if (hidden && (hidden.value || '').trim()) {
      setNative(hidden, '');
      fireInput(hidden);
    }
    if (search) {
      setNative(search, '');
      fireEvent(search, 'input');
    }
  }

  /** 联想下拉（ant-select / wea-select / wea-browser）：沿用原 Playwright 版验证过的实录操作序列。 */
  async function fillSelect(container, spec, value, log) {
    var doc = container.ownerDocument || document;
    var keyword = spec.search_value || value;
    var attempts = [];

    for (var attempt = 0; attempt < 3; attempt++) {
      // ① 打开下拉，等搜索框或选项出现
      var search = visibleIn(container, '.ant-select-search__field');
      if (!search) {
        clickEl(visibleIn(container, '.ant-select-selection') || container);
        for (var w = 0; w < 8; w++) {
          search = visibleIn(container, '.ant-select-search__field');
          if (search) break;
          if (visibleOptions(doc, '').length) break;
          await sleep(250);
        }
      }

      // ② 清预填值（OA 会给「费用承担部门」之类预填默认值）
      if (spec.clear_first) {
        clearSelection(container, search);
        await sleep(150);
        search = visibleIn(container, '.ant-select-search__field');
      }

      // ③ 写入搜索词：读该组件实测，必须走「填值 + input 事件」，逐字键入不触发联想过滤
      if (search) {
        try { search.focus(); } catch (e) {}
        await sleep(150);
        setNative(search, keyword);
        fireEvent(search, 'input');
        fireKey(search, 'keyup', keyword.slice(-1));
        if ((search.value || '').trim() !== keyword) {
          setNative(search, keyword);
          fireEvent(search, 'input');
        }
      }

      // ④ 轮询等匹配选项（下拉是 portal，挂在 frame 的 body 上）
      var option = null;
      for (var o = 0; o < 12; o++) {
        var opts = visibleOptions(doc, keyword);
        if (opts.length) { option = pickOption(opts, keyword); break; }
        await sleep(300);
      }
      attempts.push('opts=' + (option ? 'hit' : 'miss'));
      if (!option) continue;

      // ⑤ 真点击 + 校验提交
      clickEl(option);
      await sleep(400);
      if (selectCommitted(container, value)) {
        try { if (search) fireKey(search, 'keydown', 'Escape'); } catch (e) {}
        try { document.body.click(); } catch (e) {}
        return;
      }
      attempts.push('not_committed');
    }
    throw new Error('下拉选项中未找到「' + value + '」（' + attempts.join(' | ') + '）');
  }

  async function fillFile(container, files) {
    // 容器可能是上传区 div（真实 input 在内部），也可能直接就是 input；组件异步渲染，稍等重试
    function resolveInput() {
      if (container.matches && container.matches("input[type='file']")) return container;
      var inner = container.querySelector ? container.querySelector("input[type='file']") : null;
      if (!inner && container.parentElement) inner = container.parentElement.querySelector("input[type='file']");
      return inner;
    }
    var input = resolveInput();
    for (var i = 0; i < 10 && !input; i++) {
      await sleep(200);
      input = resolveInput();
    }
    if (!input) throw new Error('未找到文件输入框（selector 是否指向上传区？）');
    var W = winOf(input);
    var dt = new W.DataTransfer();
    for (var k = 0; k < files.length; k++) dt.items.add(files[k]);
    input.files = dt.files;
    fireEvent(input, 'change');
    await sleep(300);
  }

  /** 单个字段：返回 {field, status, detail}，不抛异常（异常由调用方兜住）。 */
  async function fillField(spec, values, files) {
    var label = spec.label || spec.key || '';
    var mode = spec.mode || 'input';

    var selector = spec.selector;
    if (mode === 'radio') {
      var wantYes = !!values[spec.from || ''];
      selector = wantYes ? spec.selector_yes : spec.selector_no;
      if (!selector || selector === TODO) return { field: label, status: 'todo', detail: '定位器待配置' };
      var radio = findEl(selector);
      if (!radio) throw new Error('未找到单选元素：' + selector);
      clickEl(radio);
      return { field: label, status: 'filled', detail: '勾选 ' + (wantYes ? '是' : '否') };
    }

    if (!selector || selector === TODO) return { field: label, status: 'todo', detail: '定位器待配置' };

    // 取字段值：from 指向表单输入，否则用配置常量
    var value;
    if (spec.from) {
      value = values[spec.from];
      if (value === undefined || value === null || value === '') {
        return { field: label, status: 'skipped', detail: '表单未提供值' };
      }
    } else {
      value = spec.value;
    }

    if (mode === 'select' && typeof value === 'boolean') {
      value = value ? (spec.value_yes || '是') : (spec.value_no || '否');
    }

    var node = findEl(selector);
    if (!node) throw new Error('未找到元素：' + selector + '（当前页可能不是该场景的表单？）');

    if (mode === 'file') {
      // 面板里选的本地文件直接搬进 OA 的 input（File 对象同文档传递，不经服务器）
      var picked = (files || []).slice();
      if (!picked.length) return { field: label, status: 'skipped', detail: '未选择附件' };
      await fillFile(node, picked);
      return { field: label, status: 'filled', detail: '附件 ' + picked.length + ' 个：' + picked.map(function (f) { return f.name; }).join('、') };
    }

    if (mode === 'select') {
      await fillSelect(node, spec, String(value), null);
      return { field: label, status: 'filled', detail: '选择 ' + value };
    }

    fillText(node, String(value), mode);
    return { field: label, status: 'filled', detail: String(value) };
  }

  function resolveFields(scenario) {
    var raw = scenario.fields || {};
    if (!Array.isArray(raw)) {
      return Object.keys(raw).map(function (k) { return [k, raw[k]]; });
    }
    var lib = CFG.field_library || {};
    var overrides = scenario.field_overrides || {};
    return raw.map(function (name) {
      var spec = {};
      var base = lib[name];
      if (base) {
        Object.keys(base).forEach(function (k) { spec[k] = base[k]; });
        var ov = overrides[name];
        if (ov) Object.keys(ov).forEach(function (k) { spec[k] = ov[k]; });
      } else {
        spec = { label: name, selector: TODO, mode: 'input' };
      }
      spec.key = name;
      return [name, spec];
    });
  }

  // ───────────────────────── 登录检测 ─────────────────────────

  function isLoginPage() {
    var check = (CFG.browser && CFG.browser.login_check) || {};
    var url = location.href;
    var words = check.url_contains || [];
    for (var i = 0; i < words.length; i++) {
      if (words[i] && url.toLowerCase().indexOf(String(words[i]).toLowerCase()) >= 0) return true;
    }
    var sels = check.selectors || [];
    for (var j = 0; j < sels.length; j++) {
      if (!sels[j] || sels[j] === TODO) continue;
      var n = findEl(sels[j]);
      if (n && isVisible(n)) return true;
    }
    return false;
  }

  // ───────────────────────── 面板 UI ─────────────────────────

  var host = document.createElement('div');
  host.id = 'sd-oa-panel-host';
  host.style.cssText = 'position:fixed;top:0;right:0;width:0;height:0;z-index:2147483647;';
  (document.body || document.documentElement).appendChild(host);
  var root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;

  var CARD = 'position:fixed;top:14px;right:14px;width:340px;max-height:calc(100vh - 28px);' +
    'display:flex;flex-direction:column;background:#ffffff;color:#1f2333;' +
    'border:1px solid #e3e7f1;border-radius:14px;box-shadow:0 16px 40px rgba(20,28,60,.22);' +
    'font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;' +
    'overflow:hidden;';
  var HEAD = 'display:flex;align-items:center;justify-content:space-between;gap:8px;' +
    'padding:10px 12px;border-bottom:1px solid #eef0f5;background:#fafbfd;cursor:move;user-select:none;-webkit-user-select:none;';
  var BODY = 'display:flex;flex-direction:column;gap:10px;padding:12px;overflow:auto;';
  var ROW = 'display:flex;flex-direction:column;gap:4px;';
  var LABEL = 'font-size:11.5px;color:#5b6274;';
  var INPUT = 'height:30px;border:1px solid #e3e7f1;border-radius:8px;padding:0 8px;font-size:12px;' +
    'color:#1f2333;background:#fff;box-sizing:border-box;width:100%;outline:none;';
  var BTN = 'height:30px;border:1px solid #e3e7f1;border-radius:8px;background:#fff;color:#464c5e;' +
    'font-size:12px;cursor:pointer;padding:0 10px;';
  var BTN_PRIMARY = 'height:32px;border:0;border-radius:9px;background:#18181a;color:#fff;' +
    'font-size:12px;cursor:pointer;flex:1;';
  var LOG = 'max-height:170px;overflow:auto;background:#fafbfd;border:1px solid #eef0f5;border-radius:8px;' +
    'padding:8px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;' +
    'line-height:1.6;white-space:pre-wrap;color:#5b6274;';

  var card = el('div', CARD);
  var head = el('div', HEAD);
  var titleWrap = el('div', 'display:flex;flex-direction:column;gap:1px;min-width:0;');
  titleWrap.appendChild(el('div', 'font-size:13px;font-weight:600;color:#18181a;', PANEL_CFG.title || 'AI报销代填'));
  titleWrap.appendChild(el('div', 'font-size:10.5px;color:#a3aaba;', '在当前 OA 页面直接填报 · 按住标题可拖动'));
  var closeBtn = el('button', 'border:0;background:transparent;font-size:16px;line-height:1;cursor:pointer;color:#858b9c;padding:4px;', '×');
  closeBtn.type = 'button';
  head.appendChild(titleWrap);
  head.appendChild(closeBtn);
  card.appendChild(head);

  var body = el('div', BODY);
  card.appendChild(body);

  function field(labelText, control) {
    var row = el('div', ROW);
    row.appendChild(el('div', LABEL, labelText));
    row.appendChild(control);
    return row;
  }

  // 报销场景
  var scenarioSelect = el('select', INPUT);
  var scenarios = (CFG.scenarios || []).filter(function (s) { return s.enabled !== false; });
  scenarios.forEach(function (s) {
    var opt = document.createElement('option');
    opt.value = s.id;
    opt.textContent = s.name;
    scenarioSelect.appendChild(opt);
  });
  body.appendChild(field('报销场景', scenarioSelect));

  // 报销事由
  var reasonInput = el('input', INPUT);
  body.appendChild(field('报销事由', reasonInput));

  // 公司名称
  var companySelect = el('select', INPUT);
  (PANEL_CFG.company_names || []).forEach(function (name) {
    var opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    companySelect.appendChild(opt);
  });
  body.appendChild(field('公司名称', companySelect));

  // 发票类型
  var invoiceSelect = el('select', INPUT);
  (PANEL_CFG.invoice_types || []).forEach(function (t) {
    var opt = document.createElement('option');
    opt.value = t.value;
    opt.textContent = t.label || t.value;
    invoiceSelect.appendChild(opt);
  });
  body.appendChild(field('发票类型', invoiceSelect));

  // 是否全为电子票
  var radioWrap = el('div', 'display:flex;gap:16px;align-items:center;height:26px;');
  function radio(value, text, checked) {
    var wrap = el('label', 'display:flex;align-items:center;gap:5px;cursor:pointer;color:#464c5e;');
    var input = document.createElement('input');
    input.type = 'radio';
    input.name = 'sd-oa-eticket';
    input.value = value;
    input.checked = checked;
    wrap.appendChild(input);
    wrap.appendChild(el('span', '', text));
    return wrap;
  }
  var defaultYes = PANEL_CFG.default_all_e_ticket === true;
  radioWrap.appendChild(radio('no', '否', !defaultYes));
  radioWrap.appendChild(radio('yes', '是', defaultYes));
  body.appendChild(field('是否全为电子票', radioWrap));
  radioWrap.addEventListener('change', function () { syncAttachVisibility(); });

  // 报销金额（下方 hint 用于显示自动计算/取整的原始金额）
  var amountInput = el('input', INPUT);
  amountInput.value = String(PANEL_CFG.default_amount || '100.00');
  var amountHint = el('div', 'font-size:11px;color:#a4650a;line-height:1.6;display:none;');
  amountInput.addEventListener('input', function () { amountHint.textContent = ''; amountHint.style.display = 'none'; });
  var amountBox = el('div', 'display:flex;flex-direction:column;gap:4px;');
  amountBox.appendChild(amountInput);
  amountBox.appendChild(amountHint);
  body.appendChild(field('报销金额（默认值，可改）', amountBox));

  // 附件（全为电子票才需要）：本地多选 PDF → 直接搬进 OA，同时可自动算金额
  var attachmentFiles = [];
  var attachBox = el('div', 'display:flex;flex-direction:column;gap:8px;border:1px dashed #dfe3ec;border-radius:10px;padding:10px;background:#fafbfd;');
  var attachTop = el('div', 'display:flex;align-items:center;gap:8px;');
  var pickBtn = el('button', BTN, '选择附件');
  pickBtn.type = 'button';
  var attachHint = el('span', 'font-size:10.5px;color:#a3aaba;flex:1;', '仅限 PDF，可多选');
  var clearFilesBtn = el('button', 'border:0;background:transparent;color:#a3aaba;font-size:11.5px;cursor:pointer;padding:0;', '清空');
  clearFilesBtn.type = 'button';
  attachTop.appendChild(pickBtn);
  attachTop.appendChild(attachHint);
  attachTop.appendChild(clearFilesBtn);
  attachBox.appendChild(attachTop);

  var fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.pdf,application/pdf';
  fileInput.multiple = true;
  fileInput.style.display = 'none';
  attachBox.appendChild(fileInput);

  var fileList = el('div', 'display:flex;flex-direction:column;gap:3px;');
  attachBox.appendChild(fileList);

  var lastCalcTotal = null;

  var calcWrap = el('label', 'display:flex;align-items:center;gap:6px;cursor:pointer;color:#464c5e;font-size:11.5px;');
  var calcCheck = document.createElement('input');
  calcCheck.type = 'checkbox';
  calcCheck.checked = PANEL_CFG.auto_calc_amount !== false;
  calcWrap.appendChild(calcCheck);
  calcWrap.appendChild(el('span', '', '自动计算发票总金额'));
  attachBox.appendChild(calcWrap);

  var floorWrap = el('label', 'display:flex;align-items:center;gap:6px;cursor:pointer;color:#464c5e;font-size:11.5px;');
  var floorCheck = document.createElement('input');
  floorCheck.type = 'checkbox';
  floorCheck.checked = PANEL_CFG.floor_amount_to_hundred === true;
  floorWrap.appendChild(floorCheck);
  floorWrap.appendChild(el('span', '', '金额向下取整到百位（如 1033.22 → 1000）'));
  attachBox.appendChild(floorWrap);

  var attachRow = field('电子发票附件（挂到 OA「相关票据」区）', attachBox);
  body.appendChild(attachRow);

  function syncAttachVisibility() {
    attachRow.style.display = allETicket() ? 'flex' : 'none';
  }

  function fmtSize(bytes) {
    if (bytes > 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + 'MB';
    return Math.max(1, Math.round(bytes / 1024)) + 'KB';
  }

  function renderFiles() {
    fileList.textContent = '';
    if (!attachmentFiles.length) {
      fileList.appendChild(el('div', 'font-size:11px;color:#a3aaba;', '尚未选择附件'));
      return;
    }
    attachmentFiles.forEach(function (file, index) {
      var row = el('div', 'display:flex;align-items:center;gap:6px;font-size:11.5px;color:#464c5e;');
      row.appendChild(el('span', 'color:#0f6b4f;', '✓'));
      var nameEl = el('span', 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;', file.name);
      nameEl.title = file.name;
      row.appendChild(nameEl);
      row.appendChild(el('span', 'color:#a3aaba;font-size:10.5px;', fmtSize(file.size)));
      var del = el('button', 'border:0;background:transparent;color:#a3aaba;cursor:pointer;padding:0 2px;', '×');
      del.type = 'button';
      del.addEventListener('click', function () {
        attachmentFiles.splice(index, 1);
        renderFiles();
        if (!attachmentFiles.length) { lastCalcTotal = null; amountHint.style.display = 'none'; }
        else if (calcCheck.checked) calcAmounts();
      });
      row.appendChild(del);
      fileList.appendChild(row);
    });
  }
  renderFiles();

  /** 把最近一次的发票合计写进金额框；开启「向下取整到百位」时按百位截断，并在下方
   *  hint 里括注原始合计（用户手动改金额时 hint 会被清掉）。 */
  function applyCalculatedAmount() {
    if (lastCalcTotal === null || isNaN(lastCalcTotal)) return false;
    var value = lastCalcTotal;
    var floored = false;
    if (floorCheck.checked) {
      var next = Math.floor(value / 100) * 100;
      floored = next !== value;
      value = next;
    }
    amountInput.value = value.toFixed(2);
    if (floored) {
      amountHint.textContent = '已向下取整到百位：' + value.toFixed(2) + '（原 ' + lastCalcTotal.toFixed(2) + '）';
      amountHint.style.display = 'block';
    } else {
      amountHint.textContent = '';
      amountHint.style.display = 'none';
    }
    return floored;
  }

  async function calcAmounts() {
    if (!attachmentFiles.length) return;
    if (!BASE) {
      logLine('无法定位 StaffDeck 服务地址，跳过自动计算（请手动填金额）。', 'warn');
      return;
    }
    calcCheck.disabled = true;
    logLine('正在解析 ' + attachmentFiles.length + ' 张发票的价税合计…', 'dim');
    try {
      var fd = new FormData();
      attachmentFiles.forEach(function (file) { fd.append('files', file, file.name); });
      var res = await fetch(BASE + '/api/enterprise/oa-assistant/invoice-amounts', { method: 'POST', body: fd });
      var data = await res.json();
      if (!res.ok) throw new Error((data && data.detail) || ('HTTP ' + res.status));
      lastCalcTotal = Number(data.total);
      var floored = applyCalculatedAmount();
      var suffix = floored ? '（已向下取整到百位，原 ' + lastCalcTotal.toFixed(2) + '）' : '';
      var failed = (data.items || []).filter(function (item) { return item.error; });
      if (failed.length) {
        logLine('已按可识别发票填写金额 ' + amountInput.value + suffix + '；' + failed.length + ' 张未能解析：' +
          failed.map(function (item) { return item.filename; }).join('、'), 'warn');
      } else {
        logLine('已按发票合计填写金额：' + amountInput.value + suffix, 'ok');
      }
    } catch (err) {
      logLine('自动计算失败：' + String((err && err.message) || err) + '（可手动填金额）', 'err');
    } finally {
      calcCheck.disabled = false;
    }
  }

  pickBtn.addEventListener('click', function () { fileInput.click(); });
  clearFilesBtn.addEventListener('click', function () { attachmentFiles = []; lastCalcTotal = null; amountHint.style.display = 'none'; renderFiles(); });
  floorCheck.addEventListener('change', function () {
    if (lastCalcTotal !== null) {
      applyCalculatedAmount();
      logLine(floorCheck.checked
        ? '金额已向下取整到百位：' + amountInput.value + '（原 ' + lastCalcTotal.toFixed(2) + '）'
        : '金额已按发票合计原值填写：' + amountInput.value, 'dim');
    } else if (calcCheck.checked && attachmentFiles.length) {
      calcAmounts();
    }
  });
  fileInput.addEventListener('change', function (e) {
    var picked = Array.prototype.slice.call(e.target.files || []);
    e.target.value = '';
    var invalid = picked.filter(function (file) {
      return !/\.pdf$/i.test(file.name) && file.type !== 'application/pdf';
    });
    if (invalid.length) logLine('已跳过非 PDF 文件：' + invalid.map(function (f) { return f.name; }).join('、'), 'warn');
    picked.forEach(function (file) {
      if (invalid.indexOf(file) >= 0) return;
      var dup = attachmentFiles.some(function (x) { return x.name === file.name && x.size === file.size; });
      if (!dup) attachmentFiles.push(file);
    });
    renderFiles();
    if (calcCheck.checked && attachmentFiles.length) calcAmounts();
  });

  syncAttachVisibility();

  // 填完是否点保存
  var saveWrap = el('label', 'display:flex;align-items:center;gap:7px;cursor:pointer;color:#464c5e;');
  var saveCheck = document.createElement('input');
  saveCheck.type = 'checkbox';
  saveCheck.checked = PANEL_CFG.auto_save_default === true;
  saveWrap.appendChild(saveCheck);
  saveWrap.appendChild(el('span', '', '填完后自动点「保存」'));
  body.appendChild(saveWrap);

  var logBox = el('div', LOG, '准备就绪。点「代填」开始。');
  body.appendChild(logBox);

  var footer = el('div', 'display:flex;gap:8px;padding:0 12px 12px;');
  var runBtn = el('button', BTN_PRIMARY, '代填');
  runBtn.type = 'button';
  footer.appendChild(runBtn);
  card.appendChild(footer);
  root.appendChild(card);

  // ── 拖动面板（按住标题）：变 fixed 的 left/top，位置存 sessionStorage ──
  var POS_KEY = 'sd_oa_panel_pos';
  function applyPos(left, top) {
    var w = card.offsetWidth || 340;
    var h = card.offsetHeight || 400;
    left = Math.min(Math.max(4, left), Math.max(4, window.innerWidth - w - 4));
    top = Math.min(Math.max(4, top), Math.max(4, window.innerHeight - h - 4));
    card.style.left = left + 'px';
    card.style.top = top + 'px';
    card.style.right = 'auto';
  }
  try {
    var savedPos = JSON.parse(sessionStorage.getItem(POS_KEY) || 'null');
    if (savedPos && typeof savedPos.left === 'number') applyPos(savedPos.left, savedPos.top);
  } catch (e) {}
  (function enableDrag() {
    var dragging = false;
    var startX = 0, startY = 0, startLeft = 0, startTop = 0;
    var hostDoc = host.ownerDocument || document;
    head.addEventListener('mousedown', function (e) {
      if (e.target === closeBtn) return;
      var rect = card.getBoundingClientRect();
      dragging = true;
      startX = e.clientX; startY = e.clientY;
      startLeft = rect.left; startTop = rect.top;
      card.style.right = 'auto';
      try { hostDoc.defaultView.getSelection().removeAllRanges(); } catch (err) {}
      e.preventDefault();
    });
    hostDoc.addEventListener('mousemove', function (e) {
      if (!dragging) return;
      applyPos(startLeft + (e.clientX - startX), startTop + (e.clientY - startY));
    });
    hostDoc.addEventListener('mouseup', function () {
      if (!dragging) return;
      dragging = false;
      try {
        sessionStorage.setItem(POS_KEY, JSON.stringify({
          left: parseInt(card.style.left, 10) || 0,
          top: parseInt(card.style.top, 10) || 0
        }));
      } catch (err) {}
    });
    window.addEventListener('resize', function () {
      if (card.style.left) applyPos(parseInt(card.style.left, 10) || 0, parseInt(card.style.top, 10) || 0);
    });
  })();

  function logLine(text, tone) {
    var color = { ok: '#0f6b4f', warn: '#a4650a', err: '#a4342c', dim: '#858b9c' }[tone] || '#5b6274';
    var line = el('div', 'color:' + color + ';', text);
    logBox.appendChild(line);
    logBox.scrollTop = logBox.scrollHeight;
  }

  function resetLog(text) { logBox.textContent = ''; if (text) logLine(text, 'dim'); }

  function currentScenario() {
    var id = scenarioSelect.value;
    for (var i = 0; i < scenarios.length; i++) if (scenarios[i].id === id) return scenarios[i];
    return scenarios[0];
  }

  function allETicket() {
    var picked = radioWrap.querySelector("input[name='sd-oa-eticket']:checked");
    return picked ? picked.value === 'yes' : false;
  }

  function lastMonth() {
    var month = new Date().getMonth() + 1;
    return month === 1 ? 12 : month - 1;
  }

  function refreshPrefill() {
    var scenario = currentScenario();
    if (!scenario) return;
    var template = scenario.reason_prefill || '';
    reasonInput.value = template.replace(/\{last_month\}/g, String(lastMonth()));
  }

  scenarioSelect.addEventListener('change', refreshPrefill);
  refreshPrefill();

  closeBtn.addEventListener('click', function () { collapse(); });

  function collapse() { card.style.display = 'none'; }

  function report(payload) {
    if (!BASE || !navigator.sendBeacon) return;
    try {
      var body = new Blob([JSON.stringify(payload)], { type: 'text/plain;charset=UTF-8' });
      navigator.sendBeacon(BASE + '/api/enterprise/oa-assistant/report', body);
    } catch (e) {}
  }

  async function run() {
    var scenario = currentScenario();
    if (!scenario) { logLine('没有可用场景', 'err'); return; }

    runBtn.disabled = true;
    var steps = [];
    resetLog('');
    logLine('场景：' + scenario.name + '（' + (scenario.id) + '）');

    if (isLoginPage()) {
      logLine('当前看起来是 OA 登录页：请先登录，回到报销表单页后再点「代填」。', 'warn');
      runBtn.disabled = false;
      return;
    }

    var amountValue = String(amountInput.value || '').trim();
    if (!amountValue || isNaN(Number(amountValue))) {
      logLine('报销金额未填写或格式不对（应为数字），已中止。', 'err');
      runBtn.disabled = false;
      return;
    }

    var eTicket = allETicket();
    var files = attachmentFiles.slice();
    if (eTicket && !files.length) {
      logLine('全为电子票但未选择附件：附件项会跳过，可在 OA 页面手动挂票据。', 'warn');
    }

    var values = {
      amount: amountValue,
      reason: String(reasonInput.value || '').trim(),
      company_name: companySelect.value,
      invoice_type: invoiceSelect.value,
      all_e_ticket: eTicket,
      attachment_paths: files.map(function (f) { return f.name; })
    };
    values['all_e_ticket=yes'] = eTicket;

    var fields = resolveFields(scenario);
    for (var i = 0; i < fields.length; i++) {
      var spec = fields[i][1];
      if (spec.enabled === false) continue;
      var when = spec.when;
      if (when && when in values && !values[when]) {
        steps.push({ field: spec.label || spec.key, status: 'skipped', detail: '条件 ' + when + ' 不满足' });
        logLine('○ ' + (spec.label || spec.key) + '：条件不满足，跳过', 'dim');
        continue;
      }
      try {
        var step = await fillField(spec, values, files);
        steps.push(step);
        if (step.status === 'filled') logLine('✓ ' + step.field + '：' + step.detail, 'ok');
        else if (step.status === 'todo') logLine('! ' + step.field + '：' + step.detail, 'warn');
        else logLine('○ ' + step.field + '：' + step.detail, 'dim');
      } catch (err) {
        steps.push({ field: spec.label || spec.key, status: 'error', detail: String((err && err.message) || err) });
        logLine('✗ ' + (spec.label || spec.key) + '：' + String((err && err.message) || err), 'err');
      }
    }

    if (saveCheck.checked) {
      var saveSpec = CFG.save_button || {};
      var saveSel = saveSpec.selector;
      if (!saveSel || saveSel === TODO) {
        steps.push({ field: saveSpec.label || '保存', status: 'todo', detail: '保存按钮定位器待配置' });
        logLine('! 保存：定位器待配置', 'warn');
      } else {
        var saveNode = findEl(saveSel);
        if (!saveNode) {
          steps.push({ field: saveSpec.label || '保存', status: 'error', detail: '未找到保存按钮' });
          logLine('✗ 保存：未找到按钮', 'err');
        } else {
          clickEl(saveNode);
          steps.push({ field: saveSpec.label || '保存', status: 'filled', detail: '已点击保存' });
          logLine('✓ 保存：已点击', 'ok');
        }
      }
    } else {
      logLine('（未勾选自动保存，OA 表单只填不提交）', 'dim');
    }

    var failed = steps.filter(function (s) { return s.status === 'error'; });
    var todos = steps.filter(function (s) { return s.status === 'todo'; });
    logLine('完成：成功 ' + steps.filter(function (s) { return s.status === 'filled'; }).length +
      ' · 跳过 ' + steps.filter(function (s) { return s.status === 'skipped'; }).length +
      ' · 失败 ' + failed.length + ' · 待配置 ' + todos.length,
      failed.length ? 'err' : 'ok');
    report({ kind: 'run', scenario_id: scenario.id, url: location.href, steps: steps, at: new Date().toISOString() });
    runBtn.disabled = false;
  }

  runBtn.addEventListener('click', function () { run(); });

  window.__SD_OA_PANEL__ = {
    toggle: function () { card.style.display = card.style.display === 'none' ? 'flex' : 'none'; },
    open: function () { card.style.display = 'flex'; },
    close: function () { card.style.display = 'none'; }
  };

  logLine('面板已就绪（配置版本 ' + (CFG.version || '?') + '）。', 'dim');
})();