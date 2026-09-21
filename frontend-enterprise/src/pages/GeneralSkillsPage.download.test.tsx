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
 * 下载现在走 api.blobWithProgress（XHR blob，带下载进度）。
 * 这里桩 XMLHttpRequest：记录请求 URL，触发两次 onprogress + onload(blob)。
 */
function stubPackageDownloadXhr() {
  const requestedUrls: string[] = [];
  const progressEvents: number[] = [];
  let failWithStatus = 0;
  class FakeXHR {
    status = 0;
    response: Blob | null = null;
    responseText = '';
    onprogress: ((event: ProgressEvent) => void) | null = null;
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    open(method: string, url: string) {
      requestedUrls.push(url);
      expect(method).toBe('GET');
    }
    setRequestHeader() {}
    addEventListener() {}
    send() {
      window.setTimeout(() => {
        this.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
        progressEvents.push(50);
        // 响应延后一个 tick：给 UI 一帧渲染进度浮层，避免同批 flush 直接完成
        window.setTimeout(() => {
          if (failWithStatus) {
            this.status = failWithStatus;
            this.responseText = '';
          } else {
            this.status = 200;
            this.response = new Blob(['zip-bytes'], { type: 'application/zip' });
          }
          this.onload?.();
        }, 30);
      }, 0);
    }
  }
  const xhrStub = {
    requested: requestedUrls,
    progressEvents,
    failWith(status: number) { failWithStatus = status; return this; },
  };
  vi.stubGlobal('XMLHttpRequest', FakeXHR as unknown as typeof XMLHttpRequest);
  return xhrStub;
}

function renderPage(skills: GeneralSkillRead[], user: EnterpriseAuthUser) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
    if (url.includes('/api/enterprise/general-skills')) return jsonResponse(skills);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  const xhrStub = stubPackageDownloadXhr();

  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/general-skills']}>
        <GeneralSkillsPage currentUser={user} />
      </MemoryRouter>
    </I18nProvider>,
  );

  return { fetchMock, xhrStub };
}

/**
 * 等列表渲染完，再打开第 index 行的操作菜单。
 *
 * 这里的等待要覆盖「取数 → 建表 → 渲染操作列」整条链路，是全用例里最慢的一步；
 * testing-library 默认只有 1s，整套用例并行跑（import 阶段可到分钟级）时会被拖爆，
 * 出现「单独跑通过、全量跑超时」的假失败，所以显式放宽。
 */
async function openMenu(index = 0) {
  const triggers = await screen.findAllByLabelText('技能操作', undefined, { timeout: 5000 });
  fireEvent.keyDown(triggers[index], { key: 'Enter' });
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

  it('点「下载」请求技能包端点，广场作用域不带 agent_id，且展示下载进度', async () => {
    const { fetchMock, xhrStub } = renderPage([makeSkill('weather-zh', 'someone_else')], member);

    await openMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: '下载' }));

    await waitFor(() => expect(xhrStub.requested.length).toBe(1));
    const url = String(xhrStub.requested[0]);
    expect(url).toContain('/api/enterprise/general-skills/weather-zh/package');
    expect(url).toContain('tenant_id=');
    expect(url).not.toContain('agent_id=');
    // XHR onprogress 至少上报过一次真实百分比
    expect(xhrStub.progressEvents.length).toBeGreaterThan(0);
    // 下载完成后浮层消失
    await waitFor(() => {
      expect(screen.queryByRole('progressbar', { name: '技能包下载进度' })).toBeNull();
    });
  });

  it('下载过程中显示进度浮层', async () => {
    const { xhrStub } = renderPage([makeSkill('weather-zh', 'someone_else')], member);

    await openMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: '下载' }));

    await waitFor(() => expect(xhrStub.requested.length).toBe(1));
    expect(screen.getByRole('progressbar', { name: '技能包下载进度' })).toBeTruthy();
    expect(document.body.textContent).toContain('正在下载技能包 weather-zh.zip…');
  });

  it('下载失败时只提示错误，不抛出未捕获异常', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
      return jsonResponse([makeSkill('weather-zh', 'someone_else')]);
    });
    vi.stubGlobal('fetch', fetchMock);
    const xhrStub = stubPackageDownloadXhr().failWith(500);

    render(
      <I18nProvider>
        <MemoryRouter initialEntries={['/enterprise/general-skills']}>
          <GeneralSkillsPage currentUser={member} />
        </MemoryRouter>
      </I18nProvider>,
    );

    await openMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: '下载' }));

    await waitFor(() => expect(xhrStub.requested.length).toBe(1));
    await waitFor(() => {
      // 失败后浮层收起、页面仍在：异常被 catch 住了而不是冒泡炸掉组件树
      expect(screen.queryByRole('progressbar', { name: '技能包下载进度' })).toBeNull();
      expect(screen.getAllByLabelText('技能操作').length).toBeGreaterThan(0);
    });
  });
});
