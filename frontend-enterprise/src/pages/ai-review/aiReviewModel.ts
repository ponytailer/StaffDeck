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

/** rq 任务是分钟级的，这里把状态映射成轮询间隔：活跃任务 3s，其余 10s。 */
export function taskPollingInterval(tasks: { status: AiReviewTaskStatus }[]): number {
  const active = tasks.some((task) => task.status === 'queued' || task.status === 'running');
  return active ? 3000 : 10000;
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
