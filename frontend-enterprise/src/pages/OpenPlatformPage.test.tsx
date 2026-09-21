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

  it('技能广场只要元信息，不带 MB 级的技能文件包', async () => {
    const user = userEvent.setup();
    const fetchMock = stubPlatformFetch();
    renderPlatform();

    await screen.findByText('小艾');
    await user.click(screen.getByRole('tab', { name: /技能广场/ }));

    const paths = requestedPaths(fetchMock);
    const skillRequest = paths.find((url) => url.includes('/api/enterprise/general-skills'));
    expect(skillRequest).toBeTruthy();
    expect(skillRequest).toContain('include_files=0');
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

  it('聚合计数接口失败时不显示假角标，加载完成后才出现本地数量', async () => {
    const user = userEvent.setup();
    stubPlatformFetch(); // gallery/counts 未拦截 -> 静默失败，无假角标
    renderPlatform();

    await screen.findByText('小艾');
    // 数字员工 tab 已加载 -> 本地角标 1；工具未访问且聚合计数不可用 -> 不显示
    expect(screen.getByRole('tab', { name: /数字员工广场/ }).textContent).toContain('1');
    expect(screen.getByRole('tab', { name: /工具广场/ }).textContent).not.toContain('0');

    await user.click(screen.getByRole('tab', { name: /工具广场/ }));
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /工具广场/ }).textContent).toContain('0');
    });
  });

  it('聚合计数接口一次填满所有 tab 角标，无需懒加载各模块', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/gallery/counts')) {
        return jsonResponse({ agents: 3, knowledge: 2, general_skills: 5, skills: 4, tools: 6 });
      }
      if (url.includes('/api/enterprise/agents/') && url.includes('/skills')) return jsonResponse([]);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent, galleryAgent]);
      return jsonResponse({});
    });
    vi.stubGlobal('fetch', fetchMock);
    renderPlatform();

    await screen.findByText('小艾');
    // 未访问的模块也直接显示聚合计数
    await waitFor(() => {
      expect(screen.getByRole('tab', { name: /工具广场/ }).textContent).toContain('6');
    });
    expect(
      screen.getByRole('tab', { name: /技能广场/ }).textContent,
    ).toContain('5');
    expect(screen.getByRole('tab', { name: /SOP 广场/ }).textContent).toContain('4');
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
