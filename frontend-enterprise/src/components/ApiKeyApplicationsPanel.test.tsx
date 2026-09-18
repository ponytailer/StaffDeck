// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const notifySuccess = vi.fn();
const notifyError = vi.fn();

vi.mock('@/components/ui/app-toast', () => ({
  notify: {
    success: (...args: unknown[]) => notifySuccess(...args),
    error: (...args: unknown[]) => notifyError(...args),
    warning: vi.fn(),
    info: vi.fn(),
    loading: vi.fn(),
    dismiss: vi.fn(),
  },
}));

const API_KEY = 'nbe4NQE2nxthPzSH';
const API_URL = 'https://ai-gateway.folidaymall.com/ailab/v1/chat/completions';

const approvedApplication = {
  id: 'app-1',
  tenant_id: 'tenant_demo',
  user_id: 'user-1',
  username: 'huangsong',
  purpose: '存量 Key 同步（alilab_huangsong）',
  status: 'approved' as const,
  api_key_masked: 'nbe4****PzSH',
  api_key: API_KEY,
  api_url: API_URL,
  consumer_id: 'consumer-1',
  consumer_name: 'huangsong',
  consumer_status: 'enabled' as const,
  reviewer_note: null,
  reviewed_at: null,
  created_at: '2026-09-02T02:32:22Z',
  updated_at: '2026-09-02T02:32:22Z',
};

vi.mock('../api/client', () => ({
  TENANT_ID: 'tenant_demo',
  ApiError: class ApiError extends Error {},
  api: {
    get: vi.fn((url: string) =>
      Promise.resolve(url.includes('/usage') ? [] : [approvedApplication]),
    ),
    post: vi.fn(),
    delete: vi.fn(),
  },
}));

import { I18nProvider } from '@/i18n';

import { ApiKeyApplicationsPanel } from './ApiKeyApplicationsPanel';

const originalClipboard = navigator.clipboard;
const originalExecCommand = document.execCommand;

// NOTE: use fireEvent, not userEvent — userEvent.setup() installs its own
// navigator.clipboard stub, which would hide the very condition under test.
function setClipboard(value: Clipboard | undefined) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value });
}

/** Emulates legacy copy support: execCommand('copy') dispatches a real copy event. */
function stubCopyEventExecCommand(succeeds = true) {
  const setData = vi.fn();
  const execCommand = vi.fn((command: string) => {
    if (command !== 'copy') return false;
    if (!succeeds) return true;
    const event = new Event('copy', { bubbles: true, cancelable: true });
    Object.defineProperty(event, 'clipboardData', { value: { setData } });
    document.dispatchEvent(event);
    return true;
  });
  Object.defineProperty(document, 'execCommand', { configurable: true, value: execCommand });
  return { setData, execCommand };
}

async function renderApprovedPanel() {
  render(
    <I18nProvider>
      <ApiKeyApplicationsPanel />
    </I18nProvider>,
  );
  await screen.findByLabelText('复制 API Key');
}

beforeEach(() => {
  notifySuccess.mockClear();
  notifyError.mockClear();
});

afterEach(() => {
  cleanup();
  setClipboard(originalClipboard);
  Object.defineProperty(document, 'execCommand', {
    configurable: true,
    value: originalExecCommand,
  });
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

describe('ApiKeyApplicationsPanel copy buttons', () => {
  // Regression: the buttons used to call `navigator.clipboard.writeText` directly.
  // Where navigator.clipboard is undefined (plain HTTP origin, restricted webview)
  // that throws synchronously outside any try/catch, so nothing was copied and no
  // feedback appeared. jsdom has no navigator.clipboard, which reproduces it.
  it('copies the API Key when navigator.clipboard is unavailable', async () => {
    setClipboard(undefined);
    const { setData, execCommand } = stubCopyEventExecCommand();
    await renderApprovedPanel();

    fireEvent.click(screen.getByLabelText('复制 API Key'));

    await waitFor(() => expect(notifySuccess).toHaveBeenCalledWith('API Key已复制'));
    expect(execCommand).toHaveBeenCalledWith('copy');
    expect(setData).toHaveBeenCalledWith('text/plain', API_KEY);
    expect(notifyError).not.toHaveBeenCalled();
  });

  it('copies the gateway URL when navigator.clipboard is unavailable', async () => {
    setClipboard(undefined);
    const { setData } = stubCopyEventExecCommand();
    await renderApprovedPanel();

    fireEvent.click(screen.getByLabelText('复制网关地址'));

    await waitFor(() => expect(notifySuccess).toHaveBeenCalledWith('网关地址已复制'));
    expect(setData).toHaveBeenCalledWith('text/plain', API_URL);
    expect(notifyError).not.toHaveBeenCalled();
  });

  it('still prefers the Clipboard API when the browser exposes it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    setClipboard({ writeText } as unknown as Clipboard);
    const { execCommand } = stubCopyEventExecCommand();
    await renderApprovedPanel();

    fireEvent.click(screen.getByLabelText('复制 API Key'));

    await waitFor(() => expect(notifySuccess).toHaveBeenCalledWith('API Key已复制'));
    expect(writeText).toHaveBeenCalledWith(API_KEY);
    expect(execCommand).not.toHaveBeenCalled();
  });

  it('reports a failure instead of staying silent when no copy path works', async () => {
    setClipboard(undefined);
    stubCopyEventExecCommand(false);
    await renderApprovedPanel();

    fireEvent.click(screen.getByLabelText('复制 API Key'));

    await waitFor(() => expect(notifyError).toHaveBeenCalledWith('复制失败，请手动选择复制'));
    expect(notifySuccess).not.toHaveBeenCalled();
  });
});
