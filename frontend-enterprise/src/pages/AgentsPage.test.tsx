// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui/tooltip';
import {
  AGENT_ROSTER_REFRESH_EVENT,
  ENTERPRISE_AGENT_STORAGE_KEY,
} from '@/lib/agent-scope-storage';
import type { AgentProfileRead } from '@/types';

import AgentsPage from './AgentsPage';

const agent: AgentProfileRead = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  is_overall: false,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

beforeEach(() => {
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
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe('AgentsPage team scope compatibility', () => {
  it('renders gracefully when the stored scope is a team', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      return jsonResponse([]);
    }));

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <AgentsPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    // 团队作用域匹配不到任何员工：不高亮、不报错，员工列表照常渲染。
    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
  });
});

describe('AgentsPage roster refresh', () => {
  it('重新拉取列表 when another entry point creates an employee', async () => {
    // 员工列表是每个页面各自拉的（本页只在 mount 时拉一次）。创建入口在 App 侧边栏
    // 弹窗里，本页收不到任何信号 —— 修复前表现为「新建后看不到，手动刷新页面才出现」。
    const rows: AgentProfileRead[] = [agent];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse(rows);
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <AgentsPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
    const callsBefore = fetchMock.mock.calls.length;

    // 别处新建了一个员工并广播花名册变更
    rows.push({ ...agent, id: 'agent-2', name: '小新' });
    fireEvent(window, new Event(AGENT_ROSTER_REFRESH_EVENT));

    expect((await screen.findByText('小新')).textContent).toBeTruthy();
    expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it('不监听无关事件（切换作用域不触发列表重拉）', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <AgentsPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );
    await screen.findByText('小艾');
    const callsAfterLoad = fetchMock.mock.calls.length;

    fireEvent(
      window,
      new CustomEvent('ultrarag-enterprise-agent-scope-change', {
        detail: { agentId: 'agent-2' },
      }),
    );
    await waitFor(() => expect(fetchMock.mock.calls.length).toBe(callsAfterLoad));
  });
});

describe('AgentsPage 页面级 tab', () => {
  function renderAgentsPage() {
    return render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <AgentsPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );
  }

  it('默认展示数字员工 tab，可切到技能管理 tab 并出现创建技能入口', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      if (url.includes('/api/enterprise/general-skills')) return jsonResponse([]);
      return jsonResponse([]);
    }));
    renderAgentsPage();

    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
    // 默认 tab 只渲染员工区，不渲染技能面板
    expect(screen.queryByText('我创建的技能')).toBeNull();

    await userEvent.click(screen.getByRole('tab', { name: '技能管理' }));

    expect(await screen.findByText('我创建的技能')).toBeTruthy();
    // 创建技能入口出现在技能管理 tab 顶部
    expect(screen.getByRole('button', { name: /创建技能/ })).toBeTruthy();
    // 切回后员工区还在
    await userEvent.click(screen.getByRole('tab', { name: '数字员工' }));
    expect(await screen.findByText('小艾')).toBeTruthy();
  });

  it('hash 带 #skills 时默认落在技能管理 tab', async () => {
    window.location.hash = '#skills';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => jsonResponse([])));
    renderAgentsPage();

    expect(await screen.findByText('我创建的技能')).toBeTruthy();
    // hash 路径默认在技能 tab，数字员工区不渲染
    expect(screen.queryByText('小艾')).toBeNull();
    window.location.hash = '';
  });
});
