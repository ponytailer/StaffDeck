// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import type { MCPAppViewDescriptor } from '../chatTypes';
import MCPAppView from './MCPAppView';

const descriptor: MCPAppViewDescriptor = {
  server_id: 'srv-1',
  resource_uri: 'ui://demo/app.html',
  tool_name: 'demo_tool',
  visibility: ['chat'],
  mime_type: 'text/html',
  tenant_id: 'tenant_demo',
  agent_id: null,
  session_id: null,
  active_skill_id: null,
  initial_result: null,
  initial_meta: {},
};

const appResource = {
  server_id: 'srv-1',
  uri: 'ui://demo/app.html',
  mime_type: 'text/html',
  text: '<div>demo app</div>',
  meta: {},
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

/** 按 URL 分派 fetch：app-resource 一次，app-call 依次返回给定响应。 */
function stubFetch(appCallResponses: unknown[]) {
  const calls: Array<{ url: string; body: Record<string, unknown> | null }> = [];
  const queue = [...appCallResponses];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    let body: Record<string, unknown> | null = null;
    if (typeof init?.body === 'string') {
      body = JSON.parse(init.body) as Record<string, unknown>;
    }
    calls.push({ url, body });
    if (url.includes('/app-resource')) return jsonResponse(appResource);
    if (url.includes('/app-call')) return jsonResponse(queue.shift() ?? { success: true, result: null });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

/** 组件用 iframe.contentWindow 校验消息来源；jsdom 下自造一个窗口最稳。 */
function iframeWindow(): Window {
  const iframe = screen.getByTitle('MCP App demo_tool') as HTMLIFrameElement;
  const fake = { postMessage: vi.fn() } as unknown as Window;
  Object.defineProperty(iframe, 'contentWindow', { configurable: true, value: fake });
  return fake;
}

function dispatchToolCall(source: Window, id = 1) {
  window.dispatchEvent(
    new MessageEvent('message', {
      data: { jsonrpc: '2.0', id, method: 'tools/call', params: { name: 'demo_tool', arguments: {} } },
      source: source as unknown as MessageEventSource,
    }),
  );
}

async function renderView() {
  render(
    <I18nProvider>
      <MCPAppView descriptor={descriptor} />
    </I18nProvider>,
  );
  await screen.findByTitle('MCP App demo_tool');
}

beforeAll(() => {
  window.HTMLElement.prototype.hasPointerCapture = vi.fn();
  window.HTMLElement.prototype.releasePointerCapture = vi.fn();
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('MCPAppView 副作用工具确认', () => {
  it('requires_confirmation 时先弹统一确认框，不做浏览器原生确认', async () => {
    const user = userEvent.setup();
    // 回归：以前这里是 window.confirm，必须已换成 ConfirmDialog
    const confirmSpy = vi.spyOn(window, 'confirm');
    const calls = stubFetch([
      { success: false, requires_confirmation: true },
      { success: true, result: { ok: true } },
    ]);
    await renderView();

    dispatchToolCall(iframeWindow());

    const dialog = await screen.findByRole('alertdialog');
    expect(screen.getByText('执行工具「demo_tool」？')).toBeTruthy();
    // 确认前只发出第一次探测调用，不能带 confirm_side_effect
    const appCalls = calls.filter((call) => call.url.includes('/app-call'));
    expect(appCalls).toHaveLength(1);
    expect(appCalls[0].body?.confirm_side_effect).toBe(false);

    await user.click(screen.getByRole('button', { name: '继续执行' }));

    await waitFor(() => {
      expect(calls.filter((call) => call.url.includes('/app-call'))).toHaveLength(2);
    });
    const second = calls.filter((call) => call.url.includes('/app-call'))[1];
    expect(second.body?.confirm_side_effect).toBe(true);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('取消确认时不带 confirm_side_effect 重放，并回 RPC 取消错误', async () => {
    const user = userEvent.setup();
    const calls = stubFetch([{ success: false, requires_confirmation: true }]);
    await renderView();

    const fake = iframeWindow();
    dispatchToolCall(fake, 7);

    const dialog = await screen.findByRole('alertdialog');
    await user.click(screen.getByRole('button', { name: '取消' }));

    const posted = (fake.postMessage as unknown as ReturnType<typeof vi.fn>).mock.calls;
    await waitFor(() => {
      expect(posted.length).toBeGreaterThan(0);
    });
    const payload = posted[posted.length - 1][0] as {
      id?: number;
      error?: { code?: number; message?: string };
    };
    expect(payload.id).toBe(7);
    expect(payload.error?.code).toBe(-32001);
    expect(calls.filter((call) => call.url.includes('/app-call'))).toHaveLength(1);
  });

  it('不需要确认的工具直接调用', async () => {
    const calls = stubFetch([{ success: true, result: { value: 1 } }]);
    await renderView();

    const fake = iframeWindow();
    dispatchToolCall(fake);

    await waitFor(() => {
      expect(fake.postMessage as unknown as ReturnType<typeof vi.fn>).toHaveBeenCalled();
    });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(calls.filter((call) => call.url.includes('/app-call'))).toHaveLength(1);
  });
});
