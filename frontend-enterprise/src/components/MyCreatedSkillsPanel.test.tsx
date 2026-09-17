// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { AgentProfileRead, GeneralSkillRead } from '@/types';

import MyCreatedSkillsPanel from './MyCreatedSkillsPanel';

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function makeSkill(
  slug: string,
  name: string,
  status: GeneralSkillRead['status'],
  ownerAgentId?: string,
): GeneralSkillRead {
  return {
    id: `skill-${slug}`,
    tenant_id: 'tenant_demo',
    slug,
    name,
    description: `${name} 的描述`,
    capability_scope: 'general',
    skill_markdown: '# demo',
    skill_files: [],
    metadata: ownerAgentId ? { owner_agent_id: ownerAgentId } : {},
    status,
    permissions: {},
    runtime_config: {},
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-10T00:00:00Z',
  };
}

const agents: AgentProfileRead[] = [
  {
    id: 'agent-1',
    tenant_id: 'tenant_demo',
    name: '小艾',
    is_overall: false,
    status: 'active',
    metadata: {},
    resources: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  },
];

function LocationEcho() {
  const location = useLocation();
  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>;
}

/**
 * 沿用 EmployeeGalleryPage.test.tsx 的全局 fetch 桩方式（不另发明 mock）。
 * mine=1 的 GET 返回本面板技能；publish/archive/delete 按 URL 返回对应结果，
 * 调用记录留在 fetchMock.mock.calls 里供断言 agent_id 拼装。
 */
function renderPanel(skills: GeneralSkillRead[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method || 'GET').toUpperCase();
    if (method === 'GET' && url.includes('/api/enterprise/general-skills') && url.includes('mine=1')) {
      return jsonResponse(skills);
    }
    if (method === 'POST' && /\/api\/enterprise\/general-skills\/[^/]+\/(publish|archive)/.test(url)) {
      const slug = url.match(/\/api\/enterprise\/general-skills\/([^/]+)\/(publish|archive)/)![1];
      const published = url.includes('/publish');
      const found = skills.find((item) => item.slug === slug) || skills[0];
      return jsonResponse({ ...found, status: published ? 'published' : 'archived' });
    }
    if (method === 'DELETE' && url.includes('/api/enterprise/general-skills')) {
      return jsonResponse({});
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);

  render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/enterprise/general-skills/mine']}>
        <Routes>
          <Route path="/enterprise/general-skills/mine" element={<MyCreatedSkillsPanel agents={agents} />} />
          <Route path="/enterprise/general-skills" element={<LocationEcho />} />
        </Routes>
      </MemoryRouter>
    </I18nProvider>,
  );

  return fetchMock;
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
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('MyCreatedSkillsPanel', () => {
  it('renders skill names and slugs returned by the API', async () => {
    renderPanel([
      makeSkill('report-1', '周报生成器', 'published'),
      makeSkill('summary-1', '会议纪要总结', 'draft'),
    ]);

    const table = await screen.findByRole('table', { name: '我创建的技能列表' });
    expect(within(table).getByText('周报生成器')).toBeTruthy();
    expect(within(table).getByText('report-1')).toBeTruthy();
    expect(within(table).getByText('会议纪要总结')).toBeTruthy();
    expect(within(table).getByText('summary-1')).toBeTruthy();
  });

  it('shows 技能广场 for gallery skills and the employee display name for agent-bound skills', async () => {
    renderPanel([
      makeSkill('report-1', '周报生成器', 'published'),
      makeSkill('agent-skill-1', '员工专属技能', 'published', 'agent-1'),
    ]);

    const table = await screen.findByRole('table', { name: '我创建的技能列表' });
    expect(within(table).getByText('技能广场')).toBeTruthy();
    expect(within(table).getByText('小艾')).toBeTruthy();
  });

  it('opens a confirm dialog on gallery skill 删除 and deletes without agent_id', async () => {
    const fetchMock = renderPanel([makeSkill('report-1', '周报生成器', 'published')]);

    const table = await screen.findByRole('table', { name: '我创建的技能列表' });
    const trigger = within(table).getAllByLabelText('技能操作')[0];
    fireEvent.keyDown(trigger, { key: 'Enter' });

    const deleteItem = await screen.findByRole('menuitem', { name: '删除' });
    fireEvent.click(deleteItem);

    expect(await screen.findByText(/删除技能/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '删除' }));

    await waitFor(() => {
      const del = fetchMock.mock.calls.find((call) => call[1]?.method === 'DELETE');
      expect(del).toBeTruthy();
      expect(String(del?.[0])).not.toContain('agent_id');
    });
  });

  it('removes an agent-bound skill with agent_id in the delete URL', async () => {
    const fetchMock = renderPanel([makeSkill('agent-skill-1', '员工专属技能', 'published', 'agent-1')]);

    const table = await screen.findByRole('table', { name: '我创建的技能列表' });
    const trigger = within(table).getAllByLabelText('技能操作')[0];
    fireEvent.keyDown(trigger, { key: 'Enter' });

    const removeItem = await screen.findByRole('menuitem', { name: '移除' });
    fireEvent.click(removeItem);

    expect(await screen.findByText(/移除技能/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: '移除' }));

    await waitFor(() => {
      const del = fetchMock.mock.calls.find((call) => call[1]?.method === 'DELETE');
      expect(del).toBeTruthy();
      expect(String(del?.[0])).toContain('agent_id=agent-1');
    });
  });

  it('archives an agent-bound skill with agent_id', async () => {
    const fetchMock = renderPanel([makeSkill('agent-skill-1', '员工专属技能', 'published', 'agent-1')]);

    const table = await screen.findByRole('table', { name: '我创建的技能列表' });
    const trigger = within(table).getAllByLabelText('技能操作')[0];
    fireEvent.keyDown(trigger, { key: 'Enter' });

    const archiveItem = await screen.findByRole('menuitem', { name: '停用' });
    fireEvent.click(archiveItem);

    await waitFor(() => {
      const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST');
      expect(post).toBeTruthy();
      const url = String(post?.[0]);
      expect(url).toContain('/api/enterprise/general-skills/agent-skill-1/archive');
      expect(url).toContain('agent_id=agent-1');
    });
  });

  it('publishes a gallery skill without agent_id', async () => {
    const fetchMock = renderPanel([makeSkill('report-1', '周报生成器', 'draft')]);

    const table = await screen.findByRole('table', { name: '我创建的技能列表' });
    const trigger = within(table).getAllByLabelText('技能操作')[0];
    fireEvent.keyDown(trigger, { key: 'Enter' });

    const enableItem = await screen.findByRole('menuitem', { name: '启用' });
    fireEvent.click(enableItem);

    await waitFor(() => {
      const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST');
      expect(post).toBeTruthy();
      const url = String(post?.[0]);
      expect(url).toContain('/api/enterprise/general-skills/report-1/publish');
      expect(url).not.toContain('agent_id');
    });
  });

  it('shows the empty state with a 去技能广场 button when there are no skills', async () => {
    renderPanel([]);

    expect(await screen.findByText('还没有创建过技能')).toBeTruthy();
    const goButton = screen.getByRole('button', { name: '去技能广场' });
    fireEvent.click(goButton);
    expect((await screen.findByTestId('location')).textContent).toBe('/enterprise/general-skills');
  });
});
