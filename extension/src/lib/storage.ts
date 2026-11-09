/** chrome.storage.local 封装：token、用户、租户、API 地址、悬浮球位置 */

export type StoredUser = import('../types').UserRead;

const KEYS = {
  token: 'sd_token',
  user: 'sd_user',
  expiresAt: 'sd_expires_at',
  tenantId: 'sd_tenant_id',
  apiBase: 'sd_api_base',
  ballPos: 'sd_ball_pos',
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
  clearSession: async () => {
    await remove(KEYS.token);
    await remove(KEYS.user);
    await remove(KEYS.expiresAt);
  },
};
