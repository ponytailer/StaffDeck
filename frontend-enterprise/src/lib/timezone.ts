import { getDateLocale } from '@/i18n';

const FALLBACK_TIME_ZONE = 'Asia/Shanghai';

export function getClientTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || FALLBACK_TIME_ZONE;
  } catch {
    return FALLBACK_TIME_ZONE;
  }
}

export function parseBackendDateTime(value?: string): Date {
  const text = String(value || '').trim();
  if (!text) return new Date('');
  // 纯日期字符串本就被当作 UTC 解析，无需补时区后缀
  if (!text.includes('T')) return new Date(text);
  // 后端时间戳为 naive UTC（无 Z 后缀），缺失时按 UTC 解析而非本地时间
  if (/([zZ]|[+-]\d{2}:\d{2})$/.test(text)) return new Date(text);
  return new Date(`${text}Z`);
}

export function formatClientDateTime(value?: string, emptyText = '-'): string {
  if (!value) return emptyText;
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return emptyText;
  return date.toLocaleString(getDateLocale(), {
    hour12: false,
    timeZone: getClientTimeZone(),
  });
}

/**
 * 只取日期部分（`YYYY/M/D`）的本地化格式化。
 *
 * 替代各处 `value.slice(0, 10)` 的写法：后端存的是 naive UTC，直接截字符串会把
 * 「本地凌晨」的时间戳显示成前一天（如本地 09-23 02:00 = UTC 09-22 18:00）。
 * 纯日期串（不含 `T`）本身没有时刻信息，按 UTC 输出以免被时区推前一天。
 */
export function formatClientDate(value?: string, emptyText = '-'): string {
  if (!value) return emptyText;
  const text = String(value).trim();
  const date = parseBackendDateTime(text);
  if (Number.isNaN(date.getTime())) return emptyText;
  return date.toLocaleDateString(getDateLocale(), {
    timeZone: text.includes('T') ? getClientTimeZone() : 'UTC',
  });
}
