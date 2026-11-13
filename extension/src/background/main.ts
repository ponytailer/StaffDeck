/**
 * MV3 service worker：统一网络代理。
 *
 * 为什么需要：MV3 的 content script 与宿主页面同源，跨源 fetch 受页面 CORS
 * 约束（生产网关对 OPTIONS 返回 400、无 CORS 头），登录/列表/流式全挂。
 * service worker 凭 manifest host_permissions 发请求完全豁免 CORS——
 * 这是 MV3 官方推荐的网络层位置，后端无需任何 CORS 改动。
 *
 * 协议：content script 通过 chrome.runtime.connect({ name: 'sd-proxy' }) 建立
 * 端口，首条消息发 { kind: 'fetch', url, method, headers, body }：
 *  - JSON 响应 → 回 { kind: 'response', ok, status, body }
 *  - SSE 流式 → 逐块回 { kind: 'chunk', text }，结束回 { kind: 'end' }
 * content script 断开端口（abort/页面关闭）→ SW abort 底层 fetch。
 */

type ProxyFetchRequest = {
  kind: 'fetch';
  url: string;
  method: string;
  headers: Record<string, string>;
  body?: string;
};

function isFetchRequest(v: unknown): v is ProxyFetchRequest {
  return (
    typeof v === 'object' &&
    v !== null &&
    (v as { kind?: unknown }).kind === 'fetch' &&
    typeof (v as { url?: unknown }).url === 'string'
  );
}

async function runProxyFetch(port: chrome.runtime.Port, req: ProxyFetchRequest): Promise<void> {
  const controller = new AbortController();
  port.onDisconnect.addListener(() => controller.abort());

  let response: Response;
  try {
    response = await fetch(req.url, {
      method: req.method,
      headers: req.headers,
      body: req.body,
      signal: controller.signal,
    });
  } catch (e) {
    if (controller.signal.aborted) return; // 端口已断开，无处可回
    port.postMessage({ kind: 'response', ok: false, status: 0, body: `network_error: ${String(e)}` });
    return;
  }

  const contentType = response.headers.get('content-type') || '';
  const isStream = contentType.includes('text/event-stream');

  if (!isStream) {
    const body = await response.text();
    try {
      port.postMessage({ kind: 'response', ok: response.ok, status: response.status, body });
    } catch {
      // 端口已断开
    }
    return;
  }

  // 流式：先回响应头状态，再逐块转发
  try {
    port.postMessage({ kind: 'response', ok: response.ok, status: response.status, body: '', stream: true });
  } catch {
    return;
  }
  if (!response.ok || !response.body) return;

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      port.postMessage({ kind: 'chunk', text: decoder.decode(value, { stream: true }) });
    }
    port.postMessage({ kind: 'end' });
  } catch {
    try {
      port.postMessage({ kind: 'end', aborted: true });
    } catch {
      // 端口已断开
    }
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'sd-proxy') return;
  port.onMessage.addListener((msg: unknown) => {
    if (!isFetchRequest(msg)) return;
    void runProxyFetch(port, msg);
  });
});

/** content script 里没有 chrome.tabs（仅 background 可用），新开 tab 由 SW 代劳 */
chrome.runtime.onMessage.addListener((msg: unknown, _sender, sendResponse) => {
  if (typeof msg !== 'object' || msg === null) return false;

  if ((msg as { type?: unknown }).type === 'open-tab' && typeof (msg as { url?: unknown }).url === 'string') {
    void chrome.tabs.create({ url: (msg as { url: string }).url });
    sendResponse({ ok: true });
    return false; // 同步响应
  }

  if ((msg as { type?: unknown }).type === 'read-platform-auth') {
    void readPlatformAuth().then((session) => sendResponse({ ok: true, session }));
    return true; // 异步响应：保持消息通道打开
  }

  return false;
});

/* ---------------- 平台登录态读取（插件自动登录用） ---------------- */

/** 与 manifest host_permissions 对齐：只有这些站点上的 localStorage 才可能是平台会话 */
const PLATFORM_PREFIXES = [
  'https://ai-staff.folidaymall.com',
  'http://localhost:8000',
  'http://127.0.0.1:8000',
];

type PlatformSession = {
  token: string;
  tenantId: string | null;
  expiresAt: number | null;
  origin: string;
};

/**
 * 注入到平台页 MAIN world 执行：读平台前端写下的会话。
 * 注意必须自包含——这个函数体会被序列化后送到页面里，不能引用模块作用域变量。
 */
function readAuthInPage(): PlatformSession | null {
  try {
    const raw = window.localStorage.getItem('ultrarag_auth');
    if (!raw) return null;
    const parsed = JSON.parse(raw) as {
      token?: unknown;
      user?: { tenant_id?: unknown };
      expires_at?: unknown;
    };
    if (!parsed || typeof parsed.token !== 'string' || !parsed.token) return null;
    return {
      token: parsed.token,
      tenantId:
        parsed.user && typeof parsed.user.tenant_id === 'string' ? parsed.user.tenant_id : null,
      expiresAt: typeof parsed.expires_at === 'number' ? parsed.expires_at : null,
      origin: window.location.origin,
    };
  } catch {
    return null;
  }
}

/** 在开着的平台标签页里找登录态；当前激活的标签优先 */
async function readPlatformAuth(): Promise<PlatformSession | null> {
  let tabs: chrome.tabs.Tab[];
  try {
    tabs = await chrome.tabs.query({});
  } catch {
    return null;
  }
  // 没有 tabs 权限时，tab.url 只在 host_permissions 命中的标签上才有值——正好够用
  const candidates = tabs
    .filter((tab) => typeof tab.url === 'string' && PLATFORM_PREFIXES.some((p) => tab.url!.startsWith(p)))
    .sort((a, b) => Number(b.active) - Number(a.active));

  for (const tab of candidates) {
    if (typeof tab.id !== 'number') continue;
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        world: 'MAIN',
        func: readAuthInPage,
      });
      const session = results?.[0]?.result as PlatformSession | null | undefined;
      if (session && typeof session.token === 'string' && session.token) return session;
    } catch {
      // 该标签正在加载/被浏览器策略限制，换下一个
    }
  }
  return null;
}
