// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  isDispatcherScopeId,
  rememberDispatcherScopeIds,
  resetDispatcherScopeCacheForTests,
} from './dispatcher-scope';

/**
 * 「当前作用域是不是员工小队」必须能**同步**判断：dispatcher 列表要等远程 PG
 * 往返（冷启动实测 3~8s），等它回来之前所有兜底都会把人当成员工。
 */
describe('dispatcher-scope', () => {
  beforeEach(() => {
    window.localStorage.clear();
    resetDispatcherScopeCacheForTests();
  });

  afterEach(() => {
    window.localStorage.clear();
    resetDispatcherScopeCacheForTests();
  });

  it('remembers ids so the next synchronous read works without the list', () => {
    rememberDispatcherScopeIds(['agent_d1', 'agent_d2']);
    resetDispatcherScopeCacheForTests(); // 模拟冷启动：内存缓存丢了
    expect(isDispatcherScopeId('agent_d1')).toBe(true);
    expect(isDispatcherScopeId('agent_d2')).toBe(true);
  });

  it('does not treat a regular employee as a dispatcher', () => {
    rememberDispatcherScopeIds(['agent_d1']);
    expect(isDispatcherScopeId('agent_employee')).toBe(false);
  });

  it('returns false for empty / missing ids', () => {
    rememberDispatcherScopeIds(['agent_d1']);
    expect(isDispatcherScopeId('')).toBe(false);
    expect(isDispatcherScopeId(null)).toBe(false);
    expect(isDispatcherScopeId(undefined)).toBe(false);
  });

  it('survives a corrupt cache payload instead of throwing', () => {
    window.localStorage.setItem('ultrarag_dispatcher_scope_ids', '{not json');
    resetDispatcherScopeCacheForTests();
    expect(isDispatcherScopeId('agent_d1')).toBe(false);
    expect(() => rememberDispatcherScopeIds(['agent_d1'])).not.toThrow();
    expect(isDispatcherScopeId('agent_d1')).toBe(true);
  });

  it('overwrites the cache when the list is re-fetched', () => {
    rememberDispatcherScopeIds(['agent_d1']);
    rememberDispatcherScopeIds(['agent_d2']);
    expect(isDispatcherScopeId('agent_d1')).toBe(false);
    expect(isDispatcherScopeId('agent_d2')).toBe(true);
  });
});