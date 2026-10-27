import { api, TENANT_ID } from '../api/client';

/**
 * 未读通知数的共享取数器。
 *
 * **为什么需要**：这个接口的调用方不止一个视图实例。App.tsx 让 `DistillPage`
 * 常驻挂载（非 SOP 路由时只是 CSS `hidden`），它自带一份 `AppHeader`；路由区
 * 的页面又各自渲染一份 `AppHeader`。两个实例的 effect 都会在挂载时取一次数，
 * 于是同一秒内出现两条一模一样的 `GET .../unread-count`。
 *
 * 除了实例冗余，单个实例自身也可能并发触发：`focus` 事件、5 分钟轮询、
 * `NOTIFICATIONS_CHANGED_EVENT` 三路都调 `loadUnread()`，窗口聚焦瞬间正好
 * 撞上轮询就会打出两条。
 *
 * 这里做两层收敛：
 *
 * 1. **in-flight 合并**：同一时刻只有一个真实请求，其余调用者复用它。
 * 2. **短 TTL**：`FRESH_MS` 内重复问直接返回缓存值。窗口只有 3 秒——足够
 *    吃掉同一批次的并发，又不会让「刚标记已读」之后看到旧数字。
 *
 * 标记已读等**明确要立刻刷新**的场景传 `force: true` 绕过 TTL。
 */
const FRESH_MS = 3_000;

let inflight: Promise<number> | null = null;
let lastValue = 0;
let lastFetchedAt = 0;

async function requestOnce(): Promise<number> {
  const result = await api.get<{ count: number }>(
    `/api/enterprise/notifications/unread-count?tenant_id=${TENANT_ID}`,
  );
  return Math.max(0, Number(result.count) || 0);
}

export function fetchUnreadNotificationCount(options?: { force?: boolean }): Promise<number> {
  if (inflight) return inflight;

  const fresh = lastFetchedAt > 0 && Date.now() - lastFetchedAt < FRESH_MS;
  if (fresh && !options?.force) return Promise.resolve(lastValue);

  inflight = requestOnce()
    .then((value) => {
      lastValue = value;
      lastFetchedAt = Date.now();
      return value;
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** 登出、切换租户等场景把缓存清干净，避免新会话看到上一个用户的通知数。 */
export function resetUnreadNotificationCache(): void {
  inflight = null;
  lastValue = 0;
  lastFetchedAt = 0;
}