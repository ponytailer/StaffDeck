// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import MemoriesTab from './MemoriesTab';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

/** 记忆列表返回空、清空返回 deleted: 2，并记录所有请求。 */
function stubFetch() {
  const calls: Array<{ url: string; method: string }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = String(init?.method || 'GET').toUpperCase();
    calls.push({ url, method });
    if (url.includes('/memories/me') && method === 'DELETE') return jsonResponse({ deleted: 2 });
    return jsonResponse([]);
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

async function renderTab() {
  render(
    <I18nProvider>
      <MemoriesTab />
    </I18nProvider>,
  );
  const button = await screen.findByRole('button', { name: '清空我的记忆' });
  // 首屏加载完成前按钮是 disabled 的（项目未注册 jest-dom matcher，直接读 DOM 属性）
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
  return button;
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

describe('MemoriesTab 清空记忆确认', () => {
  it('点击清空先弹统一确认框，不做浏览器原生确认', async () => {
    // 回归：以前这里是 window.confirm，必须已换成 ConfirmDialog
    const confirmSpy = vi.spyOn(window, 'confirm');
    const calls = stubFetch();
    const button = await renderTab();

    await userEvent.setup().click(button);

    expect(await screen.findByText('清空你的长期记忆？')).toBeTruthy();
    expect(screen.getByText(/长期记忆，不会影响其他用户/)).toBeTruthy();
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('确认后才发起清空请求并关闭弹窗', async () => {
    const user = userEvent.setup();
    const calls = stubFetch();
    const button = await renderTab();

    await user.click(button);
    await user.click(await screen.findByRole('button', { name: '清空' }));

    await waitFor(() => {
      expect(calls.some((call) => call.method === 'DELETE' && call.url.includes('/memories/me'))).toBe(true);
    });
    await waitFor(() => {
      expect(screen.queryByText('清空你的长期记忆？')).toBeNull();
    });
  });

  it('取消时不下发任何清空请求', async () => {
    const user = userEvent.setup();
    const calls = stubFetch();
    const button = await renderTab();

    await user.click(button);
    await user.click(await screen.findByRole('button', { name: '取消' }));

    await waitFor(() => {
      expect(screen.queryByText('清空你的长期记忆？')).toBeNull();
    });
    expect(calls.some((call) => call.method === 'DELETE')).toBe(false);
  });
});
