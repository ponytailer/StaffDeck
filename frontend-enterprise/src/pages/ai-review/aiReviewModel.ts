/** AI Reviewer 页面的纯展示逻辑：状态文案、耗时格式化、评论排序。 */

import type { AiReviewComment, AiReviewTaskStatus } from '../../api/aiReview';

export const TASK_STATUS_META: Record<
  AiReviewTaskStatus,
  { label: string; tone: string; dot: string }
> = {
  queued: {
    label: '排队中',
    tone: 'bg-[#f3f4f6] text-[#757f9c]',
    dot: 'bg-[#a3aaba]',
  },
  running: {
    label: '评审中',
    tone: 'bg-[#e8f0ff] text-[#1a71ff]',
    dot: 'bg-[#1a71ff] animate-pulse',
  },
  succeeded: {
    label: '已完成',
    tone: 'bg-[#e9f7ef] text-[#1a7f4b]',
    dot: 'bg-[#1a7f4b]',
  },
  failed: {
    label: '失败',
    tone: 'bg-[#fce7e7] text-[#c0392b]',
    dot: 'bg-[#c0392b]',
  },
};

export const PLATFORM_META: Record<string, { label: string; tone: string }> = {
  github: { label: 'GitHub', tone: 'bg-[#f3f4f6] text-[#464c5e]' },
  gitlab: { label: 'GitLab', tone: 'bg-[#fff7e8] text-[#8a4b00]' },
};

/**
 * ocr 自身的执行结论（summary.ocr_status），和后端作业状态是两回事：
 * 作业 succeeded 只代表「ocr 命令跑完了」，它仍可能报 skipped（diff 里没有可评审文件）。
 * 这里把 skipped 译成人话，避免用户误以为是故障。
 */
export type OcrStatusMeta = { label: string; tone: string; dot: string };

export function ocrStatusMeta(status: string | undefined | null): OcrStatusMeta | null {
  const key = (status ?? '').trim().toLowerCase();
  if (!key) return null;
  if (key === 'skipped') {
    return { label: '无可评审文件（已跳过）', tone: 'bg-[#fff7e8] text-[#8a4b00]', dot: 'bg-[#d98b1f]' };
  }
  if (['succeeded', 'success', 'completed', 'done', 'ok'].includes(key)) {
    return { label: '已完成', tone: 'bg-[#e9f7ef] text-[#1a7f4b]', dot: 'bg-[#1a7f4b]' };
  }
  if (['failed', 'error'].includes(key)) {
    return { label: '失败', tone: 'bg-[#fce7e7] text-[#c0392b]', dot: 'bg-[#c0392b]' };
  }
  if (['running', 'in_progress', 'in-progress'].includes(key)) {
    return { label: '评审中', tone: 'bg-[#e8f0ff] text-[#1a71ff]', dot: 'bg-[#1a71ff] animate-pulse' };
  }
  return { label: key, tone: 'bg-[#f3f4f6] text-[#757f9c]', dot: 'bg-[#a3aaba]' };
}

/** ocr 是否因为「没有可评审文件」而跳过。 */
export function isOcrSkipped(status: string | undefined | null): boolean {
  return (status ?? '').trim().toLowerCase() === 'skipped';
}

/**
 * skipped 的成因推断 + 处置建议。ocr 的 JSON 里只有「没有可评审文件」这一句，
 * 具体原因要靠上下文还原，所以这里按最常见的三种情况列出可核对的线索。
 */
export function ocrSkipHint(
  status: string | undefined | null,
  summary?: { files_reviewed?: number | null; source_branch?: string; target_branch?: string } | null,
): { headline: string; reasons: string[]; advice: string } | null {
  if (!isOcrSkipped(status)) return null;
  const reasons = [
    'diff 为空：源分支与目标分支指向同一提交（例如从 main 开到 main 的 PR，或已合并后 head 分支被删除）。',
    '变更文件类型不在 ocr 默认白名单：.txt / .md / .lock / 图片、二进制等默认不参与评审。',
    '变更文件被规则过滤掉：命中自定义规则文件的 exclude，或落在默认排除目录（tests/、node_modules/、dist/ 等）。',
  ];
  const sameBranch =
    summary?.source_branch && summary?.target_branch && summary.source_branch === summary.target_branch;
  if (sameBranch) {
    // 同一分支是确定性的空 diff，放到第一条并标注
    reasons[0] = `源分支与目标分支相同（均为 ${summary?.target_branch}），diff 恒为空。`;
  }
  return {
    headline: `ocr 没有找到可评审的文件，本次未产出任何评审意见（实际评审文件数 ${summary?.files_reviewed ?? 0}）。`,
    reasons,
    advice:
      '想让被白名单挡掉的文件也参与评审，可在「自定义评审规则」的 include 里加对应 glob（如 **/requirements.txt、**/*.md）；若 diff 本身为空，说明这次变更没有可评审内容，可忽略。',
  };
}

/** rq 任务是分钟级的，这里把状态映射成轮询间隔：活跃任务 3s，其余 10s。 */
export function taskPollingInterval(tasks: { status: AiReviewTaskStatus }[]): number {
  const active = tasks.some((task) => task.status === 'queued' || task.status === 'running');
  return active ? 3000 : 10000;
}

/* ------------------------------------------------------------------ *
 * 同一 PR/MR 只允许一个进行中任务
 * ------------------------------------------------------------------ */

/**
 * 是否处于「进行中」（排队 / 执行）。这类任务会占住该 PR/MR 的评审位：
 * 后端在创建与重试时都会拒掉重复提交，前端据此提前把入口禁掉。
 */
export function isTaskInFlight(status: string | undefined | null): boolean {
  return status === 'queued' || status === 'running';
}

/** 进行中阶段的中文名（口径与 TASK_STATUS_META 一致）。 */
export function inFlightPhaseLabel(status: string | undefined | null): string {
  return status === 'queued' ? '排队中' : '评审中';
}

/** 同一 PR/MR 已有进行中任务时的拦截文案（{1}=编号，{2}=阶段）。 */
export function duplicateReviewBlockedText(mrNumber: number, status?: string | null): string {
  return `PR/MR #${mrNumber} 已有进行中的评审任务（${inFlightPhaseLabel(
    status,
  )}），同一 PR/MR 同时只能有一个评审任务，请等它结束后再发起。`;
}

/** 拦截面板里给出的两条处置路径。 */
export function duplicateReviewAdvice(): string[] {
  return [
    '任务交给后台队列跑，通常几分钟，完成后状态自动刷新，该 PR/MR 的「发起评审」就会恢复可用。',
    '如果任务长时间卡在同一个状态，先到「评审任务」列表里把它删除或重试，再发起新的评审。',
  ];
}

/** ocr 的 elapsed 可能是秒数或带单位的字符串，统一成可读文本。 */
export function formatElapsed(value: number | string | undefined): string {
  if (value === undefined || value === null || value === '') return '';
  const seconds = typeof value === 'string' ? Number(value) : value;
  if (Number.isFinite(seconds)) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    return `${Math.floor(seconds / 60)}m${Math.round(seconds % 60)}s`;
  }
  return String(value);
}

export function formatTokens(value: number | undefined): string {
  if (value === undefined || value === null) return '-';
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

/** 行区间文案：null 视为整文件级建议。 */
export function commentRange(comment: AiReviewComment): string {
  const start = comment.start_line ?? comment.end_line;
  const end = comment.end_line ?? comment.start_line;
  if (start === undefined || start === null) return '整体';
  if (end === null || end === start) return `L${start}`;
  return `L${start}-L${end}`;
}

/**
 * 评论排序：先按文件路径分组，文件内按起始行升序；无路径的排最前。
 * ocr 的输出顺序是代理逐文件读的，这里排一次让它更接近「评审清单」。
 */
export function sortComments(comments: AiReviewComment[]): AiReviewComment[] {
  return [...comments].sort((left, right) => {
    const leftPath = left.path || '';
    const rightPath = right.path || '';
    if (leftPath !== rightPath) {
      if (!leftPath) return -1;
      if (!rightPath) return 1;
      return leftPath.localeCompare(rightPath);
    }
    const leftLine = left.start_line ?? Number.MAX_SAFE_INTEGER;
    const rightLine = right.start_line ?? Number.MAX_SAFE_INTEGER;
    return leftLine - rightLine;
  });
}

/** 建任务前的本地校验：返回给用户的报错文案，null 表示可提交。 */
export function validateTaskDraft(requirements: string, selectedPresetIds: string[]): string | null {
  if (!requirements.trim() && selectedPresetIds.length === 0) {
    return '请填写评审要求，或至少勾选一条全局预设';
  }
  return null;
}

/**
 * 自定义规则文件（ocr rule.json）的本地校验：与后端 parse_rule_file_config 同一套口径。
 * 返回 null 表示结构合法（并回传解析后的对象），否则给可直接展示的报错。
 */
export type RuleFileConfig = {
  include?: string[];
  exclude?: string[];
  rules?: { path: string; rule: string; merge_system_rule?: boolean }[];
};

export function validateRuleFileText(text: string): { ok: true; config: RuleFileConfig } | { ok: false; message: string } {
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch (error) {
    return { ok: false, message: `规则文件不是合法 JSON：${error instanceof Error ? error.message : error}` };
  }
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    return { ok: false, message: '规则文件顶层必须是 JSON 对象' };
  }
  const config = payload as Record<string, unknown>;
  const patterns = (value: unknown, field: string): string[] | string => {
    if (value === undefined) return [];
    if (!Array.isArray(value)) return `${field} 必须是 glob 数组`;
    for (const item of value) {
      if (typeof item !== 'string' || !item.trim()) return `${field} 里的每一条都必须是非空 glob 字符串`;
    }
    return value.map((item) => String(item).trim());
  };
  const include = patterns(config.include, 'include');
  if (typeof include === 'string') return { ok: false, message: include };
  const exclude = patterns(config.exclude, 'exclude');
  if (typeof exclude === 'string') return { ok: false, message: exclude };

  let rules: RuleFileConfig['rules'] = [];
  if (config.rules !== undefined) {
    if (!Array.isArray(config.rules)) return { ok: false, message: 'rules 必须是数组' };
    rules = [];
    for (let index = 0; index < config.rules.length; index += 1) {
      const entry = config.rules[index] as Record<string, unknown>;
      if (typeof entry !== 'object' || entry === null) {
        return { ok: false, message: `rules[${index}] 必须是 {path, rule} 对象` };
      }
      const path = String(entry.path ?? '').trim();
      const rule = String(entry.rule ?? '').trim();
      if (!path) return { ok: false, message: `rules[${index}] 缺少 path（文件 glob 模式）` };
      if (!rule) return { ok: false, message: `rules[${index}] 缺少 rule（评审规则文本）` };
      rules.push({ path, rule, merge_system_rule: Boolean(entry.merge_system_rule) });
    }
  }
  if (!include.length && !exclude.length && !(rules ?? []).length) {
    return { ok: false, message: '至少要有 include / exclude / rules 之一，否则直接删除这份规则' };
  }
  return { ok: true, config: { include, exclude, rules } };
}

/** 「填入示例」模板：catch-all 安全规则（合并内置）+ 生成代码排除。 */
export function sampleRuleFileText(): string {
  return JSON.stringify(
    {
      include: [],
      exclude: ['**/*.gen.ts', '**/generated/**', '**/vendor/**'],
      rules: [
        {
          path: 'src/api/**/*.go',
          rule: '所有导出的 handler 必须在处理前校验请求体；事务开始后必须立即 defer tx.Rollback()。',
        },
        {
          path: '**/*mapper*.xml',
          rule: '检查 SQL 注入风险、参数绑定遗漏与未闭合的 XML 标签。',
        },
        {
          path: '**/*',
          rule: '安全审查：标记硬编码密钥、未校验的重定向与缺失的权限校验。',
          merge_system_rule: true,
        },
      ],
    },
    null,
    2,
  );
}

/** 概要文案：规则文件里各字段条数。 */
export function ruleFileSummary(config: RuleFileConfig | null): string {
  if (!config) return '未配置 · 全部使用系统内置规则';
  const counts: string[] = [];
  if (config.include?.length) counts.push(`include ${config.include.length}`);
  if (config.exclude?.length) counts.push(`exclude ${config.exclude.length}`);
  if (config.rules?.length) counts.push(`规则 ${config.rules.length} 条`);
  return counts.length ? counts.join(' · ') : '未配置任何条目';
}

/**
 * 分页页码序列：最多 5 个页码，当前页居中；totalPages 为 0 时给 [1]。
 * 例如 (3, 10) → [1, 2, 3, 4, 5]，(9, 10) → [6, 7, 8, 9, 10]。
 */
export function pageNumbers(current: number, totalPages: number): number[] {
  const total = Math.max(1, totalPages);
  const center = Math.min(Math.max(1, current), total);
  const window = 5;
  let start = Math.max(1, center - Math.floor(window / 2));
  const end = Math.min(total, start + window - 1);
  start = Math.max(1, end - window + 1);
  const pages: number[] = [];
  for (let page = start; page <= end; page += 1) pages.push(page);
  return pages;
}

// ---------------------------------------------------------------------------
// 「评审思路」渲染：ocr 的 thinking 是模型原始思维链，动辄上万字符
// ---------------------------------------------------------------------------

/** 思维链体量文案，给用户一个「要不要展开」的预期。空值返回空串（调用方据此不渲染）。 */
export function formatThinkingSize(thinking: string | undefined | null): string {
  const size = (thinking ?? '').trim().length;
  if (size === 0) return '';
  if (size < 1000) return `${size} 字符`;
  return `${(size / 1000).toFixed(1)}k 字符`;
}

const THINKING_LIST_LINE = /^(?:[-*+]|\d+[.)])\s+/;

/**
 * 把原始思维链拆成可读的段落。
 *
 * 不做语义解析（模型输出格式不稳定），只做两件确定性的事：按空行分段、段内压掉
 * 硬换行——否则上万个字符会糊成一整段。段内若整体是列表、或含代码围栏，则保留
 * 换行，免得把结构压坏。
 */
export function splitThinkingBlocks(thinking: string | undefined | null): string[] {
  const text = (thinking ?? '').replace(/\r\n/g, '\n').trim();
  if (!text) return [];
  return text
    .split(/\n\s*\n/)
    .map(normalizeThinkingBlock)
    .filter(Boolean);
}

function normalizeThinkingBlock(block: string): string {
  const lines = block
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  if (lines.length <= 1) return lines.join('');
  const structured =
    lines.every((line) => THINKING_LIST_LINE.test(line)) || lines.some((line) => line.startsWith('```'));
  return structured ? lines.join('\n') : lines.join(' ');
}

// ---------------------------------------------------------------------------
// 报错文案里嵌的链接
// ---------------------------------------------------------------------------

export type TextSegment = { type: 'text' | 'link'; value: string };

const URL_PATTERN = /https?:\/\/[^\s（）()，。]+/g;

/**
 * 把一段文本按 http(s) 链接切成片段，便于把平台报错里的设置页地址渲染成可点锚点
 * （纯文本 URL 在中文句子里既难读也点不动）。
 */
export function splitTextLinks(text: string | undefined | null): TextSegment[] {
  const source = text ?? '';
  if (!source) return [];
  const segments: TextSegment[] = [];
  let cursor = 0;
  for (const match of source.matchAll(URL_PATTERN)) {
    const start = match.index ?? 0;
    if (start > cursor) segments.push({ type: 'text', value: source.slice(cursor, start) });
    segments.push({ type: 'link', value: match[0] });
    cursor = start + match[0].length;
  }
  if (cursor < source.length) segments.push({ type: 'text', value: source.slice(cursor) });
  return segments;
}
