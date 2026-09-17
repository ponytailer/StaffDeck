// @vitest-environment jsdom

// 页面级集成验证「我创建的技能」入口（与组件单测正交：这里真实渲染 EmployeeGalleryPage
// 并切 tab，断言区块出现/消失、mine=1 URL、顶部搜索透传过滤，以及失败路径不白屏）。
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { notify } from '@/components/ui/app-toast';
import { MemoryRouter } from 'react-router-dom';
import type { EnterpriseAuthUser } from '@/auth';
import type { AgentProfileRead, GeneralSkillRead, TeamRead } from '@/types';

import EmployeeGalleryPage from './EmployeeGalleryPage';

const currentUser: EnterpriseAuthUser = {
  id: 'user-1',
  username: 'me',
  tenant_id: 'tenant_demo',
  role: 'member',
};

const agents: AgentProfileRead[] = [
  {
    id: 'agent-1',
    tenant_id: 'tenant_demo',
    name: '小艾',
    is_overall: false,
    status: 'active',
    metadata: { owner_user_id: 'user-1' },
    resources: [],
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
  },
];

// 两条都是 user-1 创建的：weather 是广场技能（无宿主），expense 挂在 agent-1 下。
const mySkills: GeneralSkillRead[] = [
  {
    id: 's1',
    tenant_id: 'tenant_demo',
    slug: 'weather',
    name: '天气技能',
    description: '查询天气',
    skill_markdown: '# 天气',
    skill_files: [{ path: 'SKILL.md', content: '# 天气' }],
    metadata: { owner_user_id: 'user-1' },
    status: 'published',
    permissions: {},
    runtime_config: {},
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
  },
  {
    id: 's2',
    tenant_id: 'tenant_demo',
    slug: 'expense',
    name: '报销技能',
    description: '报销流程',
    skill_markdown: '# 报销',
    skill_files: [{ path: 'SKILL.md', content: '# 报销' }],
    metadata: { owner_user_id: 'user-1', owner_agent_id: 'agent-1' },
    status: 'published',
    permissions: {},
    runtime_config: {},
    created_at: '2026-09-02T00:00:00Z',
    updated_at: '2026-09-02T00:00:00Z',
  },
];

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function stubGalleryFetch(opts: {
  skills?: GeneralSkillRead[];
  skillsStatus?: number;
  skillsText?: string;
  teams?: TeamRead[];
}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/enterprise/teams')) return jsonResponse(opts.teams ?? []);
    if (url.includes('/api/enterprise/agents')) return jsonResponse(agents);
    if (url.includes('/api/enterprise/general-skills')) {
      if (opts.skillsStatus && opts.skillsStatus >= 400) {
        return {
          ok: false,
          status: opts.skillsStatus,
          statusText: 'Error',
          text: async () => opts.skillsText ?? 'Error',
        } as Response;
      }
      return jsonResponse(opts.skills ?? []);
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderGallery() {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={['/workspace/gallery']}>
        <EmployeeGalleryPage currentUser={currentUser} />
      </MemoryRouter>
    </I18nProvider>,
  );
}

beforeAll(() => {
  // Radix 在 jsdom 下需要这些 stub
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('我创建的技能 页面级集成', () => {
  it('在「我的数字员工」tab 渲染区块且请求 mine=1；其余三 tab 不出现该区块', async () => {
    const user = userEvent.setup();
    const fetchMock = stubGalleryFetch({ skills: mySkills });
    renderGallery();

    // 默认在「所有员工」：面板不应出现
    expect(screen.queryByRole('region', { name: '我创建的技能' })).toBeNull();

    await user.click(await screen.findByRole('tab', { name: '我的数字员工' }));

    const panel = await screen.findByRole('region', { name: '我创建的技能' });
    expect(panel).toBeTruthy();
    // 两条技能都列出来
    expect(within(panel).getAllByText('天气技能').length).toBeGreaterThan(0);
    expect(within(panel).getAllByText('报销技能').length).toBeGreaterThan(0);
    // 用的是 mine=1 这个 URL
    const mineCall = fetchMock.mock.calls.find(([url]) => String(url).includes('mine=1'));
    expect(mineCall).toBeTruthy();

    // 切到「所有员工」→ 区块消失
    await user.click(screen.getByRole('tab', { name: '所有员工' }));
    expect(screen.queryByRole('region', { name: '我创建的技能' })).toBeNull();

    // 切到「数字员工广场」→ 区块消失
    await user.click(screen.getByRole('tab', { name: '数字员工广场' }));
    expect(screen.queryByRole('region', { name: '我创建的技能' })).toBeNull();

    // 切到「团队对话」→ 区块消失
    await user.click(screen.getByRole('tab', { name: '团队对话' }));
    expect(screen.queryByRole('region', { name: '我创建的技能' })).toBeNull();
  }, 20000);

  it('顶部搜索框关键词透传给面板并过滤技能列表', async () => {
    const user = userEvent.setup();
    stubGalleryFetch({ skills: mySkills });
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '我的数字员工' }));
    const panel = await screen.findByRole('region', { name: '我创建的技能' });

    const search = screen.getByLabelText('搜索数字员工');
    fireEvent.change(search, { target: { value: '天气' } });

    await waitFor(() => {
      expect(within(panel).queryAllByText('报销技能')).toHaveLength(0);
    }, { timeout: 3000 });
    expect(within(panel).getAllByText('天气技能').length).toBeGreaterThan(0);
  }, 20000);

  it('mine=1 请求失败时只弹错误提示、员工网格照常渲染、页面不白屏', async () => {
    const user = userEvent.setup();
    const errorSpy = vi.spyOn(notify, 'error');
    stubGalleryFetch({ skillsStatus: 500, skillsText: 'Server Error' });
    renderGallery();

    await user.click(await screen.findByRole('tab', { name: '我的数字员工' }));

    // 员工卡片网格照常渲染（agent-1 属于当前用户）
    const grid = await screen.findByRole('region', { name: '我的数字员工' });
    expect(within(grid).getByText(/小艾/)).toBeTruthy();
    // 面板本身没崩（不白屏），只是空态
    expect(await screen.findByRole('region', { name: '我创建的技能' })).toBeTruthy();
    // 弹了错误提示
    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(errorSpy.mock.calls.some(([msg]) => String(msg).includes('Server Error'))).toBe(true);
  }, 20000);
});
