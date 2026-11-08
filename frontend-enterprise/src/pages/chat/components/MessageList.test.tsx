// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ChatMessage } from '@/types';

import type { UseChatSession } from '../useChatSession';
import MessageList from './MessageList';

const answerWithCitation: ChatMessage = {
  id: 'msg-assistant-1',
  role: 'assistant',
  content: '身高和体重确实都有要求，部分设备设有体重限制，具体以现场公告为准。[1]',
  created_at: '2026-10-19T00:00:00Z',
  metadata: {
    knowledge_citations: [
      {
        id: 'cite-1',
        label: '[1]',
        title: '乐园手册：乘坐须知',
        document_id: 'doc-1',
        section_path: '第 3 页',
      },
    ],
  },
};

function chatStub(patch: Record<string, unknown> = {}) {
  return {
    displayedMessages: [answerWithCitation],
    turnTraceRef: { current: new Map() },
    uiConfig: { show_thinking_trace: true, show_skill_trace: true, show_tool_trace: true },
    currentStream: {},
    runningTurn: null,
    expandedTraceIds: [],
    collapsedTraceIds: [],
    isCurrentStreamingTrace: () => false,
    dismissedDraftMessageIds: [],
    createdScheduledTasks: {},
    activeConversationId: 'session-1',
    currentScheduledDraft: null,
    hasVisibleMessageScheduledDraft: false,
    confirmScheduledTask: vi.fn(),
    dismissScheduledTaskDraft: vi.fn(),
    chatMessagesRef: { current: null },
    handleChatMessagesScroll: vi.fn(),
    SHOW_DEBUG: false,
    lastTurn: null,
    hideKnowledgeCitations: false,
    setActiveCitation: vi.fn(),
    toggleTrace: vi.fn(),
    rateMessage: vi.fn(),
    removeQueuedTurn: vi.fn(),
    displayedTeam: undefined,
    displayedAgent: undefined,
    slashCommands: [],
    tenantId: 'tenant_demo',
    submitSlotForm: vi.fn(),
    ...patch,
  } as unknown as UseChatSession;
}

describe('MessageList 知识来源开关', () => {
  afterEach(cleanup);

  it('展示来源时保留正文编号与来源卡片', () => {
    render(<MessageList chat={chatStub()} />);

    expect(screen.getByText(/具体以现场公告为准。\[1\]/)).toBeTruthy();
    expect(screen.getByLabelText('知识引用')).toBeTruthy();
  });

  it('全局关掉来源后，正文编号也不再出现', () => {
    render(<MessageList chat={chatStub({ hideKnowledgeCitations: true })} />);

    expect(screen.getByText('身高和体重确实都有要求，部分设备设有体重限制，具体以现场公告为准。')).toBeTruthy();
    expect(screen.queryByText(/\[1\]/)).toBeNull();
    expect(screen.queryByLabelText('知识引用')).toBeNull();
  });
});

/**
 * 回归（用户原话「小队回复耗时感觉不准，3s 左右就有答案了，但是时间计时仍然在增加」）：
 * 已经定稿的答案气泡若只拿到本地新建、没有终态的 trace，`TurnDurationNote`
 * 会把它当「本轮仍在执行」每 250ms 跳表 —— 常见触发是刷新后 `/trace` 投影
 * 还没回来，或 turn id 别名让 finishTrace 落在另一个 trace 上。
 */
describe('MessageList 总耗时显示', () => {
  afterEach(cleanup);

  const traceFor = (patch: Record<string, unknown> = {}) => ({
    lines: [],
    startedAt: Date.now() - 8900,
    ...patch,
  });

  it('答案已定稿但 trace 没有终态时不显示总耗时，等服务端投影', () => {
    const turnTraceRef = { current: new Map([['msg-assistant-1', traceFor()]]) };
    render(<MessageList chat={chatStub({ turnTraceRef })} />);

    expect(screen.queryByText(/总耗时/)).toBeNull();
  });

  it('trace 带终态时显示权威总耗时并定格', () => {
    const turnTraceRef = {
      current: new Map([['msg-assistant-1', traceFor({ durationMs: 8900, completedAt: Date.now() })]]),
    };
    render(<MessageList chat={chatStub({ turnTraceRef })} />);

    expect(screen.getByText(/总耗时/).textContent).toContain('8.9s');
    expect(screen.getByTitle(/本轮总耗时/)).toBeTruthy();
  });

  it('流式气泡本来就不挂反馈区，计时也不该出现（既有行为不变）', () => {
    const streaming = { ...answerWithCitation, isStreaming: true };
    const turnTraceRef = { current: new Map([['msg-assistant-1', traceFor()]]) };
    render(<MessageList chat={chatStub({ displayedMessages: [streaming], turnTraceRef })} />);

    expect(screen.queryByText(/总耗时/)).toBeNull();
  });
});
