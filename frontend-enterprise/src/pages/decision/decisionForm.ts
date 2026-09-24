/**
 * 决策助手（Laya）的表单模型与结果解释。
 *
 * 纯逻辑集中在这里，页面只负责渲染 —— 便于用单测覆盖 schema 组装与结论判定。
 *
 * 上游契约要点（见 backend/app/api/laya.py 顶部注释）：
 * - 请求体字段是 `questions`（复数），key 可随机，回包按同一 key 返回；
 * - `state.background` 承载决策背景；
 * - 三种题型：noul（是非）/ choice（命名分类，criteria 是「选项名 -> 说明」）/ score（有序评分，criteria 是文案数组）。
 *
 * 选项草稿按题型分槽保存（`optionsByType`），见 `withType` 的注释。
 */

export type LayaQuestionType = 'noul' | 'choice' | 'score';

export type LayaQuestionSpec =
  | { type: 'noul'; instructions: string }
  | { type: 'choice'; instructions: string; criteria: Record<string, string> }
  | { type: 'score'; instructions: string; criteria: string[] };

export type DecisionOption = {
  id: string;
  /** choice 题型下是选项名（回包里的 choice 就取自它）；score 题型下是档位文案 */
  label: string;
};

export type DecisionQuestionDraft = {
  id: string;
  /** 发送给 Laya 的随机 key，回包靠它对上题目 */
  key: string;
  type: LayaQuestionType;
  instructions: string;
  /**
   * 每种题型各存一份选项草稿。切换题型只换「当前用哪一份」，既不把现有的选项搬过去，
   * 也不清空 —— 否则会出现「分类的选项名被填进评分档位」和「切到是非再切回来内容全丢」。
   */
  optionsByType: Record<LayaQuestionType, DecisionOption[]>;
};

export type LayaAnswer = {
  type?: LayaQuestionType;
  noul?: number;
  choice?: string;
  score?: number;
  legend?: Record<string, string>;
  probabilities?: Record<string, number>;
  confidence?: number;
  action?: { act_probability?: number };
};

export type LayaRouting = {
  model?: string | null;
  repo?: string | null;
  reason?: string | null;
  workflow?: string | null;
};

export type LayaPredictResult = {
  answers: Record<string, LayaAnswer>;
  routing?: LayaRouting | null;
  elapsed_ms?: number;
  upstream_url?: string;
};

export type DecisionDistribution = {
  label: string;
  probability: number;
  winner: boolean;
};

export type DecisionConclusion = {
  key: string;
  question: string;
  kindLabel: string;
  /** 给用户看的结论（是非题的「是/否」、分类题命中的选项名、评分题的档位文案） */
  statement: string;
  detail: string | null;
  confidence: number | null;
  confidenceLabel: string;
  lowConfidence: boolean;
  actProbability: number | null;
  distribution: DecisionDistribution[];
};

export type QuestionTypeMeta = {
  label: string;
  shortLabel: string;
  hint: string;
  questionPlaceholder: string;
  optionPlaceholder: string;
  needsOptions: boolean;
};

export const QUESTION_TYPE_META: Record<LayaQuestionType, QuestionTypeMeta> = {
  noul: {
    label: '是非（二选一）',
    shortLabel: '是非',
    hint: '只需要一个问题描述，Laya 直接给出「是 / 否」判断。',
    questionPlaceholder: '如：用户是否明确要求退款？',
    optionPlaceholder: '',
    needsOptions: false,
  },
  choice: {
    label: '选择 · 分类命中',
    shortLabel: '分类',
    hint: '每个选项填一个选项名，回包里给出命中的选项名与各选项概率。',
    questionPlaceholder: '如：这条工单应由哪个部门处理？',
    optionPlaceholder: '选项名，如 billing',
    needsOptions: true,
  },
  score: {
    label: '选择 · 程度评分',
    shortLabel: '评分',
    hint: '选项是有序档位（由轻到重），回包里给出评分期望值与各档位概率。',
    questionPlaceholder: '如：这条工单的紧急程度是多少？',
    optionPlaceholder: '档位文案，如 很急，涉及钱 / 违约 / 取消订阅',
    needsOptions: true,
  },
};

export const QUESTION_TYPE_ORDER: LayaQuestionType[] = ['noul', 'choice', 'score'];

let sequence = 0;

function nextSequence(): number {
  sequence += 1;
  return sequence;
}

function randomSuffix(length: number): string {
  const random = Math.random().toString(36).slice(2, 2 + length);
  return random.padEnd(length, '0');
}

export function randomQuestionKey(): string {
  return `q_${randomSuffix(8)}`;
}

export function createOption(label = ''): DecisionOption {
  return { id: `opt_${nextSequence()}_${randomSuffix(4)}`, label };
}

function blankOptions(type: LayaQuestionType): DecisionOption[] {
  // 是非题不需要选项；两种选择类题型各给两行空位，保证「至少 2 个选项」可直接编辑。
  return type === 'noul' ? [] : [createOption(), createOption()];
}

export function createQuestion(type: LayaQuestionType = 'noul'): DecisionQuestionDraft {
  return {
    id: `q_${nextSequence()}_${randomSuffix(4)}`,
    key: randomQuestionKey(),
    type,
    instructions: '',
    optionsByType: {
      noul: [],
      choice: type === 'choice' ? blankOptions('choice') : [],
      score: type === 'score' ? blankOptions('score') : [],
    },
  };
}

/** 当前题型生效的选项行。 */
export function optionsOf(question: DecisionQuestionDraft): DecisionOption[] {
  return question.optionsByType[question.type] ?? [];
}

/** 只写回当前题型的槽位，其它题型的草稿原样保留。 */
export function withOptions(
  question: DecisionQuestionDraft,
  options: DecisionOption[],
): DecisionQuestionDraft {
  return {
    ...question,
    optionsByType: { ...question.optionsByType, [question.type]: options },
  };
}

/**
 * 切换题型：只改 `type`，各题型的选项草稿互不迁移、互不清空。
 * 若目标题型还没有草稿（首次切过去）就补两行空位；已有草稿则原样恢复。
 */
export function withType(question: DecisionQuestionDraft, type: LayaQuestionType): DecisionQuestionDraft {
  if (type === question.type) return question;
  const optionsByType = { ...question.optionsByType };
  const existing = optionsByType[type] ?? [];
  if (type !== 'noul' && existing.length < 2) {
    optionsByType[type] = [...existing, ...blankOptions(type).slice(existing.length)];
  }
  return { ...question, type, optionsByType };
}

const MAX_INSTRUCTIONS = 2000;

export function validateDraft(background: string, questions: DecisionQuestionDraft[]): string | null {
  if (!background.trim()) return '请先填写决策背景';
  if (questions.length === 0) return '至少添加一条决策内容';
  for (const [index, question] of questions.entries()) {
    const label = `第 ${index + 1} 条决策内容`;
    if (!question.instructions.trim()) return `${label}：请填写问题描述`;
    if (question.instructions.length > MAX_INSTRUCTIONS) return `${label}：问题描述过长`;
    if (!QUESTION_TYPE_META[question.type].needsOptions) continue;
    const filled = optionsOf(question).filter((option) => option.label.trim());
    if (filled.length < 2) return `${label}：至少需要 2 个选项`;
    if (question.type === 'choice') {
      const labels = filled.map((option) => option.label.trim());
      if (new Set(labels).size !== labels.length) return `${label}：选项名不能重复`;
    }
  }
  return null;
}

export function buildQuestions(
  questions: DecisionQuestionDraft[],
): Record<string, LayaQuestionSpec> {
  const payload: Record<string, LayaQuestionSpec> = {};
  for (const question of questions) {
    const instructions = question.instructions.trim();
    if (question.type === 'noul') {
      payload[question.key] = { type: 'noul', instructions };
      continue;
    }
    const filled = optionsOf(question).filter((option) => option.label.trim());
    if (question.type === 'choice') {
      // criteria 的值是「该选项的适用说明」，页面不再单独收集，用选项名自身兜底。
      payload[question.key] = {
        type: 'choice',
        instructions,
        criteria: Object.fromEntries(filled.map((option) => {
          const name = option.label.trim();
          return [name, name];
        })),
      };
      continue;
    }
    payload[question.key] = {
      type: 'score',
      instructions,
      criteria: filled.map((option) => option.label.trim()),
    };
  }
  return payload;
}

export function confidenceLabel(confidence: number | null): string {
  if (confidence === null) return '未知';
  if (confidence >= 0.8) return '高';
  if (confidence >= 0.6) return '中';
  return '低';
}

function clampProbability(value: unknown): number {
  const numeric = typeof value === 'number' && Number.isFinite(value) ? value : 0;
  return Math.min(1, Math.max(0, numeric));
}

function probabilityFrom(answer: LayaAnswer, key: string): number | null {
  const raw = answer.probabilities?.[key];
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null;
  return clampProbability(raw);
}

function conclusionFrom(
  question: DecisionQuestionDraft,
  answer: LayaAnswer | undefined,
): DecisionConclusion {
  const meta = QUESTION_TYPE_META[question.type];
  const base = {
    key: question.key,
    question: question.instructions.trim(),
    kindLabel: meta.shortLabel,
    confidence: null as number | null,
    confidenceLabel: '未知',
    lowConfidence: false,
    actProbability: null as number | null,
    distribution: [] as DecisionDistribution[],
  };

  if (!answer) {
    return { ...base, statement: '上游未返回该题结论', detail: '可重试一次；仍为空时请检查问题描述。' };
  }

  const reportedConfidence =
    typeof answer.confidence === 'number' && Number.isFinite(answer.confidence)
      ? clampProbability(answer.confidence)
      : null;

  if (question.type === 'noul') {
    const probability = typeof answer.noul === 'number' && Number.isFinite(answer.noul)
      ? clampProbability(answer.noul)
      : null;
    if (probability === null) {
      return { ...base, statement: '上游未返回该题结论', detail: '答案缺少 noul 字段。' };
    }
    const hit = probability >= 0.5;
    const confidence = reportedConfidence ?? Math.max(probability, 1 - probability);
    return {
      ...base,
      statement: hit ? '是' : '否',
      detail: `p=${probability.toFixed(4)}`,
      confidence,
      confidenceLabel: confidenceLabel(confidence),
      lowConfidence: confidence < 0.6,
      actProbability:
        typeof answer.action?.act_probability === 'number' ? clampProbability(answer.action.act_probability) : null,
      distribution: [
        { label: '是', probability, winner: hit },
        { label: '否', probability: 1 - probability, winner: !hit },
      ],
    };
  }

  if (question.type === 'choice') {
    const hitKey = typeof answer.choice === 'string' ? answer.choice : '';
    const filled = optionsOf(question).filter((option) => option.label.trim());
    const hitOption = filled.find((option) => option.label.trim() === hitKey);
    const confidence = reportedConfidence ?? probabilityFrom(answer, hitKey) ?? null;
    return {
      ...base,
      // 选项不再配「适用说明」，结论正文直接用命中的选项名。
      statement: hitOption ? hitOption.label.trim() : hitKey || '未给出选项',
      detail: hitKey ? `命中选项 ${hitKey}` : '答案缺少 choice 字段。',
      confidence,
      confidenceLabel: confidenceLabel(confidence),
      lowConfidence: confidence !== null && confidence < 0.6,
      actProbability:
        typeof answer.action?.act_probability === 'number' ? clampProbability(answer.action.act_probability) : null,
      distribution: filled.map((option) => {
        const label = option.label.trim();
        return {
          label,
          probability: probabilityFrom(answer, label) ?? 0,
          winner: label === hitKey,
        };
      }),
    };
  }

  const legend = answer.legend ?? {};
  const probabilityKeys = Object.keys(answer.probabilities ?? {}).filter((key) => /^\d+$/.test(key));
  const orderedKeys =
    probabilityKeys.length > 0
      ? probabilityKeys.sort((left, right) => Number(left) - Number(right))
      : Object.keys(legend);
  const distribution = orderedKeys.map((key) => ({
    label: legend[key] ?? `第 ${Number(key) + 1} 档`,
    probability: probabilityFrom(answer, key) ?? 0,
    winner: false,
  }));
  let winnerIndex = -1;
  distribution.forEach((item, index) => {
    if (winnerIndex === -1 || item.probability > distribution[winnerIndex].probability) winnerIndex = index;
  });
  if (winnerIndex >= 0) distribution[winnerIndex] = { ...distribution[winnerIndex], winner: true };

  const score = typeof answer.score === 'number' && Number.isFinite(answer.score) ? answer.score : null;
  const confidence = reportedConfidence ?? (winnerIndex >= 0 ? distribution[winnerIndex].probability : null);
  const statement =
    winnerIndex >= 0 ? distribution[winnerIndex].label : '未给出档位';
  return {
    ...base,
    statement,
    detail:
      score === null
        ? '答案缺少 score 字段。'
        : `评分 ${score.toFixed(4)}${distribution.length > 1 ? ` · 共 ${distribution.length} 档` : ''}`,
    confidence,
    confidenceLabel: confidenceLabel(confidence),
    lowConfidence: confidence !== null && confidence < 0.6,
    actProbability:
      typeof answer.action?.act_probability === 'number' ? clampProbability(answer.action.act_probability) : null,
    distribution,
  };
}

/** 按表单顺序把回包解释成结论列表；上游多返回的 key 会追加在末尾，避免静默丢失。 */
export function interpretResult(
  questions: DecisionQuestionDraft[],
  result: LayaPredictResult,
): DecisionConclusion[] {
  const conclusions = questions.map((question) =>
    conclusionFrom(question, result.answers?.[question.key]),
  );
  const knownKeys = new Set(questions.map((question) => question.key));
  const extras = Object.keys(result.answers ?? {}).filter((key) => !knownKeys.has(key));
  for (const key of extras) {
    const answer = result.answers[key];
    const type: LayaQuestionType =
      answer?.type === 'choice' || answer?.type === 'score' || answer?.type === 'noul'
        ? answer.type
        : 'noul';
    conclusions.push(
      conclusionFrom(
        { id: key, key, type, instructions: '（表单外的结论）', optionsByType: { noul: [], choice: [], score: [] } },
        answer,
      ),
    );
  }
  return conclusions;
}

/** 示例：复刻 Laya 官方 TICKET_QUESTIONS，用来一键试跑 / 调试。 */
export function sampleQuestions(): { background: string; questions: DecisionQuestionDraft[] } {
  const question = (
    type: LayaQuestionType,
    instructions: string,
    options: string[],
  ): DecisionQuestionDraft => ({
    ...createQuestion(type),
    instructions,
    optionsByType: {
      noul: [],
      choice: type === 'choice' ? options.map((label) => createOption(label)) : [],
      score: type === 'score' ? options.map((label) => createOption(label)) : [],
    },
  });

  return {
    background:
      '客户来电反馈本月账单被重复扣款两次，情绪激动，明确要求退款，并表示如果今天之内没有答复就会向消协投诉并取消订阅。',
    questions: [
      question('choice', '这条工单应由哪个部门处理？', ['billing', 'technical', 'sales', 'other']),
      question('score', '这条工单的紧急程度是多少？', [
        '不急，普通咨询',
        '较急，影响使用但可等待',
        '很急，涉及钱 / 违约 / 取消订阅',
        '立刻处理，已有投诉或舆情风险',
      ]),
      question('noul', '用户是否明确要求退款？', []),
      question('noul', '用户是否表达取消、退订、流失意图？', []),
    ],
  };
}

/** 后端同样限制 30 个问题，导入时先卡住，避免白跑一次请求。 */
const MAX_IMPORTED_QUESTIONS = 30;

export type DecisionImportResult =
  | { ok: true; background: string; questions: DecisionQuestionDraft[]; warning: string | null }
  | { ok: false; message: string };

function importedOptions(labels: string[], type: LayaQuestionType): DecisionOption[] {
  const options = labels.map((label) => createOption(label));
  // 选择类题型至少要两个选项，缺的补成空行交给用户填，避免导入后立刻校验失败
  if (type !== 'noul') {
    while (options.length < 2) options.push(createOption());
  }
  return options;
}

/**
 * 解析「导入 JSON」文件：结构与发给 Laya 的请求体一致
 * （`{ state: { background }, questions: { "<key>": { type, instructions, criteria } } }`）。
 * 导入时**保留文件里的 key**，这样回包与原文件的 key 一一对应，便于脚本比对。
 */
export function parseDecisionJson(raw: string): DecisionImportResult {
  const invalidJson = 'JSON 解析失败，请检查文件内容是否为合法 JSON。';
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { ok: false, message: invalidJson };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, message: invalidJson };
  }

  const root = parsed as Record<string, unknown>;
  const state =
    root.state && typeof root.state === 'object' && !Array.isArray(root.state)
      ? (root.state as Record<string, unknown>)
      : {};
  const backgroundRaw = state.background ?? root.background;
  const background = typeof backgroundRaw === 'string' ? backgroundRaw.trim() : '';
  if (!background) {
    return { ok: false, message: '导入的 JSON 缺少决策背景：请提供 state.background。' };
  }

  const missingQuestions = '导入的 JSON 缺少 questions 字段（应为「问题 key → 问题定义」的对象）。';
  const rawQuestions = root.questions;
  if (!rawQuestions || typeof rawQuestions !== 'object' || Array.isArray(rawQuestions)) {
    return { ok: false, message: missingQuestions };
  }
  const entries = Object.entries(rawQuestions as Record<string, unknown>);
  if (entries.length === 0) {
    return { ok: false, message: missingQuestions };
  }
  if (entries.length > MAX_IMPORTED_QUESTIONS) {
    return { ok: false, message: '导入的问题不能超过 30 个。' };
  }

  const questions: DecisionQuestionDraft[] = [];
  let droppedDescription = false;

  for (const [index, [rawKey, rawSpec]] of entries.entries()) {
    const position = index + 1;
    const key = rawKey.trim() || randomQuestionKey();
    if (!rawSpec || typeof rawSpec !== 'object' || Array.isArray(rawSpec)) {
      return { ok: false, message: `第 ${position} 个问题（${key}）的定义必须是对象。` };
    }
    const spec = rawSpec as Record<string, unknown>;
    const type = spec.type;
    if (type !== 'noul' && type !== 'choice' && type !== 'score') {
      return { ok: false, message: `第 ${position} 个问题（${key}）的 type 必须是 noul / choice / score。` };
    }
    const instructions = typeof spec.instructions === 'string' ? spec.instructions.trim() : '';
    if (!instructions) {
      return { ok: false, message: `第 ${position} 个问题（${key}）缺少 instructions（问题描述）。` };
    }

    let labels: string[] = [];
    if (type === 'choice') {
      const criteria = spec.criteria;
      if (Array.isArray(criteria)) {
        labels = criteria.map((item) => String(item ?? '').trim()).filter(Boolean);
      } else if (criteria && typeof criteria === 'object') {
        const record = criteria as Record<string, unknown>;
        labels = Object.keys(record).map((name) => name.trim()).filter(Boolean);
        if (
          Object.entries(record).some(
            ([name, value]) =>
              typeof value === 'string' && value.trim() !== '' && value.trim() !== name.trim(),
          )
        ) {
          droppedDescription = true;
        }
      }
      if (labels.length === 0) {
        return {
          ok: false,
          message: `第 ${position} 个问题（${key}）的 choice 类型需要 criteria 对象（选项名 → 说明）。`,
        };
      }
    }
    if (type === 'score') {
      const criteria = spec.criteria;
      if (Array.isArray(criteria)) {
        labels = criteria.map((item) => String(item ?? '').trim()).filter(Boolean);
      } else if (criteria && typeof criteria === 'object') {
        labels = Object.values(criteria as Record<string, unknown>)
          .map((item) => String(item ?? '').trim())
          .filter(Boolean);
      }
      if (labels.length === 0) {
        return {
          ok: false,
          message: `第 ${position} 个问题（${key}）的 score 类型需要 criteria 数组（档位文案，由轻到重）。`,
        };
      }
    }

    questions.push({
      id: `q_${nextSequence()}_${randomSuffix(4)}`,
      key,
      type,
      instructions,
      optionsByType: {
        noul: [],
        choice: type === 'choice' ? importedOptions(labels, 'choice') : [],
        score: type === 'score' ? importedOptions(labels, 'score') : [],
      },
    });
  }

  return {
    ok: true,
    background,
    questions,
    warning: droppedDescription
      ? '分类题只保留选项名：页面不收集「适用说明」，判定按选项名进行。'
      : null,
  };
}
