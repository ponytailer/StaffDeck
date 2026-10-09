// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlazaHomePage from './PlazaHomePage';

// 组件内通过 api client 拉新闻与数字员工候选——测试里 mock 掉，不真发请求。
const apiGet = vi.fn();
vi.mock('@/api/client', () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
  TENANT_ID: 'tenant_demo',
}));

const NEWS_PAYLOAD = {
  date: '2026-10-09',
  provenance: 'rss',
  items: {
    ai: [
      { category: 'ai', title: 'AI 新闻一', summary: 's', source: 'A', url: 'https://a.com/1', published_at: '10-09' },
      { category: 'ai', title: 'AI 新闻二', summary: 's', source: 'B', url: 'https://a.com/2', published_at: '10-09' },
      { category: 'ai', title: 'AI 新闻三', summary: 's', source: 'C', url: 'https://a.com/3', published_at: '10-09' },
    ],
    travel: [
      { category: 'travel', title: '旅文新闻一', summary: 's', source: 'D', url: 'https://t.com/1', published_at: '10-09' },
      { category: 'travel', title: '旅文新闻二', summary: 's', source: 'E', url: 'https://t.com/2', published_at: '10-09' },
      { category: 'travel', title: '旅文新闻三', summary: 's', source: 'F', url: 'https://t.com/3', published_at: '10-09' },
    ],
  },
};

function renderHome() {
  return render(
    <MemoryRouter>
      <PlazaHomePage onUseEmployee={vi.fn()} onOpenAgentApp={vi.fn()} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  apiGet.mockResolvedValue(NEWS_PAYLOAD);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('PlazaHomePage', () => {
  it('默认只读：渲染默认 Agent 应用卡片与新闻两组各 3 条，卡片不可拖（无删除按钮）', async () => {
    renderHome();
    expect(await screen.findByText('AI 新闻一')).toBeTruthy();
    expect(screen.getByText('旅文新闻三')).toBeTruthy();
    // 默认布局来自本地清单前三个 Agent 应用
    expect(screen.getByText('AI PPT Studio')).toBeTruthy();
    // 只读模式没有删除按钮与添加按钮
    expect(screen.queryByText('添加快捷方式')).toBeNull();
    expect(screen.queryAllByLabelText(/移除/)).toHaveLength(0);
    // 新闻请求发出
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
  });

  it('编辑→保存：可移除卡片，保存写入 localStorage 并回到只读', async () => {
    renderHome();
    fireEvent.click(screen.getByText('✎ 编辑布局'));
    // 编辑模式出现添加/重置按钮和每张卡的删除按钮
    expect(screen.getByText('添加快捷方式')).toBeTruthy();
    const delButtons = screen.getAllByLabelText(/移除/);
    expect(delButtons.length).toBe(3);
    fireEvent.click(delButtons[0]);
    expect(screen.getAllByLabelText(/移除/)).toHaveLength(2);

    fireEvent.click(screen.getByText('保存布局'));
    expect(screen.queryByText('保存布局')).toBeNull();
    const stored = JSON.parse(window.localStorage.getItem('ultrarag_plaza_home_layout_v1') || 'null');
    expect(stored.chips).toHaveLength(2);
    // 保存后回到只读：删除按钮消失
    expect(screen.queryAllByLabelText(/移除/)).toHaveLength(0);
  });

  it('编辑→取消：恢复进入编辑前的布局，不写 localStorage', () => {
    renderHome();
    fireEvent.click(screen.getByText('✎ 编辑布局'));
    fireEvent.click(screen.getAllByLabelText(/移除/)[0]);
    fireEvent.click(screen.getByText('取消'));
    expect(screen.getAllByText(/AI PPT Studio/).length).toBeGreaterThan(0);
    expect(window.localStorage.getItem('ultrarag_plaza_home_layout_v1')).toBeNull();
  });
});
