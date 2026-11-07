// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { employeeProfile } from '@/employee';
import { I18nProvider } from '@/i18n';
import type { DispatcherRead } from '@/lib/dispatcher';
import type { AgentProfileRead, ChatSession, TeamRead } from '@/types';

import type { UseChatSession } from '../useChatSession';
import ChatEmptyState from './ChatEmptyState';

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

const team: TeamRead = {
  id: 'team-1',
  tenant_id: 'tenant_demo',
  name: '增长团队',
  description: '负责增长实验与内容投放',
  owner_user_id: 'user-1',
  config: {},
  status: 'active',
  members: [
    { id: 'm-1', team_id: 'team-1', agent_id: 'agent-1', role: 'leader', agent_name: '小艾', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-2', team_id: 'team-1', agent_id: 'agent-2', role: 'member', agent_name: '小北', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-3', team_id: 'team-1', agent_id: 'agent-3', role: 'member', agent_name: '小南', created_at: '2026-08-01T00:00:00Z' },
    { id: 'm-4', team_id: 'team-1', agent_id: 'agent-4', role: 'member', agent_name: '小西', created_at: '2026-08-01T00:00:00Z' },
  ],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function buildChat(session: Partial<ChatSession>, extra: Record<string, unknown> = {}): UseChatSession {
  return {
    currentSession: {
      id: 'session-1',
      tenant_id: 'tenant_demo',
      status: 'active',
      updated_at: '2026-08-01T00:00:00Z',
      ...session,
    } as ChatSession,
    ...extra,
  } as unknown as UseChatSession;
}

function renderEmptyState(chat: UseChatSession) {
  return render(
    <I18nProvider>
      <ChatEmptyState chat={chat} />
    </I18nProvider>,
  );
}

afterEach(() => {
  cleanup();
});

describe('ChatEmptyState dispatcher card', () => {
  const dispatcherAgent: AgentProfileRead = {
    ...agent,
    id: 'agent-dispatcher',
    name: '测试Multi',
    description: '人事、IT 的事都能先问我',
    kind: 'dispatcher',
  };
  const dispatcher: DispatcherRead = {
    id: 'agent-dispatcher',
    tenant_id: 'tenant_demo',
    name: '测试Multi',
    description: '人事、IT 的事都能先问我',
    kind: 'dispatcher',
    status: 'active',
    model_name: 'deepseek-v4-flash',
    members: [
      {
        member_agent_id: 'agent-1',
        agent_name: '小艾',
        routing_rule: '问人事政策时转给他',
        example_queries: [],
        sort_order: 0,
        status: 'active',
        agent_status: 'active',
        free_model_enabled: false,
        has_own_model: false,
      },
      {
        member_agent_id: 'agent-2',
        agent_name: '小北',
        routing_rule: '电脑、账号、网络问题转给他',
        example_queries: [],
        sort_order: 1,
        status: 'active',
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
    usage_count: 0,
    published_to_gallery: false,
  };

  function buildDispatcherChat(extra: Record<string, unknown> = {}): UseChatSession {
    return {
      currentSession: {
        id: 'session-1',
        tenant_id: 'tenant_demo',
        status: 'active',
        updated_at: '2026-08-01T00:00:00Z',
        agent_id: 'agent-dispatcher',
      } as ChatSession,
      displayedAgent: dispatcherAgent,
      displayedProfile: employeeProfile(dispatcherAgent),
      displayedAgentIsDispatcher: true,
      displayedDispatcher: dispatcher,
      displayedDispatcherLoaded: true,
      emptyDispatcherSummary: dispatcher.description,
      emptyDispatcherStats: [
        { label: '同事', value: 2 },
        { label: '无法匹配', value: '反问' },
        { label: '模型', value: 'deepseek-v4-flash' },
      ],
      displayedTeam: null,
      agents: [dispatcherAgent, agent],
      teamEmptyStats: { tasks: 0, blackboard: 0 },
      ...extra,
    } as unknown as UseChatSession;
  }

  it('shows the form description and members instead of role/resource copy', () => {
    renderEmptyState(buildDispatcherChat());

    expect(screen.getByText(/Hello 我是/).textContent).toContain('测试Multi');
    expect(screen.getByText('人事、IT 的事都能先问我')).toBeTruthy();
    // 成员名（routing_rule 作 title，不占正文）
    expect(screen.getByText('小艾')).toBeTruthy();
    expect(screen.getByText('小北')).toBeTruthy();
    expect(screen.getByTitle('问人事政策时转给他')).toBeTruthy();
    // 统计格换成员工小队真实成立的指标
    expect(screen.getByText('同事')).toBeTruthy();
    expect(screen.getByText('无法匹配')).toBeTruthy();
    expect(screen.getByText('反问')).toBeTruthy();
    expect(screen.getByText('deepseek-v4-flash')).toBeTruthy();
  });

  it('never falls back to the employee role/resource wording', () => {
    renderEmptyState(buildDispatcherChat());

    expect(screen.queryByText(/#角色/)).toBeNull();
    expect(screen.queryByText('资料')).toBeNull();
    expect(screen.queryByText('技能')).toBeNull();
    expect(screen.queryByText('SOP')).toBeNull();
    expect(screen.queryByText('结构化整理')).toBeNull();
  });

  it('keeps a self-explaining summary when the description is empty', () => {
    renderEmptyState(buildDispatcherChat({
      displayedDispatcher: { ...dispatcher, description: '', members: [] },
      emptyDispatcherSummary: '我是 测试Multi，先说需求，我来判断交给谁。',
      emptyDispatcherStats: [
        { label: '同事', value: 0 },
        { label: '无法匹配', value: '未设置' },
        { label: '模型', value: '未配置' },
      ],
    }));

    expect(screen.getByText(/我先说|先说需求/).textContent).toContain('先说需求');
    // 详情已加载且确实没成员 → 明确说「还没有成员」，而不是一直挂「加载中」
    expect(screen.getByText('还没有成员')).toBeTruthy();
  });

  it('says members are still loading before the detail request settles', () => {
    renderEmptyState(buildDispatcherChat({
      displayedDispatcher: null,
      displayedDispatcherLoaded: false,
      emptyDispatcherSummary: '人事、IT 的事都能先问我',
      emptyDispatcherStats: [
        { label: '同事', value: 0 },
        { label: '无法匹配', value: '未设置' },
        { label: '模型', value: '未配置' },
      ],
    }));

    expect(screen.getByText('成员加载中')).toBeTruthy();
  });
});

describe('ChatEmptyState team card', () => {
  it('renders the team card for team sessions', () => {
    renderEmptyState(buildChat(
      { team_id: 'team-1', team_name: '增长团队' },
      { displayedTeam: team, agents: [agent], teamEmptyStats: { tasks: 2, blackboard: 3 } },
    ));

    expect(screen.getByText(/Hello 我们是/).textContent).toContain('增长团队');
    expect(screen.getByText('我们来做什么？')).toBeTruthy();
    expect(screen.getByText('负责增长实验与内容投放')).toBeTruthy();
    // 成员名标签，项目领导带后缀标识
    expect(screen.getByText(/小艾 · 项目领导/)).toBeTruthy();
    expect(screen.getByText(/小北/)).toBeTruthy();
    // 统计格：成员数 / 任务数 / 黑板条目数
    expect(screen.getByText('成员数')).toBeTruthy();
    expect(screen.getByText('4')).toBeTruthy();
    expect(screen.getByText('任务数')).toBeTruthy();
    expect(screen.getByText('2')).toBeTruthy();
    expect(screen.getByText('黑板条目数')).toBeTruthy();
    expect(screen.getByText('3')).toBeTruthy();
  });

  it('falls back to team_name and a member-count summary when the team is not loaded', () => {
    renderEmptyState(buildChat(
      { team_id: 'team-1', team_name: '增长团队' },
      { displayedTeam: null, agents: [], teamEmptyStats: { tasks: 0, blackboard: 0 } },
    ));

    expect(screen.getByText(/Hello 我们是/).textContent).toContain('增长团队');
    expect(screen.getByText(/团队由 0 名成员组成/)).toBeTruthy();
  });

  it('still renders the employee card for regular sessions', () => {
    renderEmptyState(buildChat(
      { agent_id: 'agent-1' },
      {
        displayedAgent: agent,
        displayedProfile: employeeProfile(agent),
        emptyRoleSummary: 'role summary',
        emptyProfileTags: ['结构化整理'],
        emptyStats: [
          { label: '资料', value: 1 },
          { label: '技能', value: 2 },
          { label: 'SOP', value: 3 },
        ],
        displayedTeam: null,
        agents: [agent],
        teamEmptyStats: { tasks: 0, blackboard: 0 },
      },
    ));

    expect(screen.getByText(/Hello 我是/).textContent).toContain('小艾');
    expect(screen.queryByText(/Hello 我们是/)).toBeNull();
    expect(screen.getByText('资料')).toBeTruthy();
  });
});
