import { ApiError } from '@/api/client';

/** 后端在「SKILL.md 引用的文件没进包」时返回的稳定错误码。 */
export const GENERAL_SKILL_MISSING_REFERENCES_CODE = 'GENERAL_SKILL_MISSING_REFERENCES';

type MissingReferencesDetail = {
  code?: unknown;
  paths?: unknown;
};

/**
 * 从「导入技能」的 400 响应里取出缺失的参考文件路径。
 *
 * 后端 `detail` 是结构化对象 `{ code, message, paths }`；`ApiError` 只保留了
 * `code` / `message` 和原始 `body`，所以这里再解析一次 body 拿 `paths`。
 * 不是该类错误一律返回空数组，调用方据此决定走普通报错还是二次确认弹窗。
 */
export function missingSkillReferencePaths(error: unknown): string[] {
  if (!(error instanceof ApiError) || error.status !== 400) return [];
  try {
    const payload = JSON.parse(error.body) as { detail?: MissingReferencesDetail };
    const detail = payload?.detail;
    if (!detail || detail.code !== GENERAL_SKILL_MISSING_REFERENCES_CODE) return [];
    if (!Array.isArray(detail.paths)) return [];
    return detail.paths.filter(
      (item): item is string => typeof item === 'string' && Boolean(item.trim()),
    );
  } catch {
    return [];
  }
}
