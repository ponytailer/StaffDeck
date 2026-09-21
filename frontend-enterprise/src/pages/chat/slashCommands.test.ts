import { describe, expect, it } from 'vitest';

import type { ChatSlashCommand } from '@/types';

import {
  matchingSlashCommands,
  selectedSlashCommandText,
  slashCommandComposerText,
  slashCommandInput,
  slashCommandMessage,
  slashCommandQuery,
  slashMenuScrollTop,
} from './slashCommands';

const commands: ChatSlashCommand[] = [
  {
    kind: 'sop',
    target: 'refund_v1',
    label: '退款流程',
    description: '处理退款',
    command: '/sop refund_v1',
  },
  {
    kind: 'skill',
    target: 'weather',
    label: '天气查询',
    description: '查询实时天气',
    command: '/skill weather',
  },
  {
    kind: 'tool',
    target: 'price_query',
    label: '价格查询',
    description: '查询商品价格',
    command: '/tool price_query',
  },
];

describe('slash command helpers', () => {
  it('opens for slash prefixes and closes after the request text begins', () => {
    expect(slashCommandQuery('/')).toEqual({ kind: undefined, search: '' });
    expect(slashCommandQuery('/sk')).toEqual({ kind: undefined, search: 'sk' });
    expect(slashCommandQuery('/skill wea')).toEqual({ kind: 'skill', search: 'wea' });
    expect(slashCommandQuery('/skill weather 查询北京天气')).toBeNull();
  });

  it('@ 与 / 等价：补全菜单同样被 @ 前缀触发，发送时归一为 / 语法', () => {
    // 输入阶段：@ 与 / 同一套补全（同一个 PREFIX_PATTERN）
    expect(slashCommandQuery('@')).toEqual({ kind: undefined, search: '' });
    expect(slashCommandQuery('@skill wea')).toEqual({ kind: 'skill', search: 'wea' });
    expect(slashCommandQuery('@技能')).toEqual({ kind: undefined, search: '技能' });
    // 发送阶段：@skill/@技能 都归一成 / 命令卡片
    expect(slashCommandMessage('@skill weather 北京天气如何', commands)).toEqual({
      command: commands[1],
      requestText: '北京天气如何',
    });
    expect(slashCommandMessage('@技能 weather 北京天气如何', commands)).toEqual({
      command: commands[1],
      requestText: '北京天气如何',
    });
  });

  it('filters by resource kind and target text', () => {
    // 工具不再出现在补全菜单（由 SOP 捆绑调用）：/tool 深层语法保留，菜单裡只剩 sop+skill
    expect(matchingSlashCommands('/tool', commands).map((item) => item.target)).toEqual([]);
    expect(matchingSlashCommands('@', commands).map((item) => item.kind)).toEqual(['sop', 'skill']);
    expect(matchingSlashCommands('/skill 天气', commands).map((item) => item.target)).toEqual([
      'weather',
    ]);
  });

  it('inserts a command with a trailing space for the task request', () => {
    expect(selectedSlashCommandText(commands[0])).toBe('/sop refund_v1 ');
  });

  it('keeps the command token separate from the editable request text', () => {
    const input = slashCommandInput(commands[0], 'refund order 1001');
    expect(input).toBe('/sop refund_v1 refund order 1001');
    expect(slashCommandComposerText(input, commands[0])).toBe('refund order 1001');
    expect(slashCommandComposerText('plain message', commands[0])).toBe('plain message');
  });

  it('restores a selected command card from a sent user message', () => {
    expect(slashCommandMessage('/skill weather 北京天气如何', commands)).toEqual({
      command: commands[1],
      requestText: '北京天气如何',
    });
  });

  it('keeps rendering a sent command after its resource is removed', () => {
    expect(slashCommandMessage('/tool legacy_tool 执行检查', [])).toEqual({
      command: {
        kind: 'tool',
        target: 'legacy_tool',
        label: 'legacy_tool',
        description: '',
        command: '/tool legacy_tool',
      },
      requestText: '执行检查',
    });
  });

  it('keeps the keyboard-selected option inside the slash menu viewport', () => {
    expect(slashMenuScrollTop(0, 240, 252, 54)).toBe(72);
    expect(slashMenuScrollTop(120, 240, 92, 54)).toBe(86);
    expect(slashMenuScrollTop(120, 240, 180, 54)).toBe(120);
  });
});
