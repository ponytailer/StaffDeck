/** chrome.storage.local 封装：token、用户、租户、API 地址、悬浮球位置 */

export type StoredUser = import('../types').UserRead;

const KEYS = {
  token: 'sd_token',
  user: 'sd_user',
  expiresAt: 'sd_expires_at',
  tenantId: 'sd_tenant_id',
  apiBase: 'sd_api_base',
  ballPos: 'sd_ball_pos',
  /** 用户主动登出时记下的平台 token 指纹：同一个 token 不再自动接管登录 */
  logoutFingerprint: 'sd_logout_fp',
} as const;

export const DEFAULT_API_BASE = 'https://ai-staff.folidaymall.com';
export const DEFAULT_TENANT_ID = 'tenant_demo';

function get<T>(key: string): Promise<T | null> {
  return new Promise((resolve) => {
    chrome.storage.local.get(key, (items) => resolve((items[key] as T) ?? null));
  });
}

function set(key: string, value: unknown): Promise<void> {
  return new Promise((resolve) => {
    chrome.storage.local.set({ [key]: value }, () => resolve());
  });
}

/** 一次写入多项：登录态同步要写 4~5 个键，合并成一次往返 */
function setMany(items: Record<string, unknown>): Promise<void> {
  return new Promise((resolve) => {
    chrome.storage.local.set(items, () => resolve());
  });
}

function remove(key: string): Promise<void> {
  return new Promise((resolve) => {
    chrome.storage.local.remove(key, () => resolve());
  });
}

export type BallPos = { x: number; y: number };

export const storage = {
  getToken: () => get<string>(KEYS.token),
  setToken: (t: string) => set(KEYS.token, t),
  getUser: () => get<StoredUser>(KEYS.user),
  setUser: (u: StoredUser) => set(KEYS.user, u),
  getExpiresAt: () => get<number>(KEYS.expiresAt),
  setExpiresAt: (v: number | null) => set(KEYS.expiresAt, v),
  getTenantId: async () => (await get<string>(KEYS.tenantId)) ?? DEFAULT_TENANT_ID,
  setTenantId: (v: string) => set(KEYS.tenantId, v),
  getApiBase: async () => (await get<string>(KEYS.apiBase)) ?? DEFAULT_API_BASE,
  setApiBase: (v: string) => set(KEYS.apiBase, v),
  getBallPos: () => get<BallPos>(KEYS.ballPos),
  setBallPos: (v: BallPos) => set(KEYS.ballPos, v),
  getLogoutFingerprint: () => get<string>(KEYS.logoutFingerprint),
  setLogoutFingerprint: (v: string) => set(KEYS.logoutFingerprint, v),
  clearLogoutFingerprint: () => remove(KEYS.logoutFingerprint),
  /**
   * 平台登录态接管专用：把会话一次性落盘（多次串行 set 在慢速环境下会被放大）。
   * 只写入显式传入的字段，避免 undefined 覆盖已有值。
   */
  setSyncedSession: (v: {
    token: string;
    expiresAt: number | null;
    apiBase: string;
    tenantId?: string | null;
    user?: StoredUser;
  }) => {
    const items: Record<string, unknown> = {
      [KEYS.token]: v.token,
      [KEYS.expiresAt]: v.expiresAt,
      [KEYS.apiBase]: v.apiBase,
    };
    if (v.tenantId) items[KEYS.tenantId] = v.tenantId;
    if (v.user) items[KEYS.user] = v.user;
    return setMany(items);
  },
  clearSession: async () => {
    await remove(KEYS.token);
    await remove(KEYS.user);
    await remove(KEYS.expiresAt);
  },
};
