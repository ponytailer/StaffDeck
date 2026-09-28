import { describe, expect, it } from 'vitest';

import {
  commentRange,
  duplicateReviewAdvice,
  duplicateReviewBlockedText,
  formatElapsed,
  formatThinkingSize,
  formatTokens,
  inFlightPhaseLabel,
  isOcrSkipped,
  isTaskInFlight,
  ocrSkipHint,
  ocrStatusMeta,
  pageNumbers,
  sampleRuleFileText,
  sortComments,
  splitTextLinks,
  splitThinkingBlocks,
  taskPollingInterval,
  validateRuleFileText,
  validateTaskDraft,
} from './aiReviewModel';
import type { AiReviewComment } from '../../api/aiReview';

describe('taskPollingInterval', () => {
  it('活跃任务用 3s，空闲用 10s', () => {
    expect(taskPollingInterval([{ status: 'queued' }, { status: 'succeeded' }])).toBe(3000);
    expect(taskPollingInterval([{ status: 'running' }])).toBe(3000);
    expect(taskPollingInterval([{ status: 'succeeded' }, { status: 'failed' }])).toBe(10000);
    expect(taskPollingInterval([])).toBe(10000);
  });
});

describe('formatElapsed / formatTokens', () => {
  it('elapsed 支持秒与分钟', () => {
    expect(formatElapsed(42)).toBe('42s');
    expect(formatElapsed(125.4)).toBe('2m5s');
    expect(formatElapsed('2.5')).toBe('3s');
    expect(formatElapsed('unknown')).toBe('unknown');
    expect(formatElapsed(undefined)).toBe('');
  });

  it('tokens 千位缩写', () => {
    expect(formatTokens(980)).toBe('980');
    expect(formatTokens(1532)).toBe('1.5k');
    expect(formatTokens(undefined)).toBe('-');
  });
});

describe('commentRange', () => {
  it('行区间与整文件', () => {
    expect(commentRange({ start_line: 3, end_line: 7 })).toBe('L3-L7');
    expect(commentRange({ start_line: 3, end_line: 3 })).toBe('L3');
    expect(commentRange({ start_line: null, end_line: null })).toBe('整体');
  });
});

describe('sortComments', () => {
  it('按文件分组、文件内按行升序', () => {
    const comments: AiReviewComment[] = [
      { path: 'b.ts', start_line: 9 },
      { path: 'a.ts', start_line: 5 },
      { path: 'a.ts', start_line: 1 },
      { path: 'b.ts', start_line: 2 },
    ];
    const sorted = sortComments(comments);
    expect(sorted.map((item) => `${item.path}:${item.start_line}`)).toEqual([
      'a.ts:1',
      'a.ts:5',
      'b.ts:2',
      'b.ts:9',
    ]);
    // 不修改入参
    expect(comments[0].path).toBe('b.ts');
  });
});

describe('validateTaskDraft', () => {
  it('要求与预设至少占一头', () => {
    expect(validateTaskDraft('', [])).toContain('评审要求');
    expect(validateTaskDraft('关注并发', [])).toBeNull();
    expect(validateTaskDraft('', ['preset_1'])).toBeNull();
  });
});

describe('pageNumbers', () => {
  it('当前页居中、最多 5 个页码、边界收敛', () => {
    expect(pageNumbers(1, 10)).toEqual([1, 2, 3, 4, 5]);
    expect(pageNumbers(3, 10)).toEqual([1, 2, 3, 4, 5]);
    expect(pageNumbers(5, 10)).toEqual([3, 4, 5, 6, 7]);
    expect(pageNumbers(9, 10)).toEqual([6, 7, 8, 9, 10]);
    expect(pageNumbers(2, 3)).toEqual([1, 2, 3]);
    expect(pageNumbers(4, 1)).toEqual([1]);
    expect(pageNumbers(0, 0)).toEqual([1]);
  });
});

describe('validateRuleFileText', () => {
  /** 断言校验失败并回传报错文案（同时完成类型窄化）。 */
  function badMessage(text: string): string {
    const result = validateRuleFileText(text);
    expect(result.ok).toBe(false);
    return result.ok ? '' : result.message;
  }

  it('示例自身就是合法结构', () => {
    const result = validateRuleFileText(sampleRuleFileText());
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.config.rules).toHaveLength(3);
      expect(result.config.rules?.[2].merge_system_rule).toBe(true);
    }
  });

  it('非法 JSON / 顶层非对象直接拦下', () => {
    expect(validateRuleFileText('{oops').ok).toBe(false);
    expect(badMessage('[1]')).toContain('顶层');
  });

  it('字段结构与空内容校验', () => {
    expect(badMessage('{"include": "x"}')).toContain('include');
    expect(badMessage('{"rules": [{"path": "", "rule": "x"}]}')).toContain('path');
    expect(badMessage('{"rules": [{"path": "a/**", "rule": ""}]}')).toContain('rule');
    expect(badMessage('{"include": []}')).toContain('至少');
  });

  it('合法内容回传清洗后的对象', () => {
    const result = validateRuleFileText('{"exclude": ["  **/vendor/**  "], "rules": [{"path": "**/*.go", "rule": "x"}]}');
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.config.exclude).toEqual(['**/vendor/**']);
      expect(result.config.rules?.[0].merge_system_rule).toBe(false);
    }
  });
});

describe('ocrStatusMeta / isOcrSkipped', () => {
  it('skipped 译成「无可评审文件（已跳过）」而不是失败', () => {
    const meta = ocrStatusMeta('skipped');
    expect(meta?.label).toBe('无可评审文件（已跳过）');
    expect(meta?.tone).toContain('#fff7e8');
    expect(isOcrSkipped('skipped')).toBe(true);
    expect(isOcrSkipped('SKIPPED')).toBe(true);
    expect(isOcrSkipped('succeeded')).toBe(false);
  });

  it('正常收尾与失败的别名都归一', () => {
    expect(ocrStatusMeta('succeeded')?.label).toBe('已完成');
    expect(ocrStatusMeta('done')?.label).toBe('已完成');
    expect(ocrStatusMeta('failed')?.label).toBe('失败');
    expect(ocrStatusMeta('running')?.label).toBe('评审中');
  });

  it('空值与未知值：空值不渲染 chip，未知值原样展示', () => {
    expect(ocrStatusMeta('')).toBeNull();
    expect(ocrStatusMeta(undefined)).toBeNull();
    expect(ocrStatusMeta('weird')?.label).toBe('weird');
  });
});

describe('ocrSkipHint', () => {
  it('非 skipped 不产提示', () => {
    expect(ocrSkipHint('succeeded')).toBeNull();
    expect(ocrSkipHint(undefined)).toBeNull();
  });

  it('skipped 时给出结论 + 三条成因 + 处置建议', () => {
    const hint = ocrSkipHint('skipped', { files_reviewed: 0 });
    expect(hint?.headline).toContain('实际评审文件数 0');
    expect(hint?.reasons).toHaveLength(3);
    expect(hint?.advice).toContain('include');
  });

  it('源分支与目标分支相同时，第一条成因换成确定性的空 diff 说明', () => {
    const hint = ocrSkipHint('skipped', {
      files_reviewed: 0,
      source_branch: 'main',
      target_branch: 'main',
    });
    expect(hint?.reasons[0]).toContain('源分支与目标分支相同（均为 main）');
  });

  it('分支不同时保留通用的三种成因', () => {
    const hint = ocrSkipHint('skipped', {
      files_reviewed: 0,
      source_branch: 'feature/x',
      target_branch: 'main',
    });
    expect(hint?.reasons[0]).toContain('diff 为空');
    expect(hint?.reasons[1]).toContain('.txt');
  });
});

describe('isTaskInFlight / inFlightPhaseLabel', () => {
  it('只有 queued / running 算「进行中」', () => {
    expect(isTaskInFlight('queued')).toBe(true);
    expect(isTaskInFlight('running')).toBe(true);
    expect(isTaskInFlight('succeeded')).toBe(false);
    expect(isTaskInFlight('failed')).toBe(false);
    expect(isTaskInFlight('')).toBe(false);
    expect(isTaskInFlight(undefined)).toBe(false);
    expect(isTaskInFlight(null)).toBe(false);
  });

  it('阶段名：queued → 排队中，其余（含未知）→ 评审中', () => {
    expect(inFlightPhaseLabel('queued')).toBe('排队中');
    expect(inFlightPhaseLabel('running')).toBe('评审中');
    expect(inFlightPhaseLabel('')).toBe('评审中');
    expect(inFlightPhaseLabel(undefined)).toBe('评审中');
  });
});

describe('duplicateReviewBlockedText', () => {
  it('带上编号与阶段，并说明「同时只能有一个」', () => {
    const text = duplicateReviewBlockedText(170, 'running');
    expect(text).toBe(
      'PR/MR #170 已有进行中的评审任务（评审中），同一 PR/MR 同时只能有一个评审任务，请等它结束后再发起。',
    );
  });

  it('排队中的任务用「排队中」而不是「评审中」', () => {
    expect(duplicateReviewBlockedText(7, 'queued')).toContain('（排队中）');
  });

  it('状态缺失时退化成通用文案，不出现空括号', () => {
    const text = duplicateReviewBlockedText(7);
    expect(text).toContain('（评审中）');
    expect(text).not.toContain('（）');
  });
});

describe('duplicateReviewAdvice', () => {
  it('给出「等待」与「先清理卡住的任务」两条路径', () => {
    const tips = duplicateReviewAdvice();
    expect(tips).toHaveLength(2);
    expect(tips[0]).toContain('恢复可用');
    expect(tips[1]).toContain('删除或重试');
  });
});

describe('formatThinkingSize', () => {
  it('空值返回空串（调用方据此不渲染整块）', () => {
    expect(formatThinkingSize('')).toBe('');
    expect(formatThinkingSize('   \n ')).toBe('');
    expect(formatThinkingSize(undefined)).toBe('');
    expect(formatThinkingSize(null)).toBe('');
  });

  it('千字符以下给精确值，以上折算成 k', () => {
    expect(formatThinkingSize('x'.repeat(820))).toBe('820 字符');
    expect(formatThinkingSize('x'.repeat(1000))).toBe('1.0k 字符');
    expect(formatThinkingSize('x'.repeat(12832))).toBe('12.8k 字符');
  });
});

describe('splitThinkingBlocks', () => {
  it('按空行分段，段内压掉硬换行（否则上万字符糊成一段）', () => {
    expect(splitThinkingBlocks('a\nb\n\nc\nd')).toEqual(['a b', 'c d']);
  });

  it('整段都是列表时保留换行，不把结构压坏', () => {
    expect(splitThinkingBlocks('- a\n- b\n- c')).toEqual(['- a\n- b\n- c']);
  });

  it('含代码围栏的段保留换行', () => {
    expect(splitThinkingBlocks('see:\n```\nx = 1\n```')).toEqual(['see:\n```\nx = 1\n```']);
  });

  it('兼容 CRLF、折叠多余空行、丢掉空段', () => {
    expect(splitThinkingBlocks('a\r\n\r\n\r\n\r\nb')).toEqual(['a', 'b']);
    expect(splitThinkingBlocks('\n\n   \n')).toEqual([]);
    expect(splitThinkingBlocks(undefined)).toEqual([]);
  });
});

describe('splitTextLinks', () => {
  it('把 http(s) 链接从文本里摘出来（含中文/括号边界）', () => {
    const segments = splitTextLinks('见 https://github.com/settings/tokens?type=beta 去开权限');
    expect(segments).toEqual([
      { type: 'text', value: '见 ' },
      { type: 'link', value: 'https://github.com/settings/tokens?type=beta' },
      { type: 'text', value: ' 去开权限' },
    ]);
  });

  it('中文全角括号紧跟链接时不会吞进 URL', () => {
    const segments = splitTextLinks('设置页（https://github.com/settings/tokens）里改');
    expect(segments.map((segment) => segment.value)).toEqual([
      '设置页（',
      'https://github.com/settings/tokens',
      '）里改',
    ]);
  });

  it('没有链接就只回一段文本；空值回空数组', () => {
    expect(splitTextLinks('纯文本，无链接')).toEqual([{ type: 'text', value: '纯文本，无链接' }]);
    expect(splitTextLinks('')).toEqual([]);
    expect(splitTextLinks(undefined)).toEqual([]);
  });
});
