// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const getMock = vi.fn();

vi.mock('../api/client', () => ({
  api: { get: (...args: unknown[]) => getMock(...args) },
  TENANT_ID: 'tenant_demo',
}));

async function loadModule() {
  vi.resetModules();
  return import('./unreadNotifications');
}

describe('fetchUnreadNotificationCount', () => {
  beforeEach(() => {
    getMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('同一时刻的并发调用只打一个请求（多是 header 实例）', async () => {
    const { fetchUnreadNotificationCount } = await loadModule();
    getMock.mockResolvedValue({ count: 7 });

    const [a, b] = await Promise.all([
      fetchUnreadNotificationCount(),
      fetchUnreadNotificationCount(),
    ]);

    expect(getMock).toHaveBeenCalledTimes(1);
    expect(a).toBe(7);
    expect(b).toBe(7);
  });

  it('TTL 内的后续调用复用缓存，不打请求', async () => {
    const { fetchUnreadNotificationCount } = await loadModule();
    getMock.mockResolvedValue({ count: 3 });

    await fetchUnreadNotificationCount();
    await fetchUnreadNotificationCount();

    expect(getMock).toHaveBeenCalledTimes(1);
  });

  it('force 绕过 TTL，用于标记已读后的立即刷新', async () => {
    const { fetchUnreadNotificationCount } = await loadModule();
    getMock.mockResolvedValueOnce({ count: 3 }).mockResolvedValueOnce({ count: 2 });

    const first = await fetchUnreadNotificationCount();
    const second = await fetchUnreadNotificationCount({ force: true });

    expect(getMock).toHaveBeenCalledTimes(2);
    expect(first).toBe(3);
    expect(second).toBe(2);
  });

  it('TTL 过期后重新取数', async () => {
    vi.useFakeTimers();
    const { fetchUnreadNotificationCount } = await loadModule();
    getMock.mockResolvedValueOnce({ count: 1 }).mockResolvedValueOnce({ count: 5 });

    await fetchUnreadNotificationCount();
    vi.advanceTimersByTime(4_000);
    const next = await fetchUnreadNotificationCount();

    expect(getMock).toHaveBeenCalledTimes(2);
    expect(next).toBe(5);
  });

  it('负数与非法值归零', async () => {
    const { fetchUnreadNotificationCount } = await loadModule();
    getMock.mockResolvedValue({ count: -9 });

    expect(await fetchUnreadNotificationCount()).toBe(0);
  });

  it('失败后不缓存，下一次仍会重试', async () => {
    const { fetchUnreadNotificationCount } = await loadModule();
    getMock.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce({ count: 4 });

    await expect(fetchUnreadNotificationCount()).rejects.toThrow('boom');
    await expect(fetchUnreadNotificationCount()).resolves.toBe(4);

    expect(getMock).toHaveBeenCalledTimes(2);
  });

  it('reset 清掉缓存，新会话重新取数', async () => {
    const { fetchUnreadNotificationCount, resetUnreadNotificationCache } = await loadModule();
    getMock.mockResolvedValueOnce({ count: 6 }).mockResolvedValueOnce({ count: 0 });

    await fetchUnreadNotificationCount();
    resetUnreadNotificationCache();
    const after = await fetchUnreadNotificationCount();

    expect(getMock).toHaveBeenCalledTimes(2);
    expect(after).toBe(0);
  });
});