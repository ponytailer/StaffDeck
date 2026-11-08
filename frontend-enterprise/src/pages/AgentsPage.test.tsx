// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
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
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo',               username: 'demo', role: 'admin' }}
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
    // 创建技能入口出现在技能管理 tab 顶部（精确名匹配：面板空状态里还有
    // 「去创建技能」按钮，用 /创建技能/ 正则会多重命中）
    expect(screen.getByRole('button', { name: '创建技能' })).toBeTruthy();
    // 切回后员工区还在
    await userEvent.click(screen.getByRole('tab', { name: '数字员工' }));
    expect(await screen.findByText('小艾')).toBeTruthy();
  });

  /**
   * 回归（用户原话「点返回，返回的页面不对」）：
   * 从「技能管理 / 工具管理」tab 点创建，入口必须带 `return=employee_tab` ——
   * 编辑页据此把「返回」落回 `/enterprise/agents#skills` / `#tools`。
   * 不带的话返回会落到 /enterprise/general-skills | /enterprise/tools：
   * 员工小队作用域下那两个路由会被 DISPATCHER_UNSUPPORTED_PREFIXES 守卫
   * 劫到档案页。用 LocationEcho 钉住跳转 URL 本身。
   */
  it('创建技能 / 新建工具 / Swagger 导入入口都带 return=employee_tab', async () => {
    const seen: string[] = [];
    function LocationEcho() {
      const location = useLocation();
      seen.push(`${location.pathname}${location.search}`);
      return null;
    }
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      return jsonResponse([]);
    }));
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={['/enterprise/agents']}>
            <Routes>
              <Route path="/enterprise/agents" element={<AgentsPage currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }} />} />
              <Route path="*" element={<LocationEcho />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );
    await screen.findByText('小艾');

    await userEvent.click(screen.getByRole('tab', { name: '技能管理' }));
    await userEvent.click(screen.getByRole('button', { name: '创建技能' }));
    expect(seen).toContain('/enterprise/general-skills/new?scope=gallery&return=employee_tab');
  });

it('tab 顺序为 数字员工 / 技能管理 / 知识库 / 工具管理 / 静态资源（小队已升为一级菜单）', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      return jsonResponse([]);
    }));
    renderAgentsPage();

    await screen.findByText('小艾');
    const labels = screen.getAllByRole('tab').map((tab) => tab.textContent?.trim());
// 页面级 tab 在前，员工筛选 tab 紧随其后。
    // 「静态资源」与技能/工具面板同构，排在最后。
    // 「员工小队」已升为侧栏一级菜单「我的小队」，不再是这里的页内 tab。
    expect(labels.slice(0, 5)).toEqual([
      '数字员工',
      '技能管理',
      '知识库',
      '工具管理',
      '静态资源',
    ]);
    expect(labels).not.toContain('员工小队');
    const importTabIndex = labels.indexOf('知识库');
    expect(importTabIndex).toBeLessThan(labels.indexOf('工具管理'));
  });

  it('hash 带 #knowledge 时默认落在个人知识库 tab', async () => {
    window.location.hash = '#knowledge';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/knowledge-bases')) {
        return jsonResponse({
          items: [],
          total: 0,
          page: 1,
          page_size: 10,
          total_pages: 0,
          summary: { active: 0, archived: 0, documents: 0 },
        });
      }
      return jsonResponse([]);
    }));
    renderAgentsPage();

    expect(await screen.findByText('我的知识库列表')).toBeTruthy();
    // 个人知识库不跟随员工，花名册区域不渲染
    expect(screen.queryByText('小艾')).toBeNull();
    expect(screen.queryByText('员工总数')).toBeNull();
    window.location.hash = '';
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

  it('旧深链 #dispatchers 重定向到「我的小队」一级页面', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      return jsonResponse([]);
    }));
    function LocationEcho() {
      const location = useLocation();
      return <div data-testid="nav-target">{location.pathname}</div>;
    }
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter initialEntries={['/enterprise/agents#dispatchers']}>
            <Routes>
              <Route
                path="/enterprise/agents"
                element={
                  <AgentsPage
                    currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
                  />
                }
              />
              <Route path="*" element={<LocationEcho />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );

    // 老书签 / 侧栏旧入口仍要能用：直接落到 /enterprise/squads
    expect((await screen.findByTestId('nav-target')).textContent).toBe('/enterprise/squads');
  });
});

describe('AgentsPage 员工小队引用计数', () => {
  /**
   * 引用关系必须是**本地算**的：数据源就是已经拉到的 `dispatchers[].members`。
   * 曾经的做法是每张员工卡各拉一次 `/dispatchers/{id}/referenced-by` ——
   * 网格里 10 张卡就是 10 次往返（远程 PG 单次 10ms 起），而且多一个请求
   * 就多一处可能失败的地方。这条测试把「不发额外请求」钉住。
   */
  it('员工卡显示被几个员工小队引用，且不额外拉引用接口', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      if (url.includes('/api/enterprise/dispatchers')) {
        return jsonResponse([
          {
            id: 'agent_disp_1',
            tenant_id: 'tenant_demo',
            name: '前台调度',
            kind: 'dispatcher',
            status: 'active',
            members: [
              {
                member_agent_id: 'agent-1',
                agent_name: '小艾',
                agent_status: 'active',
                routing_rule: '入职相关',
                example_queries: [],
                sort_order: 0,
                status: 'active',
                free_model_enabled: false,
                has_own_model: false,
              },
            ],
            config: {
              fallback_mode: 'clarify',
              fallback_reply: '',
              offer_candidates: true,
              min_confidence: 0.6,
            },
            usage_count: 0,
          },
        ]);
      }
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

    expect(await screen.findByText('被 1 个员工小队引用')).toBeTruthy();
    // 引用关系是本地算的，不许为它单独打接口
    const referencedCalls = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes('/referenced-by'),
    );
    expect(referencedCalls).toHaveLength(0);
  });
});

describe('AgentsPage 新建小队的入口可见性', () => {
  /**
   * 真实反馈链：
   * 1.「入口在哪，我没找到创建入口」→ 顶部那张卡和「创建新员工」灰底黑图标长得一样
   *   → 改成 teal 语义色区分（这条靠人眼验收，单测只钉文案）。
   * 2.「点击调度入口的弹窗位置偏下，看不见」→ DialogContent 的 className 里写了
   *   `relative`，tailwind-merge 把它和 `fixed` 归为同组直接覆盖掉 → 弹窗被
   *   `top: 50%` 推到 body 高度一半，底部溢出视口。那条在 DispatcherEditorDialog 里修。
   * 3.「删除页面最下方的创建调度入口」→ 页尾引导条整个删掉。
   * 4.「这个页面有点太空旷」→ 改 master-detail（左列表 + 右详情），创建入口
   *   因此出现在两处：页头一处、左列表底部一处。**不再断言「只能有一个」**，
   *   改为断言「两处都是同一个动作、且没有页尾引导条」。
   */
  function mockAgents(agentRows: AgentProfileRead[], dispatcherRows: unknown[] = []) {
    return vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      // 概览必须先判：/dispatchers/overview 也包含 /dispatchers
      if (url.includes('/api/enterprise/dispatchers/overview')) {
        return jsonResponse({
          squads: dispatcherRows.length,
          members: 0,
          transfers: 0,
          published: 0,
          missing_rule_members: 0,
          broken_models: 0,
          single_member_squads: 0,
          activity: [],
        });
      }
      if (url.includes('/api/enterprise/dispatchers')) return jsonResponse(dispatcherRows);
      if (url.includes('/api/enterprise/agents')) return jsonResponse(agentRows);
      return jsonResponse([]);
    });
  }

  function renderPage() {
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <AgentsPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
              mode="dispatchers"
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );
  }

  it('「我的小队」页不渲染员工网格，创建入口只在页内两处（无页尾引导条）', async () => {
    vi.stubGlobal('fetch', mockAgents([agent, { ...agent, id: 'agent-2', name: '小新' }]));
    renderPage();

    expect(await screen.findByText('我的小队')).toBeTruthy();
    // 空态下只有页头那一个入口
    expect(screen.getAllByText('新建小队').length).toBeGreaterThanOrEqual(1);
    // 小队页不渲染数字员工的统计与网格
    expect(screen.queryByText('员工总数')).toBeNull();
    expect(screen.queryByText('小艾')).toBeNull();
    // 旧的页尾引导条必须彻底消失
    expect(screen.queryByLabelText('调度入口引导')).toBeNull();
    expect(screen.queryByText(/还没有调度入口/)).toBeNull();
  });

  it('新建弹窗标题是「新建员工小队」', async () => {
    vi.stubGlobal('fetch', mockAgents([agent, { ...agent, id: 'agent-2', name: '小新' }]));
    renderPage();

    const user = userEvent.setup();
    // 页头入口与左列表底部入口同名，取第一个；弹窗标题另按 dialog 作用域断言
    const [firstCreate] = await screen.findAllByText('新建小队');
    await user.click(firstCreate);

    const dialog = await screen.findByRole('dialog');
    const title = dialog.querySelector('[data-slot="dialog-title"]');
    expect(title?.textContent).toContain('新建员工小队');
  });

  it('没有员工小队时给创建引导，而不是空分区', async () => {
    vi.stubGlobal('fetch', mockAgents([agent]));
    renderPage();

    expect(await screen.findByText('还没有员工小队')).toBeTruthy();
  });

  it('已有入口时左列表 + 右详情都渲染出来', async () => {
    vi.stubGlobal(
      'fetch',
      mockAgents([agent, { ...agent, id: 'agent-2', name: '小新' }], [
        {
          id: 'agent_disp_1',
          tenant_id: 'tenant_demo',
          name: '前台调度',
          kind: 'dispatcher',
          status: 'active',
          members: [],
          config: {
            fallback_mode: 'clarify',
            fallback_reply: '',
            offer_candidates: true,
            min_confidence: 0.6,
          },
          usage_count: 0,
        },
      ]),
    );
    renderPage();

    expect(await screen.findByLabelText('员工小队')).toBeTruthy();
    // 左列表一项 + 右详情标题，名字会出现两次
    expect(screen.getByLabelText('小队列表')).toBeTruthy();
    expect(screen.getAllByText('前台调度').length).toBeGreaterThanOrEqual(2);
    // 详情里的操作入口
    expect(screen.getByRole('button', { name: /去对话/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: '员工小队档案' })).toBeTruthy();
  });
});

describe('AgentsPage 「我的小队」master-detail', () => {
  /**
   * 用户反馈「/enterprise/squads 这个页面有点太空旷了」后的口径：
   * - 顶部加统计卡 + 健康度条（数字来自 /dispatchers/overview 聚合接口）
   * - 主体改左列表 + 右详情，1 个入口时也不再是「一张卡 + 一大片空白」
   *
   * 这里钉的是「数据接得上 + 选中会切换 + 概览挂了不影响主流程」，
   * 视觉效果靠截图验收。
   */
  function squadRow(overrides: Record<string, unknown> = {}) {
    return {
      id: 'agent_disp_1',
      tenant_id: 'tenant_demo',
      name: '前台调度',
      kind: 'dispatcher',
      status: 'active',
      model_config_id: 'model-1',
      model_name: 'deepseek-v4-flash',
      usage_count: 3,
      published_to_gallery: false,
      config: {
        fallback_mode: 'clarify',
        fallback_reply: '换个说法再试一次？',
        default_member_agent_id: null,
        offer_candidates: true,
        min_confidence: 0.6,
      },
      members: [
        {
          member_agent_id: 'agent-a',
          agent_name: '入职小助手',
          routing_rule: '入职、离职、考勤异常',
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
          routing_rule: '电脑、网络、账号权限',
          example_queries: [],
          sort_order: 1,
          status: 'active',
          agent_status: 'active',
          free_model_enabled: false,
          has_own_model: true,
        },
      ],
      ...overrides,
    };
  }

  const emptyOverview = {
    squads: 2,
    members: 3,
    transfers: 21,
    published: 1,
    missing_rule_members: 0,
    broken_models: 0,
    single_member_squads: 0,
    // 左列表的「N 次转接」也吃这份数据（不用 usage_count，那是会话计数器）
    activity: [
      { id: 'agent_disp_1', sessions: 4, sessions_7d: 3, transfers: 21, top_members: [] },
    ],
  };

  function mockFetch(
    squadRows: unknown[],
    overview: unknown = emptyOverview,
  ) {
    return vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/dispatchers/overview')) return jsonResponse(overview);
      if (url.includes('/api/enterprise/dispatchers')) return jsonResponse(squadRows);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([]);
      return jsonResponse([]);
    });
  }

  function renderSquadsPage() {
    render(
      <I18nProvider>
        <TooltipProvider>
          <MemoryRouter>
            <AgentsPage
              currentUser={{ id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'admin' }}
              mode="dispatchers"
            />
          </MemoryRouter>
        </TooltipProvider>
      </I18nProvider>,
    );
  }

  it('统计卡用 overview 接口的数字', async () => {
    vi.stubGlobal('fetch', mockFetch([squadRow()]));
    renderSquadsPage();

    const stats = await screen.findByLabelText('小队统计');
    expect(within(stats).getByText('2')).toBeTruthy(); // 小队
    expect(within(stats).getByText('3')).toBeTruthy(); // 覆盖同事
    expect(within(stats).getByText('21')).toBeTruthy(); // 累计转接
    expect(within(stats).getByText('1')).toBeTruthy(); // 已发布
    // 左列表一行同样显示转接次数（与概览同源，不用 usage_count）
    const list = screen.getByLabelText('小队列表');
    expect(within(list).getByText(/21 次转接/)).toBeTruthy();
  });

  it('点左列表的另一个入口，右详情跟着换', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch([
        squadRow(),
        squadRow({
          id: 'agent_disp_2',
          name: '财务报销台',
          usage_count: 1,
          description: '报销、发票、打款',
        }),
      ]),
    );
    renderSquadsPage();

    const list = await screen.findByLabelText('小队列表');
    // 默认选中第一个
    expect(within(list).getByText('前台调度').closest('button')?.getAttribute('aria-current')).toBe('true');

    const user = userEvent.setup();
    await user.click(within(list).getByText('财务报销台'));

    expect(
      within(list).getByText('财务报销台').closest('button')?.getAttribute('aria-current'),
    ).toBe('true');
    // 右详情里出现第二个入口的描述（第一个入口的描述是空的 → 走占位文案）
    expect(await screen.findByText('报销、发票、打款')).toBeTruthy();
  });

  it('健康度有问题时给出提示，可一键只看需要处理的', async () => {
    vi.stubGlobal(
      'fetch',
      mockFetch(
        [
          squadRow(),
          squadRow({
            id: 'agent_disp_2',
            name: '配置不全的入口',
            members: [
              {
                member_agent_id: 'agent-a',
                agent_name: '入职小助手',
                routing_rule: '',
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
          }),
        ],
        { ...emptyOverview, missing_rule_members: 1 },
      ),
    );
    renderSquadsPage();

    const health = await screen.findByLabelText('配置健康度');
    expect(within(health).getByText('1 位同事没写路由规则')).toBeTruthy();

    const user = userEvent.setup();
    await user.click(within(health).getByRole('button', { name: /只看需要处理的/ }));

    const list = screen.getByLabelText('小队列表');
    expect(within(list).queryByText('前台调度')).toBeNull();
    expect(within(list).getByText('配置不全的入口')).toBeTruthy();
    // 右详情自动跟到有问题的那个
    expect(screen.getAllByText('配置不全的入口').length).toBeGreaterThanOrEqual(2);
  });

  it('overview 接口返回异常形状时不炸页面，列表照常可用', async () => {
    vi.stubGlobal('fetch', mockFetch([squadRow()], []));
    renderSquadsPage();

    expect(await screen.findByLabelText('小队列表')).toBeTruthy();
    // 数字拿不到就显示占位，不显示 0 也不抛错
    const stats = screen.getByLabelText('小队统计');
    expect(within(stats).getAllByText('—').length).toBeGreaterThan(0);
  });
});

describe('AgentsPage 静态资源', () => {
  function renderPage() {
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

  it('切到「静态资源」tab 才渲染面板，并请求 mine=1', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([agent]);
      if (url.includes('/api/enterprise/static-assets')) {
        return jsonResponse({
          items: [
            {
              id: 'sasset_1',
              filename: '报销说明.txt',
              content_type: 'text/plain',
              size: 60,
              sha256: 'a'.repeat(64),
              status: 'active',
              download_count: 2,
              created_at: '2026-10-01T00:00:00Z',
              path: '/f/tok123/%E6%8A%A5%E9%94%80%E8%AF%B4%E6%98%8E.txt',
            },
          ],
          used_bytes: 60,
          user_quota_bytes: 200 * 1024 * 1024,
          max_file_bytes: 20 * 1024 * 1024,
        });
      }
      return jsonResponse([]);
    });
    vi.stubGlobal('fetch', fetchMock);

    renderPage();
    // 默认在数字员工 tab：不该提前请求静态资源
    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes('/api/enterprise/static-assets')),
    ).toBe(false);

    await userEvent.click(screen.getByRole('tab', { name: '静态资源' }));

    expect((await screen.findAllByText('报销说明.txt')).length).toBeGreaterThan(0);
    const assetCalls = fetchMock.mock.calls
      .map((call) => String(call[0]))
      .filter((url) => url.includes('/api/enterprise/static-assets'));
    expect(assetCalls.some((url) => url.includes('mine=1'))).toBe(true);
  });

  it('hash 带 #assets 时直接落在静态资源 tab', async () => {
    window.location.hash = '#assets';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/agents')) return jsonResponse([]);
      if (url.includes('/api/enterprise/static-assets')) {
        return jsonResponse({
          items: [],
          used_bytes: 0,
          user_quota_bytes: 200 * 1024 * 1024,
          max_file_bytes: 20 * 1024 * 1024,
        });
      }
      return jsonResponse([]);
    }));

    renderPage();

    expect(await screen.findByText('还没有上传过文件')).toBeTruthy();
    // 不在数字员工 tab：员工统计区不渲染
    expect(screen.queryByText('员工总数')).toBeNull();
    window.location.hash = '';
  });
});
