// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui/tooltip';
import { ENTERPRISE_AGENT_STORAGE_KEY } from '@/lib/agent-scope-storage';
import type { AgentProfileRead } from '@/types';

import DashboardPage from './DashboardPage';

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

describe('DashboardPage team scope compatibility', () => {
  it('never sends the team scope as an agent_id query param', async () => {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, 'team:team-1');
    const fetchedUrls: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      fetchedUrls.push(url);
      if (url.includes('/work-record')) {
        return jsonResponse({ reply_stats: { total: 0, today: 0, by_day: {} }, events: [] });
      }
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      if (url.includes('/api/enterprise/feedback/summary')) {
        return jsonResponse({ total_feedback: 0, up_count: 0, down_count: 0, bucket_counts: [] });
      }
      return jsonResponse([]);
    }));

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <DashboardPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
              isAdmin
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    // 团队作用域视为未选员工：页面回落到可用员工，正常渲染且不报 "Agent not found"。
    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
    await waitFor(() => expect(fetchedUrls.length).toBeGreaterThan(0));
    fetchedUrls.forEach((url) => {
      expect(url).not.toContain('agent_id=team');
      expect(url).not.toContain('team%3A');
      expect(url).not.toContain('/agents/team');
    });
  });
});

describe('DashboardPage hero metrics', () => {
  function tool(id: string, patch: Record<string, unknown>) {
    return {
      id,
      tenant_id: 'tenant_demo',
      name: id,
      bucket: '默认',
      tool_type: 'http',
      method: 'GET',
      url: 'https://example.com',
      headers: {},
      auth: {},
      mcp_config: {},
      input_schema: {},
      output_schema: {},
      allowed_skills: [],
      enabled: true,
      created_at: '2026-08-01T00:00:00Z',
      updated_at: '2026-08-01T00:00:00Z',
      ...patch,
    };
  }

  function heroMetricText(label: string): string | undefined {
    // tab 行里也有同名文案，只取 HeroMetric（内含 <strong> 数字）那个
    return screen
      .getAllByText(label)
      .map((element) => element.parentElement)
      .find((element) => element?.querySelector('strong'))
      ?.textContent ?? undefined;
  }

  it('counts only tools that are bound active and enabled', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/work-record')) {
        return jsonResponse({ reply_stats: { total: 0, today: 0, by_day: {} }, events: [] });
      }
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      if (url.includes('/api/enterprise/tools')) {
        return jsonResponse([
          tool('tool-active', { binding_status: 'active', enabled: true }),
          // 员工侧停用：不统计
          tool('tool-inactive', { binding_status: 'inactive', enabled: true }),
          // 工具本身停用：不统计
          tool('tool-disabled', { binding_status: 'active', enabled: false }),
          // 广场里可见但没绑定到该员工：不统计
          tool('tool-unbound', { binding_status: null, enabled: true }),
        ]);
      }
      if (url.includes('/api/enterprise/feedback/summary')) {
        return jsonResponse({ total_feedback: 0, up_count: 0, down_count: 0, bucket_counts: [] });
      }
      return jsonResponse([]);
    }));

    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <DashboardPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
              isAdmin
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
    await waitFor(() => expect(heroMetricText('工具')).toBe('1工具'));
  });
});

describe('DashboardPage dispatcher profile tabs', () => {
  /**
   * 真实反馈：「他的档案页面没法查看对话日志和定时任务」。
   *
   * 根因是 DispatcherProfileView 之前**没有页内 tab**，只按路由传进来的
   * profileTab 决定渲染什么 —— 停在 /enterprise/dashboard 时恒为 work，
   * 页面上没有任何入口能切到另外两块。这条测试把「页内 tab 存在且可切换」钉住。
   */
  const dispatcher = {
    id: 'agent_disp',
    tenant_id: 'tenant_demo',
    name: '测试Multi',
    description: '我是智能工作台，将问题转接给对应的专家',
    kind: 'dispatcher',
    status: 'active',
    model_config_id: 'model-1',
    model_name: 'deepseek-v4-flash',
    usage_count: 3,
    published_to_gallery: false,
    members: [
      {
        member_agent_id: 'agent-1',
        routing_rule: '安全、健康、环境相关的问题',
        example_queries: [],
        sort_order: 0,
        status: 'active',
        agent_name: '小安',
        agent_status: 'active',
        free_model_enabled: false,
        has_own_model: false,
      },
    ],
    config: {
      fallback_mode: 'reply',
      fallback_reply: '换个说法再试一次？',
      default_member_agent_id: null,
      offer_candidates: true,
      min_confidence: 0.5,
    },
  };

  function renderDispatcherProfile() {
    window.localStorage.setItem(ENTERPRISE_AGENT_STORAGE_KEY, dispatcher.id);
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/enterprise/dispatchers')) return jsonResponse([dispatcher]);
        if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
        // knowledge-bases 是分页包，页面读的是 `.items`；回落成裸数组会让
        // `knowledgeBases.filter` 抛错、整棵树被卸载（tab 也跟着没了）。
        if (url.includes('/api/enterprise/knowledge-bases')) {
          return jsonResponse({ items: [], total: 0, page: 1, page_size: 200 });
        }
        if (url.includes('/api/enterprise/feedback/summary')) {
          return jsonResponse({ total_feedback: 0, up_count: 0, down_count: 0, bucket_counts: [] });
        }
        return jsonResponse([]);
      }),
    );
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <DashboardPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
              isAdmin
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );
  }

  it('档案页提供页内 tab，可切到定时任务与对话日志', async () => {
    renderDispatcherProfile();

    // 三个 tab 都在：档案总览 / 定时任务 / 对话日志
    const tablist = await screen.findByLabelText('多智能体档案分区');
    expect(tablist.textContent).toContain('档案总览');
    expect(tablist.textContent).toContain('定时任务');
    expect(tablist.textContent).toContain('对话日志');

    // 多智能体不该出现它根本不配的四块
    expect(tablist.textContent).not.toContain('知识库');
    expect(tablist.textContent).not.toContain('SOP');
    expect(tablist.textContent).not.toContain('记忆');

    // 默认落在档案总览：成员与路由规则可见
    expect(await screen.findByText(/成员与路由规则/)).toBeTruthy();
    expect(screen.getByText('小安')).toBeTruthy();

    // 点「定时任务」→ 真的渲染任务面板（不再是档案总览）。
    // Radix Tabs 的 Trigger 靠 mousedown 激活，fireEvent.click 不够，必须 userEvent。
    const user = userEvent.setup();
    await user.click(within(tablist).getByText('定时任务'));
    // 空任务表 → 用空态文案断言（桌面表格 + 移动卡片各渲染一份，用 findAll）
    expect((await screen.findAllByText('暂无定时任务')).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.queryByText(/成员与路由规则/)).toBeNull());

    // 点「对话日志」→ 渲染对话日志面板
    await user.click(within(tablist).getByText('对话日志'));
    expect(await screen.findByText('对话用户')).toBeTruthy();

    // 切回档案总览仍然可用
    await user.click(within(tablist).getByText('档案总览'));
    expect(await screen.findByText(/成员与路由规则/)).toBeTruthy();
  });
});
