// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AiReviewMergeRequest, AiReviewPreset, AiReviewWorkspace } from '../../api/aiReview';
import { I18nProvider } from '../../i18n';
import CreateReviewTaskDialog from './CreateReviewTaskDialog';

const fetchMergeRequests = vi.fn();
const fetchPresets = vi.fn();
const createTask = vi.fn();

vi.mock('../../api/aiReview', () => ({
  fetchAiReviewMergeRequests: (...args: unknown[]) => fetchMergeRequests(...args),
  fetchAiReviewPresets: (...args: unknown[]) => fetchPresets(...args),
  createAiReviewTask: (...args: unknown[]) => createTask(...args),
}));

vi.mock('@/components/ui/app-toast', () => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

const WORKSPACE: AiReviewWorkspace = {
  id: 'ws-1',
  name: 'StaffDeck',
  platform: 'github',
  repo_url: 'https://github.com/ponytailer/StaffDeck.git',
  repo_path: 'ponytailer/StaffDeck',
  default_branch: 'main',
  created_at: null,
  created_by: 'admin',
};

const PRESET: AiReviewPreset = {
  id: 'preset-1',
  name: '安全口径',
  content: '关注越权',
  is_default: true,
  created_at: null,
  updated_at: null,
};

function makeMr(overrides: Partial<AiReviewMergeRequest> = {}): AiReviewMergeRequest {
  return {
    number: 42,
    title: '修复登录超时',
    description: '',
    source_branch: 'fix/login-timeout',
    target_branch: 'main',
    author: 'ponytailer',
    web_url: 'https://github.com/ponytailer/StaffDeck/pull/42',
    updated_at: '',
    in_flight_status: '',
    in_flight_task_id: '',
    in_flight_since: null,
    ...overrides,
  };
}

/** 组件的 Input / Textarea 依赖 useI18n，测试里套一层 Provider。 */
function renderDialog(props: Parameters<typeof CreateReviewTaskDialog>[0]) {
  return render(
    <I18nProvider>
      <CreateReviewTaskDialog {...props} />
    </I18nProvider>,
  );
}

const PAGE_EMPTY = { items: [], total: 0, page: 1, page_size: 50, has_more: false };

function expectSubmitLocked() {
  const button = screen.getByRole('button', { name: '已有评审任务' }) as HTMLButtonElement;
  expect(button.disabled).toBe(true);
}

afterEach(() => {
  cleanup();
  fetchMergeRequests.mockReset();
  fetchPresets.mockReset();
  createTask.mockReset();
});

describe('CreateReviewTaskDialog · 同一 PR/MR 只允许一个进行中任务', () => {
  it('选中的 PR/MR 已有进行中任务时锁住提交，并说明原因与出路', async () => {
    fetchMergeRequests.mockResolvedValue({
      ...PAGE_EMPTY,
      items: [makeMr({ in_flight_status: 'running', in_flight_task_id: 'task-1' })],
    });
    fetchPresets.mockResolvedValue([PRESET]);

    renderDialog({
      open: true,
      onClose: () => {},
      workspace: WORKSPACE,
      initialMr: makeMr({ in_flight_status: 'running', in_flight_task_id: 'task-1' }),
      onCreated: () => {},
    });

    await waitFor(() => expect(screen.getByText(/已有进行中的评审任务（评审中）/)).toBeTruthy());
    expect(screen.getByText(/同一 PR\/MR 同时只能有一个评审任务/)).toBeTruthy();
    expect(screen.getByText(/恢复可用/)).toBeTruthy();
    expect(screen.getByText(/删除或重试/)).toBeTruthy();
    // 默认预设已勾上，所以「锁住」只能来自占用判定
    expectSubmitLocked();
    expect(createTask).not.toHaveBeenCalled();
  });

  it('排队中的任务按「排队中」提示，不是「评审中」', async () => {
    fetchMergeRequests.mockResolvedValue(PAGE_EMPTY);
    fetchPresets.mockResolvedValue([PRESET]);

    renderDialog({
      open: true,
      onClose: () => {},
      workspace: WORKSPACE,
      initialMr: makeMr({ in_flight_status: 'queued' }),
      onCreated: () => {},
    });

    await waitFor(() => expect(screen.getByText(/已有进行中的评审任务（排队中）/)).toBeTruthy());
    expectSubmitLocked();
  });

  it('父级轮询到的进行中状态优先于列表快照（任务刚发起也能拦住）', async () => {
    fetchMergeRequests.mockResolvedValue(PAGE_EMPTY);
    fetchPresets.mockResolvedValue([PRESET]);

    renderDialog({
      open: true,
      onClose: () => {},
      workspace: WORKSPACE,
      initialMr: makeMr(),
      inFlightByMr: new Map([[42, 'running']]),
      onCreated: () => {},
    });

    await waitFor(() => expect(screen.getByText(/已有进行中的评审任务（评审中）/)).toBeTruthy());
    expectSubmitLocked();
  });

  it('没有进行中任务时正常给出创建入口', async () => {
    fetchMergeRequests.mockResolvedValue({ ...PAGE_EMPTY, items: [makeMr()], total: 1 });
    fetchPresets.mockResolvedValue([PRESET]);

    renderDialog({
      open: true,
      onClose: () => {},
      workspace: WORKSPACE,
      initialMr: makeMr(),
      onCreated: () => {},
    });

    await waitFor(() => {
      const button = screen.getByRole('button', { name: /创建评审任务/ }) as HTMLButtonElement;
      expect(button.disabled).toBe(false);
    });
    expect(screen.queryByText(/已有进行中的评审任务/)).toBeNull();
  });

  it('弹窗内的选择列表把进行中的 PR/MR 标出来并禁止选中', async () => {
    fetchMergeRequests.mockResolvedValue({
      ...PAGE_EMPTY,
      items: [makeMr({ in_flight_status: 'running' }), makeMr({ number: 43, title: '其他改动' })],
      total: 2,
    });
    fetchPresets.mockResolvedValue([PRESET]);

    renderDialog({
      open: true,
      onClose: () => {},
      workspace: WORKSPACE,
      initialMr: null,
      onCreated: () => {},
    });

    await waitFor(() => expect(screen.getByText('评审中')).toBeTruthy());
    const blockedRow = screen.getByRole('button', {
      name: /PR\/MR #42 已有进行中的评审任务/,
    }) as HTMLButtonElement;
    expect(blockedRow.disabled).toBe(true);
    const freeRow = screen.getByRole('button', { name: /#43 其他改动/ }) as HTMLButtonElement;
    expect(freeRow.disabled).toBe(false);
  });
});
