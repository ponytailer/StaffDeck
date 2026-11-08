// @vitest-environment jsdom

/**
 * 员工广场的「我的小队」tab。
 *
 * 这个 tab 是用户要求的入口（截图里三个 tab 少了小队）：员工广场是「能聊的对象」
 * 总览，小队也是可聊对象，但**代码路径完全不同** —— 它不在 /api/enterprise/agents
 * 里、进对话前必须先切作用域。这里的断言就钉这两件事。
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { ENTERPRISE_AGENT_STORAGE_KEY } from '@/lib/agent-scope-storage';
import type { EnterpriseAuthUser } from '@/auth';
import type { DispatcherRead } from '@/lib/dispatcher';
import type { AgentProfileRead } from '@/types';

import EmployeeGalleryPage from './EmployeeGalleryPage';

const currentUser: EnterpriseAuthUser = {
  id: 'user-1',
  username: 'me',
  tenant_id: 'tenant_demo',
  role: 'member',
};

const agents: AgentProfileRead[] = [
  {
    id: 'agent-1',
    tenant_id: 'tenant_demo',
    name: '小艾',
    is_overall: false,
    status: 'active',
    metadata: { owner_user_id: 'user-1' },
    resources: [],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
  },
];

function squadRow(overrides: Record<string, unknown> = {}): DispatcherRead {
  return {
    id: 'agent_disp_1',
    tenant_id: 'tenant_demo',
    name: '人事服务台',
    description: '入职、考勤、薪资都转这里',
    kind: 'dispatcher',
    status: 'active',
    model_config_id: 'model-1',
    model_name: 'deepseek-v4-flash',
    usage_count: 12,
    published_to_gallery: true,
    config: {
      fallback_mode: 'clarify',
      fallback_reply: '',
      default_member_agent_id: null,
      offer_candidates: true,
      min_confidence: 0.6,
    },
    members: [
      {
        member_agent_id: 'agent-a',
        agent_name: '入职小助手',
        routing_rule: '入职、离职',
        example_queries: [],
        sort_order: 0,
        status: 'active',
        agent_status: 'active',
        free_model_enabled: false,
        has_own_model: false,
      },
      {
        member_agent_id: 'agent-b',
        agent_name: 'IT 支持',
        routing_rule: '电脑、网络',
        example_queries: [],
        sort_order: 1,
        status: 'active',
        agent_status: 'active',
        free_model_enabled: false,
        has_own_model: false,
      },
    ],
    ...overrides,
  } as unknown as DispatcherRead;
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubFetch(squads: DispatcherRead[] = [squadRow()]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/enterprise/dispatchers')) return jsonResponse(squads);
    if (url.includes('/api/enterprise/agents')) return jsonResponse(agents);
    if (url.includes('/api/enterprise/general-skills')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="nav-target">{location.pathname}</div>;
}

function renderGallery() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/workspace/gallery']}>
        <Routes>
          <Route
            path="*"
            element={
              <>
                <EmployeeGalleryPage currentUser={currentUser} />
                <LocationEcho />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('员工广场「我的小队」tab', () => {
  it('tab 栏里有「我的小队」，切过去才请求 dispatchers', async () => {
    const fetchMock = stubFetch();
    const user = userEvent.setup();
    renderGallery();

    // 默认在「所有员工」：员工列表已拉完，但**不该**提前拉小队（懒加载）
    await screen.findByRole('tab', { name: '我的小队' }, { timeout: 4000 });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes('/api/enterprise/agents')),
      ).toBe(true),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes('/api/enterprise/dispatchers')),
      ).toBe(false),
    );

    await user.click(screen.getByRole('tab', { name: '我的小队' }));

    // 小队卡按小队口径渲染：成员数 / 被使用 / 兜底
    expect(await screen.findByText('人事服务台', undefined, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByText(/员工小队 · 2\/3 位同事/)).toBeTruthy();
    expect(screen.getAllByText('12').length).toBeGreaterThan(0);
    expect(screen.getByText('反问')).toBeTruthy();
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes('/api/enterprise/dispatchers')),
    ).toBe(true);
  });

  it('点「去对话」先切作用域再进对话（否则对话页会把小队当员工兜底掉）', async () => {
    stubFetch();
    const user = userEvent.setup();
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '我的小队' }, { timeout: 4000 }));
    const card = await screen.findByRole('button', { name: /员工小队 人事服务台/ }, { timeout: 4000 });

    await user.click(card);

    // 作用域已落库 = 小队 id
    await waitFor(() =>
      expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('agent_disp_1'),
    );
    await waitFor(() =>
      expect(screen.getByTestId('nav-target').textContent).toBe('/workspace/chat/draft/agent_disp_1'),
    );
  });

  it('点「档案」切作用域并进员工档案页', async () => {
    stubFetch();
    const user = userEvent.setup();
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '我的小队' }, { timeout: 4000 }));
    await user.click(await screen.findByRole('button', { name: '档案' }, { timeout: 4000 }));

    await waitFor(() =>
      expect(screen.getByTestId('nav-target').textContent).toBe('/enterprise/dashboard'),
    );
    expect(window.localStorage.getItem(ENTERPRISE_AGENT_STORAGE_KEY)).toBe('agent_disp_1');
  });

  it('没有小队时给「去我的小队」引导', async () => {
    stubFetch([]);
    const user = userEvent.setup();
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '我的小队' }, { timeout: 4000 }));

    expect(await screen.findByText('还没有员工小队', undefined, { timeout: 4000 })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: '去我的小队' }));
    await waitFor(() =>
      expect(screen.getByTestId('nav-target').textContent).toBe('/enterprise/squads'),
    );
  });

  it('搜索框也过滤小队（按名字 / 成员名）', async () => {
    stubFetch([squadRow(), squadRow({ id: 'agent_disp_2', name: '财务报销台' })]);
    const user = userEvent.setup();
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '我的小队' }, { timeout: 4000 }));
    expect(await screen.findByText('财务报销台', undefined, { timeout: 4000 })).toBeTruthy();

    await user.type(screen.getByLabelText('搜索数字员工'), 'IT 支持');
    await waitFor(() => {
      // 两个小队都含「IT 支持」这个成员，都留下；换成只命中一个的关键词
      expect(screen.getAllByText(/员工小队 · 2\/3 位同事/).length).toBe(2);
    });

    await user.clear(screen.getByLabelText('搜索数字员工'));
    await user.type(screen.getByLabelText('搜索数字员工'), '财务');
    await waitFor(() => expect(screen.queryByText('人事服务台')).toBeNull());
    expect(screen.getByText('财务报销台')).toBeTruthy();
  });
});
