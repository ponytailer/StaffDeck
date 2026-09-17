// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { EnterpriseAuthUser } from '@/auth';
import type { AgentProfileRead } from '@/types';

import OpenPlatformPage from './OpenPlatformPage';

const admin: EnterpriseAuthUser = {
  id: 'user-admin',
  username: 'admin',
  tenant_id: 'tenant_demo',
  role: 'admin',
};

const overallAgent = {
  id: 'agent_tenant_demo_overall',
  tenant_id: 'tenant_demo',
  name: '整体智能体',
  is_overall: true,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
} as unknown as AgentProfileRead;

const galleryAgent = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  description: '客服数字员工',
  is_overall: false,
  status: 'active',
  metadata: { published_to_gallery: true },
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
} as unknown as AgentProfileRead;

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubPlatformFetch() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/enterprise/agents/') && url.includes('/skills')) return jsonResponse([]);
    if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent, galleryAgent]);
    if (url.includes('/api/enterprise/knowledge-bases')) {
      return jsonResponse([{
        id: 'kb-1',
        tenant_id: 'tenant_demo',
        name: '产品知识库',
        description: '产品资料',
        status: 'active',
        document_count: 3,
        bucket_count: 2,
        chunk_count: 10,
      }]);
    }
    if (url.includes('/api/enterprise/general-skills')) return jsonResponse([]);
    if (url.includes('/api/enterprise/tools')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function requestedPaths(fetchMock: ReturnType<typeof stubPlatformFetch>): string[] {
  return fetchMock.mock.calls.map((call) => String(call[0]));
}

function renderPlatform(initialPath = '/enterprise/platform/agents', currentUser = admin) {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[initialPath]}>
        <Routes>
          <Route
            path="/enterprise/platform/:kind"
            element={<OpenPlatformPage currentUser={currentUser} isAdmin />}
          />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('OpenPlatformPage 懒加载', () => {
  it('进入页面只拉当前模块，不会把 5 个接口全打一遍', async () => {
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    // 数字员工 tab 渲染出来即代表这批请求已经发完
    expect(await screen.findByText('小艾')).toBeTruthy();

    const paths = requestedPaths(fetchMock);
    expect(paths.some((url) => url.includes('/api/enterprise/agents'))).toBe(true);
    expect(paths.some((url) => url.includes('/api/enterprise/knowledge-bases'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/general-skills'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/tools'))).toBe(false);
  });

  it('切到知识库 tab 时才拉知识库接口，其余模块仍不拉', async () => {
    const user = userEvent.setup();
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    await screen.findByText('小艾');
    await user.click(screen.getByRole('tab', { name: /知识库广场/ }));

    expect(await screen.findByText('产品知识库')).toBeTruthy();

    const paths = requestedPaths(fetchMock);
    expect(paths.filter((url) => url.includes('/api/enterprise/knowledge-bases'))).toHaveLength(1);
    expect(paths.some((url) => url.includes('/api/enterprise/general-skills'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/tools'))).toBe(false);
    // 广场资源要挂在 overall 宿主下取广场作用域
    expect(paths.some((url) => url.includes('agent_id=agent_tenant_demo_overall'))).toBe(true);
  });

  it('来回切 tab 不会重复请求已加载过的模块', async () => {
    const user = userEvent.setup();
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    await screen.findByText('小艾');
    await user.click(screen.getByRole('tab', { name: /知识库广场/ }));
    await screen.findByText('产品知识库');
    await user.click(screen.getByRole('tab', { name: /数字员工广场/ }));
    await user.click(screen.getByRole('tab', { name: /知识库广场/ }));
    await screen.findByText('产品知识库');

    const paths = requestedPaths(fetchMock);
    expect(paths.filter((url) => url.includes('/api/enterprise/agents'))).toHaveLength(1);
    expect(paths.filter((url) => url.includes('/api/enterprise/knowledge-bases'))).toHaveLength(1);
  });

  it('未访问过的模块不显示数量角标，加载完成后才出现', async () => {
    const user = userEvent.setup();
    stubPlatformFetch();
    renderPlatform();

    await screen.findByText('小艾');
    // 数字员工 tab 已加载 -> 角标 1；其余模块未知 -> 不显示
    expect(screen.getByRole('tab', { name: /数字员工广场/ }).textContent).toContain('1');
    expect(screen.getByRole('tab', { name: /工具广场/ }).textContent).not.toContain('0');

    await user.click(screen.getByRole('tab', { name: /工具广场/ }));
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /工具广场/ }).textContent).toContain('0');
    });
  });

  it('深链到某个模块时只拉它自己', async () => {
    const fetchMock = stubPlatformFetch();
    renderPlatform('/enterprise/platform/knowledge');

    expect(await screen.findByText('产品知识库')).toBeTruthy();

    const paths = requestedPaths(fetchMock);
    expect(paths.some((url) => url.includes('/api/enterprise/knowledge-bases'))).toBe(true);
    expect(paths.some((url) => url.includes('/api/enterprise/general-skills'))).toBe(false);
    expect(paths.some((url) => url.includes('/api/enterprise/tools'))).toBe(false);
  });
});
