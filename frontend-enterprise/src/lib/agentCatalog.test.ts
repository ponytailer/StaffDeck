import { describe, expect, it } from 'vitest';

import {
  AGENT_CATALOG,
  agentCatalogMeta,
  findAgentCatalogEntry,
  listAgentCatalog,
  normalizeAgentCatalog,
} from './agentCatalog';

describe('normalizeAgentCatalog', () => {
  it('丢弃缺字段的条目而不是抛错', () => {
    const rows = normalizeAgentCatalog({
      agents: [
        { id: 'ok', name: '可用', entry: 'ok', capability: 'slides' },
        { id: '', name: '缺 id', entry: 'no-id' },
        { name: '缺 id 与 entry' },
        null,
        'not-an-object',
      ],
    });
    expect(rows.map((row) => row.entry)).toEqual(['ok']);
  });

  it('按 entry 去重并补默认值', () => {
    const rows = normalizeAgentCatalog([
      { id: 'a', name: ' A ', entry: 'dup', capability: 'slides' },
      { id: 'b', name: 'B', entry: 'dup', capability: 'slides' },
      { id: 'c', name: 'C', entry: 'c', capability: 'slides', author: '', updated_at: '', category: '' },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows[0].name).toBe('A');
    expect(rows[1].author).toBe('平台内置');
    expect(rows[1].category).toBe('通用');
    expect(rows[1].updatedAt).toBe('');
  });

  it('status 只认 offline，其余一律 active', () => {
    const rows = normalizeAgentCatalog([
      { id: 'a', name: 'A', entry: 'a', status: 'OFFLINE' },
      { id: 'b', name: 'B', entry: 'b', status: 'whatever' },
    ]);
    expect(rows.map((row) => row.status)).toEqual(['offline', 'active']);
  });

  it('接受顶层数组', () => {
    expect(normalizeAgentCatalog([{ id: 'a', name: 'A', entry: 'a' }])).toHaveLength(1);
    expect(normalizeAgentCatalog(null)).toEqual([]);
  });
});

describe('Agent 清单', () => {
  it('随包发布至少包含 AI 幻灯片生成', () => {
    const entry = findAgentCatalogEntry('slides-maker');
    expect(entry).not.toBeNull();
    expect(entry?.capability).toBe('slides');
    expect(entry?.status).toBe('active');
  });

  it('随包发布包含决策助手（decision 能力，无样式口径）', () => {
    const entry = findAgentCatalogEntry('decision-assistant');
    expect(entry).not.toBeNull();
    expect(entry?.capability).toBe('decision');
    expect(entry?.prompt).toBe('');
  });

  it('随包发布包含 AI CodeReviewer（code-review 能力，adminOnly）', () => {
    const entry = findAgentCatalogEntry('code-reviewer');
    expect(entry).not.toBeNull();
    expect(entry?.capability).toBe('code-review');
    expect(entry?.adminOnly).toBe(true);
  });

  it('adminOnly 条目只进管理员视角的列表', () => {
    const memberList = listAgentCatalog(false);
    const adminList = listAgentCatalog(true);
    expect(memberList.some((row) => row.entry === 'code-reviewer')).toBe(false);
    expect(adminList.some((row) => row.entry === 'code-reviewer')).toBe(true);
    // 非条目级别的过滤对两个视角一致：都是在线条目
    expect(memberList.every((row) => row.status === 'active')).toBe(true);
    expect(adminList.every((row) => row.status === 'active')).toBe(true);
  });

  it('adminOnly 字段缺失时按普通条目处理', () => {
    const [row] = normalizeAgentCatalog([{ id: 'a', name: 'A', entry: 'a' }]);
    expect(row.adminOnly).toBe(false);
    const [explicit] = normalizeAgentCatalog([
      { id: 'b', name: 'B', entry: 'b', admin_only: true },
    ]);
    expect(explicit.adminOnly).toBe(true);
  });

  it('entry 与 id 都能命中同一条目', () => {
    const byEntry = findAgentCatalogEntry('slides-maker');
    const byId = findAgentCatalogEntry(byEntry!.id);
    expect(byId).toEqual(byEntry);
    expect(findAgentCatalogEntry(undefined)).toBeNull();
    expect(findAgentCatalogEntry('not-exists')).toBeNull();
  });

  it('列表只返回在线条目', () => {
    expect(listAgentCatalog().every((row) => row.status === 'active')).toBe(true);
    expect(listAgentCatalog().length).toBeLessThanOrEqual(AGENT_CATALOG.length);
  });

  it('meta 行是「作者 · 更新于 日期」', () => {
    const entry = findAgentCatalogEntry('slides-maker')!;
    // 不硬编码作者名：清单里的作者会换人，断言只锁格式
    expect(entry.author).not.toBe('');
    expect(agentCatalogMeta(entry)).toBe(`${entry.author} · 更新于 ${entry.updatedAt}`);
    expect(agentCatalogMeta({ ...entry, updatedAt: '' })).toBe(entry.author);
  });

  it('prompt 字段缺失时是空串，不影响页面渲染', () => {
    const [withoutPrompt] = normalizeAgentCatalog([{ id: 'a', name: 'A', entry: 'a' }]);
    expect(withoutPrompt.prompt).toBe('');
    const [withPrompt] = normalizeAgentCatalog([
      { id: 'b', name: 'B', entry: 'b', prompt: '  结论先行  ' },
    ]);
    expect(withPrompt.prompt).toBe('结论先行');
  });

  it('随包发布的 AI 幻灯片生成带「旅文汇报样式」口径', () => {
    const entry = findAgentCatalogEntry('slides-maker')!;
    expect(entry.prompt).toContain('旅文汇报口径');
  });
});
