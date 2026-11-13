/**
 * content script 侧的网络 transport。
 *
 * 默认走 background 代理（Chromium 是 service worker，Firefox 是 event page，
 * 对外都是 runtime.connect 端口）——两者都凭 host_permissions 发请求，豁免 CORS；
 * SW 不可用时（理论不应发生）回退直连 fetch——同源页面下直连也能工作。
 * 对上层 api.ts 暴露统一的 proxyFetch：JSON 一次性返回，SSE 通过 onChunk 逐块回调。
 */

import { extRuntime, isExtensionContext } from './ext';

export type ProxyFetchResult = {
  ok: boolean;
  status: number;
  body: string;
  /** true 表示响应是流式且已通过 onChunk 读完 */
  streamed?: boolean;
};

type ProxyInit = {
  method: string;
  headers: Record<string, string>;
  body?: string;
  signal?: AbortSignal;
  onChunk?: (text: string) => void;
};

function abortLikeError(): Error {
  const e = new Error('aborted');
  e.name = 'AbortError';
  return e;
}

export function proxyFetch(url: string, init: ProxyInit): Promise<ProxyFetchResult> {
  if (!isExtensionContext()) return directFetch(url, init);

  return new Promise<ProxyFetchResult>((resolve, reject) => {
    let port: chrome.runtime.Port;
    try {
      port = extRuntime.connect('sd-proxy');
    } catch {
      void directFetch(url, init).then(resolve, reject);
      return;
    }

    const onAbort = () => {
      port.disconnect();
      reject(abortLikeError());
    };
    if (init.signal?.aborted) {
      reject(abortLikeError());
      return;
    }
    init.signal?.addEventListener('abort', onAbort, { once: true });

    let settled = false;
    const finish = (fn: () => void) => {
      if (settled) return;
      settled = true;
      init.signal?.removeEventListener('abort', onAbort);
      try {
        port.disconnect();
      } catch {
        // 已断开
      }
      fn();
    };

    port.onMessage.addListener((msg: unknown) => {
      if (typeof msg !== 'object' || msg === null) return;
      const m = msg as Record<string, unknown>;
      if (m.kind === 'response') {
        const result: ProxyFetchResult = {
          ok: m.ok === true,
          status: typeof m.status === 'number' ? m.status : 0,
          body: typeof m.body === 'string' ? m.body : '',
          streamed: m.stream === true ? true : undefined,
        };
        if (!result.streamed) {
          finish(() => resolve(result));
        } else if (!result.ok || result.status === 0) {
          // 错误响应不会流式，直接结束
          finish(() => resolve(result));
        }
        // 成功的流式响应：等 chunk/end
        return;
      }
      if (m.kind === 'chunk') {
        if (typeof m.text === 'string' && m.text) init.onChunk?.(m.text);
        return;
      }
      if (m.kind === 'end') {
        finish(() => resolve({ ok: true, status: 200, body: '', streamed: true }));
      }
    });

    port.onDisconnect.addListener(() => {
      // SW 正常读完会主动 disconnect；异常断开（SW 被杀）且未 settled 则报网络错误
      finish(() => {
        reject(new Error('proxy_disconnected'));
      });
    });

    port.postMessage({
      kind: 'fetch',
      url,
      method: init.method,
      headers: init.headers,
      body: init.body,
    });
  });
}

async function directFetch(url: string, init: ProxyInit): Promise<ProxyFetchResult> {
  const response = await fetch(url, {
    method: init.method,
    headers: init.headers,
    body: init.body,
    signal: init.signal,
  });
  if (response.body && init.onChunk) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      init.onChunk(decoder.decode(value, { stream: true }));
    }
    return { ok: response.ok, status: response.status, body: '', streamed: true };
  }
  return { ok: response.ok, status: response.status, body: await response.text() };
}
