// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '@/auth';
import { I18nProvider } from '@/i18n';
import type { AgentProfileRead, GeneralSkillRead } from '@/types';

import GeneralSkillsPage from './GeneralSkillsPage';

const overallAgent: AgentProfileRead = {
  id: 'agent_overall',
  tenant_id: 'tenant_demo',
  name: '整体智能体',
  is_overall: true,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const member: EnterpriseAuthUser = {
  id: 'user_member',
  tenant_id: 'tenant_demo',
  username: 'member',
  role: 'member',
};

const admin: EnterpriseAuthUser = { ...member, id: 'user_admin', username: 'admin', role: 'admin' };

function makeSkill(slug: string, ownerUserId?: string): GeneralSkillRead {
  return {
    id: `skill-${slug}`,
    tenant_id: 'tenant_demo',
    slug,
    name: `${slug} 技能`,
    description: 'demo',
    capability_scope: 'general',
    skill_markdown: '# demo',
    skill_files: [],
    metadata: ownerUserId ? { owner_user_id: ownerUserId } : {},
    status: 'published',
    permissions: {},
    runtime_config: {},
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-10T00:00:00Z',
  };
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

/**
 * 用全局 fetch 桩驱动页面（与 MyCreatedSkillsPanel.test.tsx 同款手法）。
 * 列表走 JSON，技能包下载走 blob——断言点就是「有没有打到 /package 这个 URL」。
 */
function renderPage(skills: GeneralSkillRead[], user: EnterpriseAuthUser) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
    if (url.includes('/api/enterprise/general-skills') && url.includes('/package')) {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        blob: async () => new Blob(['zip-bytes'], { type: 'application/zip' }),
      } as unknown as Response;
    }
    if (url.includes('/api/enterprise/general-skills')) return jsonResponse(skills);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);

  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/general-skills']}>
        <GeneralSkillsPage currentUser={user} />
      </MemoryRouter>
    </I18nProvider>,
  );

  return fetchMock;
}

/** 等列表渲染完，再打开第 index 行的操作菜单。 */
async function openMenu(index = 0) {
  const triggers = await screen.findAllByLabelText('技能操作');
  fireEvent.keyDown(triggers[index], { key: 'Enter' });
}

function packageCalls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes('/package'));
}

beforeEach(() => {
  window.localStorage.clear();
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
  // jsdom 没有 object URL 实现，下载链路需要它
  if (!window.URL.createObjectURL) window.URL.createObjectURL = () => 'blob:mock';
  if (!window.URL.revokeObjectURL) window.URL.revokeObjectURL = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('GeneralSkillsPage 技能包下载', () => {
  it('广场技能对非管理员仍渲染操作菜单，但菜单里只有「下载」', async () => {
    // 关键行为变化：以前这里整体返回 null，非管理员看不到任何操作入口
    renderPage([makeSkill('weather-zh', 'someone_else')], member);

    await openMenu();

    expect(await screen.findByRole('menuitem', { name: '下载' })).toBeTruthy();
    expect(screen.queryByRole('menuitem', { name: '编辑' })).toBeNull();
    expect(screen.queryByRole('menuitem', { name: '删除' })).toBeNull();
  });

  it('管理员看到「下载」与「编辑」「删除」', async () => {
    renderPage([makeSkill('weather-zh', 'someone_else')], admin);

    await openMenu();

    expect(await screen.findByRole('menuitem', { name: '下载' })).toBeTruthy();
    expect(screen.getByRole('menuitem', { name: '编辑' })).toBeTruthy();
    expect(screen.getByRole('menuitem', { name: '删除' })).toBeTruthy();
  });

  it('创建者本人也能看到管理项', async () => {
    renderPage([makeSkill('weather-zh', 'user_member')], member);

    await openMenu();

    expect(await screen.findByRole('menuitem', { name: '下载' })).toBeTruthy();
    expect(screen.getByRole('menuitem', { name: '编辑' })).toBeTruthy();
  });

  it('点「下载」请求技能包端点，广场作用域不带 agent_id', async () => {
    const fetchMock = renderPage([makeSkill('weather-zh', 'someone_else')], member);

    await openMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: '下载' }));

    await waitFor(() => expect(packageCalls(fetchMock).length).toBe(1));
    const url = String(packageCalls(fetchMock)[0][0]);
    expect(url).toContain('/api/enterprise/general-skills/weather-zh/package');
    expect(url).toContain('tenant_id=');
    expect(url).not.toContain('agent_id=');
  });

  it('下载失败时只提示错误，不抛出未捕获异常', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
      if (url.includes('/package')) {
        return { ok: false, status: 500, statusText: 'Server Error', text: async () => '' } as Response;
      }
      return jsonResponse([makeSkill('weather-zh', 'someone_else')]);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/enterprise/general-skills']}>
          <GeneralSkillsPage currentUser={member} />
        </MemoryRouter>
      </I18nProvider>,
    );

    await openMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: '下载' }));

    await waitFor(() => expect(packageCalls(fetchMock).length).toBe(1));
    // 页面仍在，说明异常被 catch 住了而不是冒泡炸掉组件树
    expect(screen.getAllByLabelText('技能操作').length).toBeGreaterThan(0);
  });
});
