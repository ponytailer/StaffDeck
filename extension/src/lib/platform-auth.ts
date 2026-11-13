/**
 * 从 AIStaff 平台的登录态自动接管插件登录（用户装完插件不用再输一次账号密码）。
 *
 * 平台前端把会话写在页面 localStorage 的 `ultrarag_auth`
 * （见 frontend-enterprise/src/auth.ts ENTERPRISE_AUTH_STORAGE_KEY）：
 *   { token, user: { id, tenant_id, ... }, expires_at }
 * 后端鉴权走 Authorization: Bearer <JWT>，不是 cookie —— 所以这里「找 token」就够了，
 * 不需要 cookies 权限，也不需要后端任何改动。
 *
 * 两条取数路径：
 *  1. 当前页就是平台页：content script 的 localStorage 与页面同源共享，直接读（零权限、最快）；
 *  2. 当前页不是平台页（如 OA 报销页）：让 service worker 找到后台开着的平台标签页，
 *     用 chrome.scripting 在 MAIN world 里读同一份 localStorage（需 scripting 权限）。
 *     平台标签页没开着时无法读取 —— 这是浏览器沙箱的硬边界，此时退回账号密码登录。
 *
 * 三条安全边界：
 *  - 平台 token 本身已过期（expires_at 到点）不接管；
 *  - 接管后必须能用它打通 /api/auth/me，否则视为无效并清空，绝不留半截登录态；
 *  - 用户在插件里主动登出过、且平台 token 没变 → 尊重登出，不再自动接管
 *    （否则「退出登录」会被下一次打开面板立刻打回原形）；平台重新登录换了 token 才恢复。
 */

import { fetchMe } from './api';
import { storage } from './storage';
import type { UserRead } from '../types';

/** 平台前端会话的 localStorage key（与 frontend-enterprise/src/auth.ts 对齐） */
export const PLATFORM_AUTH_STORAGE_KEY = 'ultrarag_auth';

/** 允许自动接管的平台地址（与 manifest host_permissions 保持一致） */
const PLATFORM_PREFIXES = [
  'https://ai-staff.folidaymall.com',
  'http://localhost:8000',
  'http://127.0.0.1:8000',
];

export type PlatformSession = {
  token: string;
  tenantId: string | null;
  expiresAt: number | null;
  origin: string;
};

export type SyncResult =
  | { ok: true; user: UserRead; origin: string }
  | { ok: false; reason: 'no-platform-session' | 'expired' | 'logged-out' | 'invalid' | 'unreachable' };

/** 是否平台域名（只有平台页的 localStorage 才可能是我们的会话） */
export function isPlatformOrigin(origin: string): boolean {
  return PLATFORM_PREFIXES.some((p) => origin === p || origin.startsWith(`${p}/`));
}

/**
 * token 指纹（FNV-1a + 长度）：只用来判断「平台 token 有没有换过」，
 * 不落原文，避免登出后还把可用凭证留在存储里。
 */
export function fingerprintToken(token: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < token.length; i += 1) {
    h ^= token.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `${h.toString(16)}:${token.length}`;
}

function parseSession(raw: string | null, origin: string): PlatformSession | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as {
      token?: unknown;
      user?: { tenant_id?: unknown };
      expires_at?: unknown;
    };
    if (!parsed || typeof parsed.token !== 'string' || !parsed.token) return null;
    const tenant = parsed.user && typeof parsed.user.tenant_id === 'string' ? parsed.user.tenant_id : null;
    return {
      token: parsed.token,
      tenantId: tenant,
      expiresAt: typeof parsed.expires_at === 'number' ? parsed.expires_at : null,
      origin,
    };
  } catch {
    return null;
  }
}

/** 路径 1：当前页即平台页，直接读本页 localStorage */
function readFromThisPage(): PlatformSession | null {
  if (typeof window === 'undefined' || !isPlatformOrigin(window.location.origin)) return null;
  try {
    return parseSession(window.localStorage.getItem(PLATFORM_AUTH_STORAGE_KEY), window.location.origin);
  } catch {
    // 隐私模式下 localStorage 可能直接抛错
    return null;
  }
}

/** 路径 2：让 service worker 去开着的平台标签页里读（见 background/main.ts） */
async function readFromOpenTabs(): Promise<PlatformSession | null> {
  if (typeof chrome === 'undefined' || !chrome.runtime?.sendMessage) return null;
  try {
    const res = (await chrome.runtime.sendMessage({ type: 'read-platform-auth' })) as
      | { ok?: boolean; session?: PlatformSession | null }
      | undefined;
    if (!res || res.ok !== true || !res.session) return null;
    const s = res.session;
    if (typeof s.token !== 'string' || !s.token || !isPlatformOrigin(String(s.origin))) return null;
    return {
      token: s.token,
      tenantId: typeof s.tenantId === 'string' ? s.tenantId : null,
      expiresAt: typeof s.expiresAt === 'number' ? s.expiresAt : null,
      origin: s.origin,
    };
  } catch {
    // SW 未就绪 / 消息通道被页面关闭
    return null;
  }
}

/**
 * 尝试用平台登录态登录插件。
 * 成功时已把 token / user / 租户 / 服务地址全部写好，调用方直接进列表页即可。
 */
export async function syncAuthFromPlatform(): Promise<SyncResult> {
  const session = readFromThisPage() ?? (await readFromOpenTabs());
  if (!session) return { ok: false, reason: 'no-platform-session' };

  if (session.expiresAt !== null && session.expiresAt * 1000 <= Date.now()) {
    return { ok: false, reason: 'expired' };
  }

  const fingerprint = fingerprintToken(session.token);
  if ((await storage.getLogoutFingerprint()) === fingerprint) {
    return { ok: false, reason: 'logged-out' };
  }

  // 先落 token 再验证：request() 从存储里取 Authorization。
  // 多个键合并成一次写入（串行 storage 往返在慢速/节流环境会被放大）
  const previousBase = await storage.getApiBase();
  const previousTenant = await storage.getTenantId();
  await storage.setSyncedSession({
    token: session.token,
    expiresAt: session.expiresAt,
    apiBase: session.origin,
    tenantId: session.tenantId,
  });

  try {
    const user = await fetchMe();
    // 以服务端为准回填用户资料与租户（平台会话里的 tenant_id 只是线索）
    await storage.setSyncedSession({
      token: session.token,
      expiresAt: session.expiresAt,
      apiBase: session.origin,
      tenantId: user.tenant_id || session.tenantId,
      user,
    });
    await storage.clearLogoutFingerprint();
    return { ok: true, user, origin: session.origin };
  } catch {
    // token 无效/服务不可达：不要留下半截会话，回滚到原本的地址与租户
    await storage.clearSession();
    await storage.setApiBase(previousBase);
    await storage.setTenantId(previousTenant);
    return { ok: false, reason: 'invalid' };
  }
}
