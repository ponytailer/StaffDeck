import { describe, expect, it } from 'vitest';

import type { ChatMessage } from '@/types';

import { computeMergedMessages, upsertStreamingTracePlaceholder } from './chatHelpers';

/**
 * 转接路径下两个「真实被用户截图反馈过」的渲染缺陷的回归防线。
 *
 * 1. **答案流完又消失、几秒后才回来**
 *    `loadTraces` 每轮（~1.5s）只要发现本轮有可见 trace 就会调
 *    `upsertStreamingTracePlaceholder`，而它曾经无条件写 `content: ''`。
 *    子 turn 的增量早已把正文流进同一个气泡，于是每轮轮询都把答案清空
 *    一次、下一次 delta 又补回来 —— 用户看到的就是反复闪烁。
 *
 * 2. **同一条答案上方出现两段「已转交给 X」**
 *    `dispatch_notice` 会从 SSE 实时与事件回放两条路各来一次，两个 turn id
 *    一个是本地 `turn_xxx`、一个是服务端 `msg_xxx`。上游判重之外，
 *    `computeMergedMessages` 出口必须再做一层兜底：同轮只放一条通过。
 */
function msg(overrides: Partial<ChatMessage> & Pick<ChatMessage, 'id' | 'role'>): ChatMessage {
  return {
    content: '',
    created_at: '2026-11-07T06:19:00Z',
    ...overrides,
  } as ChatMessage;
}

function emptySlot(messages: ChatMessage[] = []) {
  return {
    sessionId: 'sess_1',
    serverMessages: [] as ChatMessage[],
    realtimeMessages: messages,
  } as unknown as Parameters<typeof upsertStreamingTracePlaceholder>[0];
}

describe('upsertStreamingTracePlaceholder — 绝不覆盖已有正文', () => {
  it('已有正文的流式气泡：不动，且报告「无需变更」', () => {
    const slot = emptySlot([
      msg({
        id: '__streaming_sess_1_turn_1',
        role: 'assistant',
        turnId: 'turn_1',
        content: '重庆 Mall 目前可停约 2000 辆车',
        isStreaming: true,
      }),
    ]);

    const changed = upsertStreamingTracePlaceholder(slot, 'sess_1', 'turn_1');

    expect(changed).toBe(false);
    expect(slot.realtimeMessages[0].content).toBe('重庆 Mall 目前可停约 2000 辆车');
  });

  it('占位符已存在但正文为空：只补元信息，正文仍为空（不产生假内容）', () => {
    const slot = emptySlot([
      msg({
        id: '__streaming_sess_1_turn_1',
        role: 'assistant',
        turnId: 'turn_1',
        content: '',
        isStreaming: true,
      }),
    ]);

    const changed = upsertStreamingTracePlaceholder(slot, 'sess_1', 'turn_1');

    expect(changed).toBe(false);
    expect(slot.realtimeMessages[0].content).toBe('');
  });

  it('气泡不存在时新建一个空占位符（供 trace / 耗时挂载）', () => {
    const slot = emptySlot([]);

    const changed = upsertStreamingTracePlaceholder(slot, 'sess_1', 'turn_1');

    expect(changed).toBe(true);
    expect(slot.realtimeMessages).toHaveLength(1);
    expect(slot.realtimeMessages[0].isStreaming).toBe(true);
    expect(slot.realtimeMessages[0].content).toBe('');
  });

  it('已有正文但已结束流式：不能借挂载 trace 之机把 isStreaming 翻回来并清空正文', () => {
    const slot = emptySlot([
      msg({
        id: '__streaming_sess_1_turn_1',
        role: 'assistant',
        turnId: 'turn_1',
        content: '完整答案正文',
        isStreaming: false,
      }),
    ]);

    upsertStreamingTracePlaceholder(slot, 'sess_1', 'turn_1');

    expect(slot.realtimeMessages[0].content).toBe('完整答案正文');
  });
});

describe('computeMergedMessages — 同一轮只渲染一条转接通知', () => {
  function slotWith(messages: ChatMessage[]) {
    return {
      sessionId: 'sess_1',
      serverMessages: messages,
      realtimeMessages: [] as ChatMessage[],
    } as unknown as Parameters<typeof computeMergedMessages>[0];
  }

  it('两条 turn id 不同的 notice（本地 turn_xxx + 服务端 msg_xxx）只留一条', () => {
    const slot = slotWith([
      msg({ id: 'msg_user_1', role: 'user', turnId: 'msg_turn_1', content: '重庆Mall停车怎么收费' }),
      msg({
        id: 'notice_turn_abc',
        role: 'assistant',
        turnId: 'turn_abc',
        content: '已转交给 重庆 AI G.O',
        metadata: { dispatch_notice: { member_name: '重庆 AI G.O' }, client_turn_id: 'turn_abc' },
      }),
      msg({
        id: 'msg_notice_1',
        role: 'assistant',
        turnId: 'msg_turn_1',
        content: '已转交给 重庆 AI G.O',
        metadata: { dispatch_notice: { member_name: '重庆 AI G.O' }, client_turn_id: 'turn_abc' },
      }),
      msg({
        id: 'msg_answer_1',
        role: 'assistant',
        turnId: 'msg_turn_1',
        content: '目前只明确了停车位配置，尚未公布收费标准。',
        metadata: { dispatch: { member_name: '重庆 AI G.O', intent: '重庆Mall停车收费咨询' } },
      }),
    ]);

    const merged = computeMergedMessages(slot, 'msg_turn_1');
    const notices = merged.filter((item) => Boolean(item.metadata?.dispatch_notice));
    const answers = merged.filter((item) => item.role === 'assistant' && !item.metadata?.dispatch_notice);

    expect(notices).toHaveLength(1);
    expect(answers).toHaveLength(1);
    expect(answers[0].content).toContain('停车位配置');
  });

  it('不同轮次的通知互不影响（历史会话里的转接记录不能被吞）', () => {
    const slot = slotWith([
      msg({ id: 'msg_user_1', role: 'user', turnId: 'msg_turn_1', content: '第一轮' }),
      msg({
        id: 'msg_notice_1',
        role: 'assistant',
        turnId: 'msg_turn_1',
        content: '已转交给 亚特 AI G.O',
        metadata: { dispatch_notice: { member_name: '亚特 AI G.O' } },
      }),
      msg({ id: 'msg_answer_1', role: 'assistant', turnId: 'msg_turn_1', content: '第一轮答案' }),
      msg({ id: 'msg_user_2', role: 'user', turnId: 'msg_turn_2', content: '第二轮' }),
      msg({
        id: 'msg_notice_2',
        role: 'assistant',
        turnId: 'msg_turn_2',
        content: '已转交给 重庆 AI G.O',
        metadata: { dispatch_notice: { member_name: '重庆 AI G.O' } },
      }),
      msg({ id: 'msg_answer_2', role: 'assistant', turnId: 'msg_turn_2', content: '第二轮答案' }),
    ]);

    const merged = computeMergedMessages(slot, null);
    const notices = merged.filter((item) => Boolean(item.metadata?.dispatch_notice));

    expect(notices).toHaveLength(2);
    expect(notices.map((item) => item.turnId)).toEqual(['msg_turn_1', 'msg_turn_2']);
  });
});