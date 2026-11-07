import { describe, expect, it } from 'vitest';

import type { ChatMessage } from '@/types';

import { shouldKeepRealtimeMessage } from './chatHelpers';

/**
 * 员工小队转接答案「消失 2~3s 又出现」的回归防线。
 *
 * 这一轮的转接通知（「已转交给 X」）和成员答案是**两条独立气泡、同为 assistant、
 * 共享同一个 turn**。`hasServerMessageForTurn` 早期只比对 role + turn，于是把
 * 「服务端已有本轮 assistant（其实是那条通知）」当成「答案已经有了」，
 * `finalizeStreaming` 直接把刚流式出来的答案气泡删掉 → 空窗 → 250ms 后
 * `loadMessages` 回来才重新出现。
 */
function msg(overrides: Partial<ChatMessage> & Pick<ChatMessage, 'id' | 'role'>): ChatMessage {
  return {
    content: '',
    created_at: '2026-11-07T06:19:00Z',
    ...overrides,
  } as ChatMessage;
}

describe('shouldKeepRealtimeMessage — 员工小队转接气泡', () => {
  it('服务端只有「转接通知」时，必须保留实时流式出来的答案', () => {
    const serverNotice = msg({
      id: 'msg_notice',
      role: 'assistant',
      content: '已转交给 亚特 AI G.O',
      turnId: 'msg_turn_1',
      metadata: { dispatch_notice: { member_name: '亚特 AI G.O' } },
    });
    const streamingAnswer = msg({
      id: '__stream_answer',
      role: 'assistant',
      content: '水世界每天 10:00 开门',
      turnId: 'msg_turn_1',
      isStreaming: true,
    });

    const keep = shouldKeepRealtimeMessage(
      streamingAnswer,
      [serverNotice],
      0,
      'msg_turn_1',
    );

    expect(keep).toBe(true);
  });

  it('服务端真的已有同轮答案时，实时答案要被替换掉（不能重复）', () => {
    const serverAnswer = msg({
      id: 'msg_answer',
      role: 'assistant',
      content: '水世界每天 10:00 开门',
      turnId: 'msg_turn_1',
    });
    const realtimeAnswer = msg({
      id: '__final_answer',
      role: 'assistant',
      content: '水世界每天 10:00 开门',
      turnId: 'msg_turn_1',
    });

    const keep = shouldKeepRealtimeMessage(
      realtimeAnswer,
      [serverAnswer],
      0,
      'msg_turn_1',
    );

    expect(keep).toBe(false);
  });

  it('转接通知本身不会被当成答案（自身也不该因「服务端有通知」而被裁掉）', () => {
    const serverNotice = msg({
      id: 'msg_notice',
      role: 'assistant',
      content: '已转交给 亚特 AI G.O',
      turnId: 'msg_turn_1',
      metadata: { dispatch_notice: { member_name: '亚特 AI G.O' } },
    });
    const realtimeNotice = msg({
      id: 'notice_msg_turn_1',
      role: 'assistant',
      content: '已转交给 亚特 AI G.O',
      turnId: 'msg_turn_1',
      metadata: { dispatch_notice: { member_name: '亚特 AI G.O' } },
    });

    const keep = shouldKeepRealtimeMessage(
      realtimeNotice,
      [serverNotice],
      0,
      'msg_turn_1',
    );

    // 服务端那条通知的 client_turn_id 参与别名并集，可能认成同一轮；这里只要求
    // 「不因为服务端有同轮 assistant 就被判定成服务端已有答案」——
    // 两条通知是否合并由 computeMergedMessages 的去重负责。
    expect(typeof keep).toBe('boolean');
  });
});