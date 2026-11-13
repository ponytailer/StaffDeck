/**
 * 跨浏览器扩展 API 适配层（Chrome / Edge / Firefox 共用一份业务代码）。
 *
 * 两个浏览器的差异集中在三处，统一在这里抹平，上层只写 await：
 *  1. 命名空间：Chromium 只有 `chrome.*`；Firefox 两者都有，但语义不同 ——
 *     `browser.*` 返回 Promise，`chrome.*` 只能回调。
 *  2. 返回形态：Promise 风格直接 resolve；回调风格要读 `runtime.lastError`。
 *  3. 后台形态：Chrome 是 service worker（无 window），Firefox 是 event page。
 *     这一条不影响 API 调用，只影响 manifest，见 manifest.firefox.json。
 *
 * 判定方式：Firefox 的 `browser` 命名空间带 `runtime.id`，Chromium 里 `browser`
 * 未定义（除非装了 polyfill，那种情况下它也是 Promise 风格，判断依然成立）。
 */

type ChromeNs = typeof chrome;
type BrowserLike = Record<string, any>; // Firefox 的 browser.* 没有类型包，按鸭子类型用

const g = globalThis as unknown as { browser?: BrowserLike; chrome?: ChromeNs };

/** Firefox 且 Promise 风格 */
const nativeBrowser: BrowserLike | null = g.browser?.runtime?.id ? g.browser : null;
/** 兜底空对象：非扩展环境（单测、调试页）下也不至于在模块求值阶段就崩 */
const api: BrowserLike = (nativeBrowser ?? g.chrome ?? {}) as BrowserLike;

export const isFirefox = nativeBrowser !== null;

/**
 * 当前是否跑在扩展环境里（content script 内为 true）。
 * 单元测试 / 纯网页里打开调试页时为 false，上层据此回退直连 fetch 或 window.open。
 */
export function isExtensionContext(): boolean {
  try {
    return Boolean(api?.runtime?.id);
  } catch {
    return false;
  }
}

/**
 * 调用一个扩展 API 并统一成 Promise。
 * - Promise 风格（Firefox browser.*）：直接 await 返回值；
 * - 回调风格（Chrome chrome.*）：补一个回调，回调里读 lastError 转成 reject。
 */
function call<T>(invoke: (cb?: (value: T) => void) => unknown): Promise<T> {
  if (nativeBrowser) {
    try {
      return Promise.resolve(invoke() as Promise<T>);
    } catch (e) {
      return Promise.reject(e);
    }
  }
  return new Promise<T>((resolve, reject) => {
    try {
      invoke((value: T) => {
        const err = (api.runtime as { lastError?: { message?: string } } | undefined)?.lastError;
        if (err) reject(new Error(err.message || 'extension_api_error'));
        else resolve(value);
      });
    } catch (e) {
      reject(e);
    }
  });
}

export const extStorage = {
  get<T>(key: string): Promise<T | null> {
    return call<Record<string, unknown>>((cb) =>
      cb ? api.storage.local.get(key, cb) : api.storage.local.get(key),
    ).then((items) => ((items ?? {})[key] as T) ?? null);
  },
  set(items: Record<string, unknown>): Promise<void> {
    return call<void>((cb) =>
      cb ? api.storage.local.set(items, cb) : api.storage.local.set(items),
    );
  },
  remove(key: string): Promise<void> {
    return call<void>((cb) =>
      cb ? api.storage.local.remove(key, cb) : api.storage.local.remove(key),
    );
  },
};

export const extRuntime = {
  id: (): string => (api.runtime?.id as string | undefined) ?? '',
  getURL: (path: string): string => api.runtime.getURL(path) as string,
  connect: (name: string) => api.runtime.connect({ name }) as chrome.runtime.Port,
  onMessage: api.runtime?.onMessage as chrome.runtime.ExtensionMessageEvent,
  onConnect: api.runtime?.onConnect as chrome.runtime.ExtensionConnectEvent,
  /**
   * 给后台发消息。后台没醒/没注册监听时浏览器会丢 lastError，
   * 这里统一吞掉并返回 null —— 调用方按「没拿到响应」处理即可。
   */
  async sendMessage<T>(message: unknown): Promise<T | null> {
    try {
      const res = await call<T | undefined>((cb) =>
        cb ? api.runtime.sendMessage(message, cb) : api.runtime.sendMessage(message),
      );
      return (res ?? null) as T | null;
    } catch {
      return null;
    }
  },
};

export const extTabs = {
  query: (): Promise<chrome.tabs.Tab[]> =>
    call<chrome.tabs.Tab[]>((cb) => (cb ? api.tabs.query({}, cb) : api.tabs.query({}))).then(
      (tabs) => tabs ?? [],
    ),
  create: (url: string): Promise<void> =>
    call<unknown>((cb) => (cb ? api.tabs.create({ url }, cb) : api.tabs.create({ url }))).then(
      () => undefined,
    ),
};

export const extScripting = {
  /**
   * 在目标标签页的页面上下文里执行函数并取回结果。
   *
   * Firefox 只有 128+ 才认 `world: 'MAIN'`，更早的版本（含 115 ESR）会直接抛错。
   * 退化路径去默认的隔离环境执行：那里 `window.localStorage` 读到的仍是页面
   * 同源存储，取到的会话完全一致，只是少了「与页面共享全局」这一层。
   */
  async executeInPage<T>(tabId: number, func: () => T): Promise<{ found: boolean; value: T | null }> {
    const run = (withWorld: boolean) =>
      call<Array<{ result?: T; error?: unknown }>>((cb) => {
        const details: Record<string, unknown> = { target: { tabId }, func };
        if (withWorld) details.world = 'MAIN';
        return cb ? api.scripting.executeScript(details, cb) : api.scripting.executeScript(details);
      });

    for (const withWorld of [true, false]) {
      try {
        const results = await run(withWorld);
        const first = results?.[0];
        // 页面里的函数自己抛错：换环境也没用，直接当没读到
        if (first && first.error != null) continue;
        if (first) return { found: true, value: (first.result ?? null) as T | null };
      } catch {
        // 该标签正在加载 / 不支持 world 参数，换下一种
      }
    }
    return { found: false, value: null };
  },
};

/**
 * 注册后台消息处理器，屏蔽两个浏览器的响应协议差异：
 *  - Firefox（browser.*）：监听器返回 Promise 即为异步响应；
 *  - Chromium（chrome.*）：必须 sendResponse + return true。
 * 处理器返回 undefined 表示「这条消息不归我管」，交回浏览器。
 */
export function onBackgroundMessage(handler: (msg: unknown) => Promise<unknown> | undefined): void {
  if (nativeBrowser) {
    extRuntime.onMessage.addListener(((msg: unknown) => handler(msg) ?? undefined) as never);
    return;
  }
  extRuntime.onMessage.addListener(
    ((msg: unknown, _sender: unknown, sendResponse: (v: unknown) => void) => {
      const result = handler(msg);
      if (!result) return false;
      void result.then(sendResponse, () => sendResponse({ ok: false }));
      return true;
    }) as never,
  );
}
