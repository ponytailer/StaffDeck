// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AiReviewTaskDetail } from '../../api/aiReview';
import { ApiError } from '../../api/client';
import ReviewResultDialog from './ReviewResultDialog';

const fetchDetail = vi.fn();
const syncTask = vi.fn();

vi.mock('../../api/aiReview', () => ({
  AI_REVIEW_PLATFORM_WRITE_DENIED_CODE: 'AI_REVIEW_PLATFORM_WRITE_DENIED',
  fetchAiReviewTaskDetail: (...args: unknown[]) => fetchDetail(...args),
  syncAiReviewTaskToPlatform: (...args: unknown[]) => syncTask(...args),
}));

vi.mock('@/components/ui/app-toast', () => ({
  notify: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

function makeDetail(overrides: Partial<AiReviewTaskDetail> = {}): AiReviewTaskDetail {
  return {
    id: 'task-1',
    workspace_id: 'ws-1',
    workspace_name: 'pydantic-client',
    status: 'succeeded',
    mr_number: 170,
    mr_title: 'Bump pydantic',
    mr_description: '',
    source_branch: 'main',
    target_branch: 'main',
    author: 'dependabot',
    web_url: 'https://github.com/ponytailer/pydantic-client/pull/170',
    requirements: '',
    error: '',
    created_at: '2026-09-24T02:00:00Z',
    started_at: null,
    finished_at: null,
    platform_synced_at: null,
    platform_sync_url: '',
    has_result: false,
    result_json: [],
    summary_json: { ocr_status: 'skipped', files_reviewed: 0 },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  fetchDetail.mockReset();
  syncTask.mockReset();
});

describe('ReviewResultDialog · ocr skipped 语义化', () => {
  it('skipped 显示「无可评审文件（已跳过）」+ 成因 + 建议，且不提供回写按钮', async () => {
    fetchDetail.mockResolvedValue(makeDetail());
    render(<ReviewResultDialog taskId="task-1" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('无可评审文件（已跳过）')).toBeTruthy());
    // 结论句带实际评审文件数
    expect(screen.getByText(/实际评审文件数 0/)).toBeTruthy();
    // 同分支 → 确定性的空 diff 成因
    expect(screen.getByText(/源分支与目标分支相同（均为 main）/)).toBeTruthy();
    // 处置建议
    expect(screen.getByText(/「自定义评审规则」的 include/)).toBeTruthy();
    // 没有意见可回写
    expect(screen.queryByText('回写到 PR / MR')).toBeNull();
    // 不再落到「变更很小」的通用空态
    expect(screen.queryByText(/这次评审没有产出行级意见/)).toBeNull();
  });

  it('正常产出意见时展示评论并给出回写入口，不出现跳过面板', async () => {
    fetchDetail.mockResolvedValue(
      makeDetail({
        source_branch: 'feature/x',
        target_branch: 'main',
        summary_json: { ocr_status: 'succeeded', files_reviewed: 2, model: 'deepseek-v4.1-flash' },
        result_json: [{ path: 'src/a.py', start_line: 3, end_line: 5, content: '缺少错误处理' }],
      }),
    );
    render(<ReviewResultDialog taskId="task-1" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('已完成')).toBeTruthy());
    expect(screen.getByText('缺少错误处理')).toBeTruthy();
    expect(screen.getByText('回写到 PR / MR')).toBeTruthy();
    expect(screen.queryByText(/实际评审文件数/)).toBeNull();
  });
});

describe('ReviewResultDialog · 评审思路可读性', () => {
  // 故意做成「段落 + 列表 + 段落」：原来的实现会把它们糊成一整段
  const thinking = 'First paragraph spans\none wrapped line.\n\n- item a\n- item b\n\nLast paragraph.';

  function renderWithThinking() {
    fetchDetail.mockResolvedValue(
      makeDetail({
        source_branch: 'feature/x',
        target_branch: 'main',
        summary_json: { ocr_status: 'succeeded', files_reviewed: 1 },
        result_json: [{ path: 'src/a.py', start_line: 1, end_line: 2, content: '问题', thinking }],
      }),
    );
    return render(<ReviewResultDialog taskId="task-1" onClose={() => {}} />);
  }

  it('默认收起，并标注体量，避免上万字符默认铺开', async () => {
    renderWithThinking();
    await waitFor(() => expect(screen.getByText('评审思路')).toBeTruthy());

    const details = document.querySelector('details');
    expect(details).toBeTruthy();
    expect(details?.open).toBe(false);
    expect(screen.getByText(/^\d+ 字符$/)).toBeTruthy();
    expect(screen.getByText('模型内部推理，未做整理')).toBeTruthy();
  });

  it('展开后按空行分段：段内压换行、列表段保结构', async () => {
    renderWithThinking();
    await waitFor(() => expect(screen.getByText('评审思路')).toBeTruthy());

    const blocks = Array.from(document.querySelectorAll('details p')).map((node) => node.textContent);
    expect(blocks).toEqual([
      'First paragraph spans one wrapped line.',
      '- item a\n- item b',
      'Last paragraph.',
    ]);
  });

  it('没有 thinking 时不渲染这一块', async () => {
    fetchDetail.mockResolvedValue(
      makeDetail({
        source_branch: 'feature/x',
        target_branch: 'main',
        summary_json: { ocr_status: 'succeeded', files_reviewed: 1 },
        result_json: [{ path: 'src/a.py', start_line: 1, end_line: 2, content: '问题' }],
      }),
    );
    render(<ReviewResultDialog taskId="task-1" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByText('问题')).toBeTruthy());
    expect(screen.queryByText('评审思路')).toBeNull();
    expect(document.querySelector('details')).toBeNull();
  });
});

describe('ReviewResultDialog · 回写失败引导', () => {
  function primeSyncableDetail() {
    fetchDetail.mockResolvedValue(
      makeDetail({
        source_branch: 'feature/x',
        target_branch: 'main',
        summary_json: { ocr_status: 'succeeded', files_reviewed: 1 },
        result_json: [{ path: 'src/a.py', start_line: 3, end_line: 5, content: '缺少错误处理' }],
      }),
    );
  }

  async function triggerSync() {
    await waitFor(() => expect(screen.getByText('回写到 PR / MR')).toBeTruthy());
    fireEvent.click(screen.getByText('回写到 PR / MR'));
    await waitFor(() => expect(screen.getByText('回写')).toBeTruthy());
    fireEvent.click(screen.getByText('回写'));
  }

  it('token 缺写权限：常驻引导 + 可点的设置页链接 + 可跳转平台设置', async () => {
    primeSyncableDetail();
    syncTask.mockRejectedValue(
      new ApiError(
        502,
        JSON.stringify({
          detail: {
            code: 'AI_REVIEW_PLATFORM_WRITE_DENIED',
            message:
              '回写 PR 评论被 GitHub 拒绝：这个 token 能读仓库、但没有写权限。见 https://github.com/settings/tokens?type=beta',
          },
        }),
        'Bad Gateway',
      ),
    );
    const onOpenSettings = vi.fn();
    render(
      <ReviewResultDialog taskId="task-1" onClose={() => {}} onOpenSettings={onOpenSettings} />,
    );
    await triggerSync();

    await waitFor(() =>
      expect(screen.getByText('回写被代码平台拒绝：token 缺少写权限')).toBeTruthy(),
    );
    expect(screen.getByText(/这个 token 能读仓库、但没有写权限/)).toBeTruthy();
    // 报错里的设置页地址渲染成可点链接，而不是纯文本
    const link = screen.getByText('https://github.com/settings/tokens?type=beta');
    expect(link.tagName).toBe('A');
    expect(link.getAttribute('href')).toBe('https://github.com/settings/tokens?type=beta');

    fireEvent.click(screen.getByText('去平台设置补权限'));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
  });

  it('其它回写失败只留提示，不给「去平台设置」按钮', async () => {
    primeSyncableDetail();
    syncTask.mockRejectedValue(
      new ApiError(502, JSON.stringify({ detail: '平台暂时不可用' }), 'Bad Gateway'),
    );
    render(<ReviewResultDialog taskId="task-1" onClose={() => {}} onOpenSettings={vi.fn()} />);
    await triggerSync();

    await waitFor(() => expect(screen.getByText('回写失败')).toBeTruthy());
    expect(screen.getByText('平台暂时不可用')).toBeTruthy();
    expect(screen.queryByText('去平台设置补权限')).toBeNull();
  });
});
