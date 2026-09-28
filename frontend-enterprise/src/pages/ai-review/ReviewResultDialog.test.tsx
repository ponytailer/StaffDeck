// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AiReviewTaskDetail } from '../../api/aiReview';
import ReviewResultDialog from './ReviewResultDialog';

const fetchDetail = vi.fn();

vi.mock('../../api/aiReview', () => ({
  fetchAiReviewTaskDetail: (...args: unknown[]) => fetchDetail(...args),
  syncAiReviewTaskToPlatform: vi.fn(),
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
