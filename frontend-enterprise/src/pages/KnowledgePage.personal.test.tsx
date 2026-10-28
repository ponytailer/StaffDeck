// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui/tooltip';
import type { AgentProfileRead, KnowledgeBasePage } from '@/types';

import KnowledgeManagePage from './KnowledgePage';

const member = { id: 'user-1', tenant_id: 'tenant_demo', username: 'demo', role: 'member' as const };

const employee: AgentProfileRead = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  is_overall: false,
  status: 'active',
  metadata: { owner_user_id: 'user-1', owner_username: 'demo' },
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

function personalPage(rows: KnowledgeBasePage['items']): KnowledgeBasePage {
  return {
    items: rows,
    total: rows.length,
    page: 1,
    page_size: 10,
    total_pages: rows.length ? 1 : 0,
    summary: { active: rows.length, archived: 0, documents: 0 },
  };
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function mockFetch(rows: KnowledgeBasePage['items'] = []) {
  const calls: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (url.includes('/api/enterprise/knowledge-bases/api-access')) {
      return jsonResponse({
        required_scope: 'knowledge:read',
        endpoint_path: '/api/v1/knowledge-bases/{knowledge_base_id}:search',
        docs_path: '/api/v1/docs',
        public_base_url: 'https://staffdeck.example',
      });
    }
    if (url.includes('/api/enterprise/knowledge-bases')) {
      return jsonResponse(personalPage(rows));
    }
    if (url.includes('/api/enterprise/knowledge/documents')) {
      return jsonResponse({ items: [], total: 0, page: 1, page_size: 50, total_pages: 0 });
    }
    if (url.includes('/api/enterprise/agents')) return jsonResponse([employee]);
    return jsonResponse([]);
  }));
  return calls;
}

function renderPage(mode: 'employee' | 'personal') {
  return render(
    <I18nProvider>
      <TooltipProvider>
        <MemoryRouter>
          <KnowledgeManagePage mode={mode} currentUser={member} />
        </MemoryRouter>
      </TooltipProvider>
    </I18nProvider>,
  );
}

beforeEach(() => {
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  window.location.hash = '';
});

describe('KnowledgeManagePage 个人知识库模式', () => {
  it('按 mine=1 取个人库，且不跟随员工作用域', async () => {
    const calls = mockFetch([
      {
        id: 'kb_mine',
        tenant_id: 'tenant_demo',
        name: '我的报销依据',
        description: '个人知识库',
        status: 'active',
        version: '1.0.0',
        document_count: 0,
        bucket_count: 0,
        chunk_count: 0,
        created_at: '2026-10-01T00:00:00Z',
        updated_at: '2026-10-01T00:00:00Z',
      },
    ]);
    renderPage('personal');

    expect(await screen.findByText('我的知识库列表')).toBeTruthy();
    await waitFor(() => {
      const listCalls = calls.filter((url) => url.includes('/api/enterprise/knowledge-bases?'));
      expect(listCalls.length).toBeGreaterThan(0);
      expect(listCalls.every((url) => url.includes('mine=1'))).toBe(true);
    });
    // 个人模式不解析数字员工，也不带 agent_id
    expect(calls.some((url) => url.includes('/api/enterprise/agents'))).toBe(false);
    expect(calls.some((url) => url.includes('agent_id='))).toBe(false);
  });

  it('新增菜单只有「新建知识库 / 导入备份包」，没有广场/员工复制与删除', async () => {
    mockFetch([
      {
        id: 'kb_mine',
        tenant_id: 'tenant_demo',
        name: '我的报销依据',
        description: '',
        status: 'active',
        version: '1.0.0',
        document_count: 0,
        bucket_count: 0,
        chunk_count: 0,
        created_at: '2026-10-01T00:00:00Z',
        updated_at: '2026-10-01T00:00:00Z',
      },
    ]);
    renderPage('personal');

    await screen.findByText('我的知识库列表');
    await userEvent.click(screen.getByRole('button', { name: /新增/ }));
    expect(await screen.findByText('新建知识库')).toBeTruthy();
    expect(screen.getByText('导入知识库备份包')).toBeTruthy();
    expect(screen.queryByText('从广场复制')).toBeNull();
    expect(screen.queryByText('从数字员工复制')).toBeNull();
    expect(screen.queryByText('从我的知识库导入')).toBeNull();
  });
});

describe('KnowledgeManagePage 个人知识库的「API 接入」指引', () => {
  const row = {
    id: 'kb_mine',
    tenant_id: 'tenant_demo',
    name: '我的报销依据',
    description: '',
    status: 'active',
    version: '1.0.0',
    capability_scope: 'general' as const,
    metadata: {},
    document_count: 0,
    bucket_count: 0,
    chunk_count: 0,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
  };

  it('个人模式可打开指引，示例里带上当前知识库 id 与对外基址', async () => {
    mockFetch([row]);
    renderPage('personal');

    await screen.findByText('我的知识库列表');
    // 等列表行回来，确保弹窗里能拿到当前知识库
    expect((await screen.findAllByText('我的报销依据')).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole('button', { name: /API 接入/ }));

    expect(await screen.findByText('API 接入 · 我的知识库')).toBeTruthy();
    // 示例里已把 {knowledge_base_id} 换成当前库，且基址取自后端 api-access
    const sample = await screen.findByTestId('knowledge-api-sample-curl');
    expect(sample.textContent).toContain('https://staffdeck.example/api/v1/knowledge-bases/kb_mine:search');
    expect(sample.textContent).toContain('Authorization: Bearer');
  });

  it('员工模式不显示该入口', async () => {
    mockFetch([]);
    renderPage('employee');

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /新增/ })).toBeTruthy();
    });
    expect(screen.queryByRole('button', { name: /API 接入/ })).toBeNull();
  });
});

describe('KnowledgeManagePage 员工知识库的「从我的知识库导入」', () => {
  it('新增菜单包含导入项，弹窗从 mine=1 取来源列表', async () => {
    const calls = mockFetch([]);
    renderPage('employee');

    // 等员工作用域解析完成（默认落到成员可用的员工）
    await waitFor(() => {
      expect(calls.some((url) => url.includes('/api/enterprise/agents'))).toBe(true);
    });
    await waitFor(() => {
      expect(calls.some((url) => url.includes('agent_id=agent-1'))).toBe(true);
    });

    await userEvent.click(await screen.findByRole('button', { name: /新增/ }));
    const importItem = await screen.findByText('从我的知识库导入');
    await userEvent.click(importItem);

    expect(
      await screen.findByText('从「我的知识库」导入一份员工专属副本；后续改动互不影响，可重复导入覆盖。'),
    ).toBeTruthy();
    await waitFor(() => {
      expect(calls.some((url) => url.includes('mine=1') && url.includes('/api/enterprise/knowledge-bases?'))).toBe(true);
    });
  });
});