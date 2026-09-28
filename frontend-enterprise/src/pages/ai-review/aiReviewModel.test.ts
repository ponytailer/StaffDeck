import { describe, expect, it } from 'vitest';

import {
  commentRange,
  formatElapsed,
  formatTokens,
  isOcrSkipped,
  ocrSkipHint,
  ocrStatusMeta,
  pageNumbers,
  sampleRuleFileText,
  sortComments,
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
