// @vitest-environment jsdom

import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { SidebarProvider } from '@/components/ui/sidebar';
import { TooltipProvider } from '@/components/ui/tooltip';
import type { AgentProfileRead } from '@/types';
import type { DispatcherRead } from '@/lib/dispatcher';

import AppSidebar from './AppSidebar';

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

const dispatcher: DispatcherRead = {
  id: 'dispatcher-1',
  tenant_id: 'tenant_demo',
  name: '测试Multi',
  description: '把问题分给最合适的同事',
  kind: 'dispatcher',
  status: 'active',
  model_name: 'deepseek-v4.1-flash',
  members: [
    {
      member_agent_id: 'agent-1',
      routing_rule: '增长相关的问题转给他',
      example_queries: ['怎么拉新'],
      sort_order: 0,
      status: 'active',
      agent_name: '小艾',
      agent_status: 'active',
      free_model_enabled: false,
      has_own_model: false,
    },
  ],
  config: {
    fallback_mode: 'clarify',
    fallback_reply: '',
    offer_candidates: true,
    min_confidence: 0.5,
  },
  usage_count: 3,
  published_to_gallery: false,
};

function stubRadixPointerApis() {
  if (!window.ResizeObserver) {
    window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
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
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
}

function renderSidebar(props: {
  selectedAgentId: string;
  onSelectAgent?: (value: string) => void;
}) {
  return render(
    <I18nProvider>
      <TooltipProvider>
        <SidebarProvider>
          <AppSidebar
            selected="/enterprise/dashboard"
            onNavigate={() => {}}
            isAdmin={false}
            scopeAgents={[agent]}
            selectedAgentId={props.selectedAgentId}
            onSelectAgent={props.onSelectAgent ?? (() => {})}
            onOpenChat={() => {}}
          />
        </SidebarProvider>
      </TooltipProvider>
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

beforeEach(() => {
  stubRadixPointerApis();
});

describe('AppSidebar dispatcher scope', () => {
  function renderDispatcherSidebar() {
    return render(
      <I18nProvider>
        <TooltipProvider>
          <SidebarProvider>
            <AppSidebar
              selected="/enterprise/dashboard"
              onNavigate={() => {}}
              isAdmin={false}
              sidebarDispatcher={dispatcher}
              scopeAgents={[agent]}
              scopeDispatchers={[dispatcher]}
              selectedAgentId="dispatcher-1"
              onSelectAgent={() => {}}
              onOpenChat={() => {}}
            />
          </SidebarProvider>
        </TooltipProvider>
      </I18nProvider>,
    );
  }

  it('当前作用域是员工小队时显示它的名字，而不是「未选择 / -」', () => {
    renderDispatcherSidebar();

    const trigger = screen.getByLabelText('切换当前员工');
    expect(within(trigger).getByText('员工小队')).toBeTruthy();
    expect(within(trigger).getByText('测试Multi')).toBeTruthy();
    expect(within(trigger).queryByText('未选择')).toBeNull();
    expect(within(trigger).queryByText('-')).toBeNull();
  });

  it('员工能力那组导航（知识库/技能/SOP/工具）对它不出现', () => {
    renderDispatcherSidebar();

    expect(screen.queryByText('知识库')).toBeNull();
    expect(screen.queryByText('技能')).toBeNull();
    expect(screen.queryByText('SOP')).toBeNull();
    expect(screen.queryByText('工具')).toBeNull();
    // 换成对员工小队成立的入口
    expect(screen.getByText('员工小队档案')).toBeTruthy();
    expect(screen.getByText('成员与路由规则')).toBeTruthy();
    // 员工档案这个名字也不该出现（它不是一个员工）
    expect(screen.queryByText('员工档案')).toBeNull();
  });

  it('切换器下拉里把员工小队单独分组，副标题是成员数而不是岗位名', async () => {
    const user = userEvent.setup();
    const onSelectAgent = vi.fn();
    render(
      <I18nProvider>
        <TooltipProvider>
          <SidebarProvider>
            <AppSidebar
              selected="/enterprise/dashboard"
              onNavigate={() => {}}
              isAdmin={false}
              sidebarDispatcher={dispatcher}
              scopeAgents={[agent]}
              scopeDispatchers={[dispatcher]}
              selectedAgentId="dispatcher-1"
              onSelectAgent={onSelectAgent}
              onOpenChat={() => {}}
            />
          </SidebarProvider>
        </TooltipProvider>
      </I18nProvider>,
    );

    await user.click(screen.getByLabelText('切换当前员工'));
    const menu = await screen.findByRole('menu');
    expect(within(menu).getByText('员工小队')).toBeTruthy();
    const item = within(menu)
      .getAllByRole('menuitem')
      .find((row) => row.textContent?.includes('测试Multi'));
    expect(item?.textContent).toContain('1/3 位同事');
    expect(item?.textContent).not.toContain('待补充岗位');
  });

  it('普通员工作用域下仍然是原来那套员工导航（回归）', () => {
    render(
      <I18nProvider>
        <TooltipProvider>
          <SidebarProvider>
            <AppSidebar
              selected="/enterprise/dashboard"
              onNavigate={() => {}}
              isAdmin={false}
              sidebarAgent={agent}
              scopeAgents={[agent]}
              selectedAgentId="agent-1"
              onSelectAgent={() => {}}
              onOpenChat={() => {}}
            />
          </SidebarProvider>
        </TooltipProvider>
      </I18nProvider>,
    );

    expect(screen.getByText('知识库')).toBeTruthy();
    expect(screen.getByText('SOP')).toBeTruthy();
    expect(screen.queryByText('成员与路由规则')).toBeNull();
  });
});

describe('AppSidebar chat variant group conversations', () => {
  const leaderAgent: AgentProfileRead = { ...agent, id: 'agent-tl', name: '队长' };
  function renderChatSidebar(
    handlers: { onOpenHome?: () => void; onOpenGallery?: () => void } = {},
  ) {
    const sessions = [
      {
        id: 'session-tl',
        tenant_id: 'tenant_demo',
        agent_id: 'agent-tl',
        team_id: 'team-1',
        title: '团队 增长团队 · TL 对话',
        status: 'active',
        updated_at: '2026-08-01T00:00:00Z',
      },
      {
        id: 'session-member',
        tenant_id: 'tenant_demo',
        agent_id: 'agent-1',
        title: '写公告',
        status: 'active',
        updated_at: '2026-08-01T00:00:00Z',
      },
    ];
    return render(
      <I18nProvider>
        <TooltipProvider>
          <SidebarProvider>
            <AppSidebar
              variant="chat"
              sessions={sessions}
              agents={[agent, leaderAgent]}
              sessionFilter="all"
              onSessionFilterChange={() => {}}
              sessionFilterOptions={[{ value: 'all', label: '全部员工 · 2' }]}
              isSessionUnread={() => false}
              onOpenSession={() => {}}
              onOpenHome={handlers.onOpenHome ?? (() => {})}
              onOpenGallery={handlers.onOpenGallery ?? (() => {})}
              onRenameSession={() => {}}
              onDeleteSession={() => {}}
              onOpenAdmin={() => {}}
            />
          </SidebarProvider>
        </TooltipProvider>
      </I18nProvider>,
    );
  }

  it('logo 点击回到站点首页，而不是数字员工广场（对话端）', async () => {
    const user = userEvent.setup();
    const onOpenHome = vi.fn();
    const onOpenGallery = vi.fn();
    renderChatSidebar({ onOpenHome, onOpenGallery });

    await user.click(screen.getByLabelText('返回首页'));

    expect(onOpenHome).toHaveBeenCalledTimes(1);
    expect(onOpenGallery).not.toHaveBeenCalled();
  });

  it('shows the employee name in a styled tooltip on avatar hover', async () => {
    const user = userEvent.setup();
    renderChatSidebar();

    const employeeRow = screen.getByText('写公告').closest('[role="button"]');
    const avatar = employeeRow?.querySelector('span');
    expect(avatar).toBeTruthy();
    await user.hover(avatar as Element);

    const tooltip = await screen.findByRole('tooltip');
    expect(tooltip.textContent).toContain('小艾');
  });
});

describe('AppSidebar primary navigation', () => {
  function renderManagementSidebar(
    onNavigate: (route: string) => void = () => {},
    { isAdmin = false }: { isAdmin?: boolean } = {},
  ) {
    return render(
      <I18nProvider>
        <TooltipProvider>
          <SidebarProvider>
            <AppSidebar
              selected="/enterprise/dashboard"
              onNavigate={onNavigate}
              isAdmin={isAdmin}
              scopeAgents={[agent]}
              selectedAgentId="agent-1"
              onSelectAgent={() => {}}
              onOpenChat={() => {}}
            />
          </SidebarProvider>
        </TooltipProvider>
      </I18nProvider>,
    );
  }

  it('「我的团队」已从侧边栏移除', () => {
    renderManagementSidebar();
    expect(screen.queryByText('我的团队')).toBeNull();
    expect(screen.queryByText('团队对话')).toBeNull();
  });

  it('决策助手不在侧边栏：入口已移入 Agent 广场清单', () => {
    renderManagementSidebar();
    expect(screen.queryByText('决策助手')).toBeNull();
  });

  it('AI CodeReviewer 不在侧边栏：入口已移入 Agent 广场清单（adminOnly 条目）', () => {
    renderManagementSidebar(undefined, { isAdmin: true });
    expect(screen.queryByText('AI CodeReviewer')).toBeNull();
  });

  it('账号管理不在侧边栏：已并入 API Key 审批页的用户管理 tab', () => {
    renderManagementSidebar(undefined, { isAdmin: false });
    expect(screen.queryByText('账号管理')).toBeNull();

    cleanup();
    renderManagementSidebar(undefined, { isAdmin: true });
    expect(screen.queryByText('账号管理')).toBeNull();
    expect(screen.getByText('API Key 审批')).toBeTruthy();
  });
});
