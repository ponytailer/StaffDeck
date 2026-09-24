import { describe, expect, it } from 'vitest';

import {
  buildQuestions,
  createOption,
  createQuestion,
  interpretResult,
  optionsOf,
  parseDecisionJson,
  sampleQuestions,
  validateDraft,
  withOptions,
  withType,
  type DecisionQuestionDraft,
} from './decisionForm';

function draft(
  type: DecisionQuestionDraft['type'],
  instructions: string,
  options: string[] = [],
): DecisionQuestionDraft {
  const base = createQuestion(type);
  return {
    ...base,
    instructions,
    optionsByType: { ...base.optionsByType, [type]: options.map((label) => createOption(label)) },
  };
}

describe('buildQuestions', () => {
  it('maps 是非 题 to a noul spec without criteria', () => {
    const questions = { ...createQuestion('noul'), instructions: ' 用户是否明确要求退款？ ' };
    const payload = buildQuestions([questions]);
    expect(payload[questions.key]).toEqual({
      type: 'noul',
      instructions: '用户是否明确要求退款？',
    });
  });

  it('maps 分类 题 to criteria keyed by option name', () => {
    const question = draft('choice', '这条工单应由哪个部门处理？', ['billing', 'technical']);
    const payload = buildQuestions([question]);
    expect(payload[question.key]).toEqual({
      type: 'choice',
      instructions: '这条工单应由哪个部门处理？',
      criteria: { billing: 'billing', technical: 'technical' },
    });
  });

  it('maps 评分 题 to an ordered criteria array and drops blank rows', () => {
    const question = draft('score', '这条工单的紧急程度是多少？', [
      '不急，普通咨询',
      '',
      '立刻处理，已有投诉风险',
    ]);
    const payload = buildQuestions([question]);
    expect(payload[question.key]).toEqual({
      type: 'score',
      instructions: '这条工单的紧急程度是多少？',
      criteria: ['不急，普通咨询', '立刻处理，已有投诉风险'],
    });
  });

  it('keeps the response key aligned with the request key', () => {
    const question = createQuestion('noul');
    const payload = buildQuestions([{ ...question, instructions: '是否？' }]);
    expect(Object.keys(payload)).toEqual([question.key]);
    expect(question.key).toMatch(/^q_[a-z0-9]{8}$/);
  });
});

describe('validateDraft', () => {
  it('accepts a well-formed draft of every type', () => {
    expect(
      validateDraft('背景描述', [
        draft('noul', '用户是否明确要求退款？'),
        draft('choice', '哪个部门？', ['billing', 'technical']),
        draft('score', '多急？', ['不急', '很急']),
      ]),
    ).toBeNull();
  });

  it('requires a background and at least one question', () => {
    expect(validateDraft('   ', [draft('noul', '是否？')])).toBe('请先填写决策背景');
    expect(validateDraft('背景', [])).toBe('至少添加一条决策内容');
  });

  it('requires instructions and at least two filled options', () => {
    expect(validateDraft('背景', [draft('noul', '  ')])).toBe('第 1 条决策内容：请填写问题描述');
    expect(validateDraft('背景', [draft('score', '多急？', ['不急'])])).toBe(
      '第 1 条决策内容：至少需要 2 个选项',
    );
    expect(validateDraft('背景', [draft('choice', '哪个部门？', ['billing'])])).toBe(
      '第 1 条决策内容：至少需要 2 个选项',
    );
  });

  it('rejects duplicated choice option names', () => {
    expect(validateDraft('背景', [draft('choice', '哪个部门？', ['billing', 'billing'])])).toBe(
      '第 1 条决策内容：选项名不能重复',
    );
  });
});

describe('withType', () => {
  it('seeds two blank options when a flavour is first used', () => {
    const noul = draft('noul', '是否？');
    const asChoice = withType(noul, 'choice');
    expect(optionsOf(asChoice)).toHaveLength(2);
    expect(optionsOf(asChoice).every((option) => option.label === '')).toBe(true);
    expect(optionsOf(withType(asChoice, 'noul'))).toEqual([]);
  });

  it('reuses the existing draft instead of reseeding it on every switch', () => {
    const asChoice = withType(draft('noul', '是否？'), 'choice');
    const refilled = withOptions(asChoice, [createOption('billing'), createOption('technical')]);
    const backAndForth = withType(withType(withType(refilled, 'score'), 'noul'), 'choice');
    expect(optionsOf(backAndForth).map((option) => option.label)).toEqual(['billing', 'technical']);
  });

  it('never migrates 分类 options into 评分 (and vice versa)', () => {
    const choice = draft('choice', '哪个部门？', ['billing', 'technical']);
    const asScore = withType(choice, 'score');
    // 评分槽是空的 → 只补两行空位，不搬分类的选项名
    expect(optionsOf(asScore).map((option) => option.label)).toEqual(['', '']);
    // 分类自己的草稿原样保留
    expect(asScore.optionsByType.choice.map((option) => option.label)).toEqual(['billing', 'technical']);
  });

  it('keeps both drafts alive across 是非 so nothing is lost when switching back', () => {
    const choice = draft('choice', '哪个部门？', ['billing', 'technical']);
    const scored = withOptions(withType(choice, 'score'), [createOption('不急'), createOption('很急')]);

    const asNoul = withType(scored, 'noul');
    expect(optionsOf(asNoul)).toEqual([]);

    expect(withType(asNoul, 'score').optionsByType.score.map((option) => option.label)).toEqual([
      '不急',
      '很急',
    ]);
    expect(withType(withType(asNoul, 'score'), 'choice').optionsByType.choice.map((o) => o.label)).toEqual([
      'billing',
      'technical',
    ]);
  });

  it('is a no-op when the type does not change', () => {
    const question = draft('choice', '哪个部门？', ['billing', 'technical']);
    expect(withType(question, 'choice')).toBe(question);
  });
});

describe('interpretResult', () => {
  it('reads a 是非 answer from the noul probability', () => {
    const question = draft('noul', '用户是否明确要求退款？');
    const [conclusion] = interpretResult([question], {
      answers: { [question.key]: { type: 'noul', noul: 0.9961, confidence: 0.9961 } },
    });
    expect(conclusion.statement).toBe('是');
    expect(conclusion.kindLabel).toBe('是非');
    expect(conclusion.confidence).toBeCloseTo(0.9961, 4);
    expect(conclusion.confidenceLabel).toBe('高');
    expect(conclusion.lowConfidence).toBe(false);
    expect(conclusion.distribution.map((item) => [item.label, item.winner])).toEqual([
      ['是', true],
      ['否', false],
    ]);
    // 1 - 0.9961 存在浮点误差，按精度比较
    expect(conclusion.distribution[1].probability).toBeCloseTo(0.0039, 6);
  });

  it('reports 否 and flags a low-confidence verdict', () => {
    const question = draft('noul', '用户是否表达流失意图？');
    const [conclusion] = interpretResult([question], {
      answers: { [question.key]: { type: 'noul', noul: 0.45, confidence: 0.55 } },
    });
    expect(conclusion.statement).toBe('否');
    expect(conclusion.confidenceLabel).toBe('低');
    expect(conclusion.lowConfidence).toBe(true);
  });

  it('falls back to max(p, 1-p) when confidence is absent', () => {
    const question = draft('noul', '是否？');
    const [conclusion] = interpretResult([question], {
      answers: { [question.key]: { type: 'noul', noul: 0.04 } },
    });
    expect(conclusion.confidence).toBeCloseTo(0.96, 4);
    expect(conclusion.confidenceLabel).toBe('高');
  });

  it('surfaces the matched choice name and marks the winner in the form order', () => {
    const question = draft('choice', '这条工单应由哪个部门处理？', ['billing', 'technical']);
    const [conclusion] = interpretResult([question], {
      answers: {
        [question.key]: {
          type: 'choice',
          choice: 'billing',
          probabilities: { billing: 0.9949, technical: 0.0051 },
          confidence: 0.9738,
          action: { act_probability: 1 },
        },
      },
    });
    expect(conclusion.statement).toBe('billing');
    expect(conclusion.detail).toBe('命中选项 billing');
    expect(conclusion.actProbability).toBe(1);
    expect(conclusion.distribution.map((item) => item.label)).toEqual(['billing', 'technical']);
    expect(conclusion.distribution[0].winner).toBe(true);
    expect(conclusion.distribution[1].winner).toBe(false);
  });

  it('picks the level with the highest probability for 评分 题', () => {
    const question = draft('score', '这条工单的紧急程度是多少？', [
      '不急，普通咨询',
      '较急，影响使用但可等待',
      '很急，涉及钱 / 违约 / 取消订阅',
      '立刻处理，已有投诉或舆情风险',
    ]);
    const [conclusion] = interpretResult([question], {
      answers: {
        [question.key]: {
          type: 'score',
          score: 2.9464,
          legend: {
            0: '不急，普通咨询',
            1: '较急，影响使用但可等待',
            2: '很急，涉及钱 / 违约 / 取消订阅',
            3: '立刻处理，已有投诉或舆情风险',
          },
          probabilities: { 0: 0.0017, 1: 0.0137, 2: 0.021, 3: 0.9635 },
          confidence: 0.8653,
        },
      },
    });
    expect(conclusion.statement).toBe('立刻处理，已有投诉或舆情风险');
    expect(conclusion.detail).toBe('评分 2.9464 · 共 4 档');
    expect(conclusion.distribution.map((item) => item.label)).toEqual([
      '不急，普通咨询',
      '较急，影响使用但可等待',
      '很急，涉及钱 / 违约 / 取消订阅',
      '立刻处理，已有投诉或舆情风险',
    ]);
    expect(conclusion.distribution[3].winner).toBe(true);
  });

  it('orders score probabilities numerically even when keys arrive unsorted', () => {
    const question = draft('score', '多急？', ['轻', '中', '重']);
    const [conclusion] = interpretResult([question], {
      answers: {
        [question.key]: {
          type: 'score',
          score: 1,
          legend: { 0: '轻', 1: '中', 2: '重' },
          probabilities: { 2: 0.1, 0: 0.2, 1: 0.7 },
        },
      },
    });
    expect(conclusion.statement).toBe('中');
    expect(conclusion.distribution.map((item) => item.label)).toEqual(['轻', '中', '重']);
  });

  it('degrades gracefully when an answer is missing or incomplete', () => {
    const missing = draft('noul', '是否？');
    const broken = draft('score', '多急？', ['轻', '重']);
    const conclusions = interpretResult([missing, broken], { answers: {} });
    expect(conclusions[0].statement).toBe('上游未返回该题结论');
    expect(conclusions[1].statement).toBe('上游未返回该题结论');
    expect(conclusions[1].confidenceLabel).toBe('未知');

    const [partial] = interpretResult([broken], {
      answers: { [broken.key]: { type: 'score' } },
    });
    expect(partial.statement).toBe('未给出档位');
    expect(partial.detail).toBe('答案缺少 score 字段。');
  });

  it('appends answers for keys outside the form instead of dropping them', () => {
    const question = draft('noul', '是否？');
    const conclusions = interpretResult([question], {
      answers: {
        [question.key]: { type: 'noul', noul: 0.9 },
        extra_key: { type: 'noul', noul: 0.1 },
      },
    });
    expect(conclusions).toHaveLength(2);
    expect(conclusions[1].key).toBe('extra_key');
    expect(conclusions[1].question).toBe('（表单外的结论）');
  });
});

describe('sampleQuestions', () => {
  it('reproduces the official ticket schema shape', () => {
    const sample = sampleQuestions();
    expect(sample.questions.map((question) => question.type)).toEqual(['choice', 'score', 'noul', 'noul']);
    const payload = buildQuestions(sample.questions);
    const specs = Object.values(payload);
    expect(specs[0]).toMatchObject({ type: 'choice', criteria: { other: 'other' } });
    expect(specs[1]).toMatchObject({ type: 'score' });
    expect(specs[1]).toHaveProperty('criteria.3', '立刻处理，已有投诉或舆情风险');
    expect(specs[2]).toEqual({ type: 'noul', instructions: '用户是否明确要求退款？' });
    expect(validateDraft(sample.background, sample.questions)).toBeNull();
  });
});

describe('parseDecisionJson', () => {
  const payload = {
    state: { background: '客户要求退款并威胁投诉' },
    questions: {
      refund_requested: { type: 'noul', instructions: '用户是否明确要求退款？' },
      department: {
        type: 'choice',
        instructions: '这条工单应由哪个部门处理？',
        criteria: { billing: '账单、扣款', technical: 'Bug、报错' },
      },
      urgency: {
        type: 'score',
        instructions: '这条工单的紧急程度是多少？',
        criteria: ['不急', '很急'],
      },
    },
  };

  it('rebuilds the form draft and keeps the keys from the file', () => {
    const result = parseDecisionJson(JSON.stringify(payload));
    expect(result.ok).toBe(true);
    if (!result.ok) return;

    expect(result.background).toBe('客户要求退款并威胁投诉');
    expect(result.questions.map((question) => question.key)).toEqual([
      'refund_requested',
      'department',
      'urgency',
    ]);

    const [noul, choice, score] = result.questions;
    expect(noul.type).toBe('noul');
    expect(noul.optionsByType.noul).toEqual([]);

    expect(choice.type).toBe('choice');
    expect(optionsOf(choice).map((option) => option.label)).toEqual(['billing', 'technical']);

    expect(score.type).toBe('score');
    expect(optionsOf(score).map((option) => option.label)).toEqual(['不急', '很急']);

    // 导入结果可以直接提交：分槽草稿 + 校验都通过
    expect(validateDraft(result.background, result.questions)).toBeNull();
    expect(Object.keys(buildQuestions(result.questions))).toHaveLength(3);
  });

  it('warns when classification descriptions are dropped', () => {
    const result = parseDecisionJson(JSON.stringify(payload));
    expect(result.ok && result.warning).toContain('分类题只保留选项名');
  });

  it('accepts a bare string array for choice criteria and pads to two rows', () => {
    const result = parseDecisionJson(
      JSON.stringify({
        state: { background: 'bg' },
        questions: { q1: { type: 'choice', instructions: '选哪个？', criteria: ['甲'] } },
      }),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(optionsOf(result.questions[0]).map((option) => option.label)).toEqual(['甲', '']);
    expect(result.warning).toBeNull();
  });

  it('accepts criteria given as an object for score questions (keeps the values)', () => {
    const result = parseDecisionJson(
      JSON.stringify({
        state: { background: 'bg' },
        questions: {
          q1: { type: 'score', instructions: '多急？', criteria: { low: '不急', high: '很急' } },
        },
      }),
    );
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(optionsOf(result.questions[0]).map((option) => option.label)).toEqual(['不急', '很急']);
  });

  it('rejects malformed inputs with a readable reason', () => {
    const cases: [unknown, string][] = [
      ['{ not json', 'JSON 解析失败'],
      [JSON.stringify({ questions: { q1: { type: 'noul', instructions: 'x' } } }), '缺少决策背景'],
      [JSON.stringify({ state: { background: 'bg' } }), '缺少 questions'],
      [JSON.stringify({ state: { background: 'bg' }, questions: {} }), '缺少 questions'],
      [
        JSON.stringify({ state: { background: 'bg' }, questions: { q1: { type: 'rating', instructions: 'x' } } }),
        'type 必须是',
      ],
      [
        JSON.stringify({ state: { background: 'bg' }, questions: { q1: { type: 'noul', instructions: '  ' } } }),
        '缺少 instructions',
      ],
      [
        JSON.stringify({ state: { background: 'bg' }, questions: { q1: { type: 'choice', instructions: 'x', criteria: {} } } }),
        'choice 类型需要 criteria',
      ],
      [
        JSON.stringify({ state: { background: 'bg' }, questions: { q1: { type: 'score', instructions: 'x' } } }),
        'score 类型需要 criteria',
      ],
    ];

    for (const [input, fragment] of cases) {
      const result = parseDecisionJson(typeof input === 'string' ? input : JSON.stringify(input));
      expect(result.ok).toBe(false);
      if (result.ok) continue;
      expect(result.message).toContain(fragment);
    }
  });

  it('refuses more than 30 questions', () => {
    const questions = Object.fromEntries(
      Array.from({ length: 31 }, (_, index) => [
        `q_${index}`,
        { type: 'noul', instructions: `问题 ${index}` },
      ]),
    );
    const result = parseDecisionJson(JSON.stringify({ state: { background: 'bg' }, questions }));
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.message).toContain('不能超过 30 个');
  });
});
