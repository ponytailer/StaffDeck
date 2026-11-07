// @vitest-environment jsdom

/**
 * 调度入口的前端契约：
 * - 编辑器拒绝 <2 / >3 成员、缺规则、缺模型（保存与试跑都要拦）；
 * - payload 形状与后端一致（members[].routing_rule / config.fallback_mode）；
 * - MessageBubble 有/无 metadata.dispatch 两态；
 * - AgentsPage 把 dispatcher 单独成组，不混进员工网格。
 */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui/tooltip';
import type { AgentProfileRead, ChatMessage, ModelConfigRead } from '@/types';
import { MAX_DISPATCH_MEMBERS, MIN_DISPATCH_MEMBERS, memberCountHint } from '@/lib/dispatcher';

import AgentsPage from './AgentsPage';
import MessageBubble, { dispatchAnswerInfo } from './chat/components/MessageBubble';
import type { MessageRender } from './chat/components/MessageBubble';
import {
  computeMergedMessages,
  dispatchNoticeInfo,
  hasAssistantCarrierForTurn,
  hasAssistantMessageForTurn,
} from './chat/chatHelpers';

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
  if (!HTMLElement.prototype.scrollIntoView) {
    HTMLElement.prototype.scrollIntoView = () => {};
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

/* ─────────────────────── 数量口径 ─────────────────────── */

describe('dispatcher member count contract', () => {
  it('下限是 2 而不是 1', () => {
    expect(MIN_DISPATCH_MEMBERS).toBe(2);
    expect(MAX_DISPATCH_MEMBERS).toBe(3);
  });

  it('1 个成员时给出「直接建普通员工」的理由，而不是只说数量不对', () => {
    const hint = memberCountHint(1);
    expect(hint).toBeTruthy();
    expect(hint).toContain('普通员工');
  });

  it('3 个成员合法、4 个越界', () => {
    expect(memberCountHint(3)).toBeNull();
    expect(memberCountHint(4)).toContain('最多');
  });
});

/* ─────────────────────── 答案归属 │ chip ─────────────────────── */

function bubbleMessage(metadata?: Record<string, unknown>): ChatMessage {
  return {
    id: 'm-assistant',
    role: 'assistant',
    content: '差旅费走「差旅报销单」，电子发票上传 PDF 或 OFD 原件。',
    metadata,
    created_at: '2026-11-06T00:00:00Z',
  };
}

function bubbleRender(): MessageRender {
  return {
    traceTurnId: 'turn-1',
    summary: null,
    details: [],
    expanded: false,
    showInlineTrace: false,
    visibleContent: '差旅费走「差旅报销单」，电子发票上传 PDF 或 OFD 原件。',
    citations: [],
    scheduledDraft: null,
    scheduledTaskPrompt: false,
    attachments: [],
    harnessArtifacts: [],
    statusOnly: false,
  };
}

function fakeChat(): Parameters<typeof MessageBubble>[0]['chat'] {
  return {
    tenantId: 'tenant_demo',
    activeConversationId: 'session-1',
    displayedAgent: null,
    displayedTeam: null,
    slashCommands: [],
    toggleTrace: () => {},
    rateMessage: () => {},
    setActiveCitation: () => {},
    confirmScheduledTask: async () => {},
    dismissScheduledTaskDraft: () => {},
    removeQueuedTurn: () => {},
    submitSlotForm: () => {},
  } as unknown as Parameters<typeof MessageBubble>[0]['chat'];
}

describe('dispatch answer attribution', () => {
  it('metadata.dispatch 存在时解析出成员名与意图', () => {
    const info = dispatchAnswerInfo(
      bubbleMessage({
        dispatch: { member_agent_id: 'agent_hr', member_name: '小王', intent: '差旅报销' },
      }),
    );
    expect(info?.memberName).toBe('小王');
    expect(info?.intent).toBe('差旅报销');
  });

  it('普通员工的答案没有 dispatch 元数据 → 不解析', () => {
    expect(dispatchAnswerInfo(bubbleMessage({}))).toBeNull();
    expect(dispatchAnswerInfo(bubbleMessage())).toBeNull();
  });

  it('dispatch 存在但缺 member_name（异常数据）→ 不显示 chip', () => {
    expect(dispatchAnswerInfo(bubbleMessage({ dispatch: { member_agent_id: 'x' } }))).toBeNull();
  });

  it('渲染「由 小王 回答」', () => {
    const item = bubbleMessage({
      dispatch: { member_agent_id: 'agent_hr', member_name: '小王', intent: '差旅报销' },
    });
    render(
      <MessageBubble
        chat={fakeChat()}
        item={item}
        render={{ ...bubbleRender(), dispatchInfo: dispatchAnswerInfo(item) }}
      />,
    );
    expect(screen.getByText(/由 小王 回答/)).toBeTruthy();
  });

  it('没有归属信息时不渲染 chip', () => {
    render(<MessageBubble chat={fakeChat()} item={bubbleMessage()} render={bubbleRender()} />);
    expect(screen.queryByText(/回答/)).toBeNull();
  });
});

/* ─────────────────── 转接通知（路由先说话） ─────────────────── */

function noticeMessage(meta: Record<string, unknown>, turnId = 'turn-1'): ChatMessage {
  return {
    id: 'm-notice',
    role: 'assistant',
    content: '已转交给 亚特 AI G.O',
    metadata: { dispatch_notice: meta, turn_id: turnId, client_turn_id: turnId },
    created_at: '2026-11-06T00:00:00Z',
    turnId,
  };
}

describe('dispatch transfer notice', () => {
  it('metadata.dispatch_notice 能解析出目标成员', () => {
    const info = dispatchNoticeInfo(noticeMessage({ member_name: '亚特 AI G.O', intent: '雪场交通' }));
    expect(info?.memberName).toBe('亚特 AI G.O');
    expect(info?.intent).toBe('雪场交通');
  });

  it('缺 member_name 的异常数据不解析', () => {
    expect(dispatchNoticeInfo(noticeMessage({ member_agent_id: 'x' }))).toBeNull();
  });

  it('渲染成「路由」身份的气泡，并带出成员与意图', () => {
    render(
      <MessageBubble
        chat={fakeChat()}
        item={noticeMessage({ member_name: '亚特 AI G.O', intent: '雪场交通' })}
        render={bubbleRender()}
      />,
    );
    expect(screen.getByText('路由')).toBeTruthy();
    expect(screen.getByText('亚特 AI G.O')).toBeTruthy();
    expect(screen.getByText('雪场交通')).toBeTruthy();
  });

  it('转接通知不显示点赞等答案操作（它不是答案）', () => {
    render(
      <MessageBubble
        chat={fakeChat()}
        item={noticeMessage({ member_name: '亚特 AI G.O' })}
        render={bubbleRender()}
      />,
    );
    expect(screen.queryByLabelText('点赞')).toBeNull();
  });

  it('转接通知**不算**这一轮已经有答案', () => {
    // 关键契约：否则子 turn 还没跑完就被判定完成 → stream.loading 提前落 true→false，
    // 轮询停止、成员答案永远接不上。
    const slot = {
      serverMessages: [noticeMessage({ member_name: '亚特 AI G.O' })],
      realtimeMessages: [],
    } as unknown as Parameters<typeof hasAssistantMessageForTurn>[0];
    expect(hasAssistantMessageForTurn(slot, 'turn-1')).toBe(false);
    expect(hasAssistantCarrierForTurn(slot, 'turn-1')).toBe(false);
  });

  it('成员答案和转接通知共存：两条都保留，且通知排在答案前面', () => {
    const answer: ChatMessage = {
      id: 'm-answer',
      role: 'assistant',
      content: '从日内瓦坐大巴约 3.5 小时到霞慕尼。',
      metadata: { turn_id: 'turn-1', client_turn_id: 'turn-1' },
      created_at: '2026-11-06T00:00:03Z',
      turnId: 'turn-1',
    };
    const slot = {
      serverMessages: [
        {
          id: 'm-user',
          role: 'user',
          content: '亚特兰蒂斯怎么去',
          created_at: '2026-11-06T00:00:00Z',
        } as ChatMessage,
        noticeMessage({ member_name: '亚特 AI G.O' }),
        answer,
      ],
      realtimeMessages: [],
    } as unknown as Parameters<typeof computeMergedMessages>[0];

    const merged = computeMergedMessages(slot);
    const assistants = merged.filter((item) => item.role === 'assistant');
    expect(assistants).toHaveLength(2);
    expect(assistants[0].metadata?.dispatch_notice).toBeTruthy();
    expect(assistants[1].id).toBe('m-answer');
    expect(hasAssistantMessageForTurn(slot, 'turn-1')).toBe(true);

    // 通知必须夹在「用户消息之后、答案之前」——不能顶到用户消息前面
    // （真实踩过：排序规则没限定 assistant↔assistant，通知跑到了提问上面）。
    const order = merged.map((item) => item.id);
    expect(order.indexOf('m-user')).toBeLessThan(order.indexOf('m-notice'));
    expect(order.indexOf('m-notice')).toBeLessThan(order.indexOf('m-answer'));
  });

  it('流式气泡期间也要显示「由 X 回答」（与普通 agent 表现一致）', () => {
    // 服务端只在**最终答案消息**里写 metadata.dispatch，流式期间的草稿气泡
    // 由 useChatSession 自己补（turnDispatchRef）。这里锁死「带 dispatch 的
    // 流式气泡照样渲染 chip」这条渲染契约。
    const streaming: ChatMessage = {
      id: 'stream:turn-1',
      role: 'assistant',
      content: '从日内瓦坐大巴约',
      created_at: '2026-11-06T00:00:01Z',
      turnId: 'turn-1',
      isStreaming: true,
      metadata: { dispatch: { member_agent_id: 'agent_atlantis', member_name: '亚特 AI G.O' } },
    };
    render(
      <MessageBubble
        chat={fakeChat()}
        item={streaming}
        render={{ ...bubbleRender(), dispatchInfo: dispatchAnswerInfo(streaming) }}
      />,
    );
    expect(screen.getByText(/由 亚特 AI G\.O 回答/)).toBeTruthy();
  });
});

/* ─────────────────────── 列表分组 ─────────────────────── */

const employee: AgentProfileRead = {
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

const dispatcherRow = {
  id: 'agent_disp',
  tenant_id: 'tenant_demo',
  name: '人事服务台',
  description: '人事问题都可以先问我',
  kind: 'dispatcher',
  status: 'active',
  model_config_id: 'model-1',
  model_name: 'deepseek-v4-flash',
  usage_count: 0,
  members: [
    {
      member_agent_id: 'agent-1',
      routing_rule: '入职、离职、考勤',
      example_queries: ['入职要什么材料'],
      sort_order: 0,
      status: 'active',
      agent_name: '小艾',
      agent_description: '入职办理',
      agent_status: 'active',
      free_model_enabled: false,
      has_own_model: false,
    },
  ],
  config: {
    fallback_mode: 'clarify',
    fallback_reply: '换个说法再试一次？',
    default_member_agent_id: null,
    offer_candidates: true,
    min_confidence: 0.5,
  },
};

const model: ModelConfigRead = {
  id: 'model-1',
  tenant_id: 'tenant_demo',
  name: 'deepseek-v4-flash',
  model: 'deepseek-v4-flash',
  enabled: true,
  is_default: true,
} as ModelConfigRead;

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

describe('AgentsPage dispatcher grouping', () => {
  it('多智能体单独成组，不混进员工卡片网格', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/enterprise/dispatchers')) return jsonResponse([dispatcherRow]);
        if (url.includes('/api/enterprise/model-configs')) return jsonResponse([model]);
        if (url.includes('/api/enterprise/agents')) return jsonResponse([employee]);
        return jsonResponse([]);
      }),
    );

    renderPage();

    // 员工与入口都要出现，但入口在独立的「多智能体」tab 里，不混进员工网格。
    expect((await screen.findAllByText('小艾')).length).toBeGreaterThan(0);
    expect(screen.queryByText('人事服务台')).toBeNull();
    expect(screen.queryByLabelText('多智能体')).toBeNull();

    fireEvent.click(screen.getByRole('tab', { name: '多智能体' }));
    expect(await screen.findByText('人事服务台')).toBeTruthy();
    const section = screen.getByLabelText('多智能体');
    expect(section.textContent).toContain('人事服务台');
    // 员工统计卡也不该把入口算进去（切 tab 后员工区已不渲染）
    expect(screen.queryByLabelText('数字员工统计')).toBeNull();
  });

  it('没有多智能体时给创建引导，而不是空分区', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/enterprise/agents')) return jsonResponse([employee]);
        return jsonResponse([]);
      }),
    );

    renderPage();
    expect((await screen.findByText('小艾')).textContent).toBeTruthy();
    expect(screen.queryByLabelText('多智能体')).toBeNull();

    fireEvent.click(screen.getByRole('tab', { name: '多智能体' }));
    expect(await screen.findByText('还没有多智能体')).toBeTruthy();
    expect(screen.getByText('创建多智能体')).toBeTruthy();
  });

  it('缺规则的成员在卡片上标出来', async () => {
    const broken = {
      ...dispatcherRow,
      members: [{ ...dispatcherRow.members[0], routing_rule: '' , example_queries: [] }],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('/api/enterprise/dispatchers')) return jsonResponse([broken]);
        if (url.includes('/api/enterprise/agents')) return jsonResponse([employee]);
        return jsonResponse([]);
      }),
    );

    renderPage();
    fireEvent.click(screen.getByRole('tab', { name: '多智能体' }));
    expect(await screen.findByText('缺规则')).toBeTruthy();
  });
});