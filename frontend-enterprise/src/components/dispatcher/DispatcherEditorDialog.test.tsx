// @vitest-environment jsdom

/**
 * 调度入口编辑器的保存/试跑校验。
 *
 * 这些断言针对用户明确要求的那条规则：**成员数至少要 >1，保存或者试运行时得检查**。
 * 所以三个入口都要拦：保存按钮、试跑按钮、以及后端返回的兜底。
 */

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { DispatcherRead } from '@/lib/dispatcher';
import type { AgentProfileRead, ModelConfigRead } from '@/types';

import { DISPATCHER_AVATAR_PRESETS } from './DispatcherAvatar';
import DispatcherEditorDialog from './DispatcherEditorDialog';

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

const models: ModelConfigRead[] = [
  {
    id: 'model-1',
    tenant_id: 'tenant_demo',
    name: 'deepseek-v4-flash',
    model: 'deepseek-v4-flash',
    enabled: true,
    is_default: true,
  } as ModelConfigRead,
];

function agent(id: string, name: string): AgentProfileRead {
  return {
    id,
    tenant_id: 'tenant_demo',
    name,
    is_overall: false,
    status: 'active',
    metadata: {},
    resources: [],
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  } as AgentProfileRead;
}

const agents = [agent('agent-a', '小王'), agent('agent-b', '小李'), agent('agent-c', '小陈')];

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
  if (!HTMLElement.prototype.scrollIntoView) {
    HTMLElement.prototype.scrollIntoView = () => {};
  }
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderEditor(overrides: Partial<Parameters<typeof DispatcherEditorDialog>[0]> = {}) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  render(
    <I18nProvider>
      <DispatcherEditorDialog
        open
        dispatcher={null}
        agents={agents}
        models={models}
        onClose={onClose}
        onSaved={onSaved}
        {...overrides}
      />
    </I18nProvider>,
  );
  return { onClose, onSaved };
}

/** 走「添加同事」选择器把成员加进去。 */
async function addMember(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(screen.getByRole('button', { name: /添加同事/ }));
  await user.click(await screen.findByText(name));
}

describe('DispatcherEditorDialog 三段式骨架', () => {
  it('渲染三段标题与模型必填标记', () => {
    renderEditor();
    expect(screen.getByText('基础信息')).toBeTruthy();
    expect(screen.getByText('成员与路由规则')).toBeTruthy();
    expect(screen.getByText('无法匹配时怎么办')).toBeTruthy();
    // 模型必填：下拉里带「请选择模型（必填）」
    expect(screen.getByText(/请选择模型（必填）/)).toBeTruthy();
  });

  it('空态显示 0 / 3 与下限提示', () => {
    renderEditor();
    expect(screen.getByText('0 / 3')).toBeTruthy();
    expect(screen.getByText(/至少要有 2 个同事/)).toBeTruthy();
  });

  it('不填名称时保存被拦', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderEditor();

    await user.click(screen.getByRole('button', { name: '新建小队' }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('DispatcherEditorDialog 成员数校验', () => {
  it('只有 1 个成员时保存被拦，且不发请求', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderEditor();

    await user.type(screen.getByPlaceholderText('例如：人事服务台'), '人事服务台');
    await addMember(user, '小王');
    expect(screen.getByText('1 / 3')).toBeTruthy();

    await user.click(screen.getByRole('button', { name: '新建小队' }));
    expect(fetchMock).not.toHaveBeenCalled();
    // 计数徽标进入警示态以外，提示条必须解释为什么不行
    expect(screen.getByText(/只有 1 个成员时没有「选谁」的判断/)).toBeTruthy();
  });

  it('只有 1 个成员时试跑被拦，不发 preview 请求', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderEditor();

    await addMember(user, '小王');
    await user.type(
      screen.getByPlaceholderText(/输入一句用户可能会问的话/),
      '报销怎么提交',
    );
    await user.click(screen.getByRole('button', { name: '试一下' }));

    expect(fetchMock).not.toHaveBeenCalled();
    expect(await screen.findByText('无法试跑')).toBeTruthy();
  });

  it('3 个成员时不再提示下限', async () => {
    const user = userEvent.setup();
    renderEditor();
    await addMember(user, '小王');
    await addMember(user, '小李');
    await addMember(user, '小陈');
    expect(screen.getByText('3 / 3')).toBeTruthy();
    expect(screen.queryByText(/至少要有 2 个同事/)).toBeNull();
    // 达到上限后「添加同事」按钮禁用并换文案
    expect(screen.getByRole('button', { name: /已达上限 3 个同事/ })).toBeTruthy();
  });

  it('成员缺路由规则时保存被拦', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderEditor();

    await user.type(screen.getByPlaceholderText('例如：人事服务台'), '人事服务台');
    await addMember(user, '小王');
    await addMember(user, '小李');

    await user.click(screen.getByRole('button', { name: '新建小队' }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('DispatcherEditorDialog 提交载荷', () => {
  it('两个成员 + 规则 → payload 形状正确', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        id: 'agent_disp',
        tenant_id: 'tenant_demo',
        name: '人事服务台',
        kind: 'dispatcher',
        status: 'active',
        members: [],
        config: {},
        usage_count: 0,
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    const { onSaved } = renderEditor();

    await user.type(screen.getByPlaceholderText('例如：人事服务台'), '人事服务台');
    await addMember(user, '小王');
    await addMember(user, '小李');

    const ruleInputs = screen.getAllByPlaceholderText(/员工咨询入职材料/);
    await user.type(ruleInputs[0], '入职、离职、考勤异常');
    await user.type(ruleInputs[1], '薪资、个税、社保');

    await user.click(screen.getByRole('button', { name: '新建小队' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(String(call[1].body));
    expect(body.name).toBe('人事服务台');
    // 模型必填且默认预选了一条
    expect(body.model_config_id).toBe('model-1');
    expect(body.members).toHaveLength(2);
    expect(body.members[0]).toMatchObject({
      member_agent_id: 'agent-a',
      routing_rule: '入职、离职、考勤异常',
      sort_order: 0,
    });
    expect(body.config.fallback_mode).toBe('clarify');
    // clarify 模式下不该把默认成员带上去
    expect(body.config.default_member_agent_id).toBeNull();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it('切成「转给默认同事」时带上 default_member_agent_id', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        id: 'agent_disp',
        tenant_id: 'tenant_demo',
        name: '人事服务台',
        kind: 'dispatcher',
        status: 'active',
        members: [],
        config: {},
        usage_count: 0,
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderEditor();

    await user.type(screen.getByPlaceholderText('例如：人事服务台'), '人事服务台');
    await addMember(user, '小王');
    await addMember(user, '小李');
    const ruleInputs = screen.getAllByPlaceholderText(/员工咨询入职材料/);
    await user.type(ruleInputs[0], '入职流程');
    await user.type(ruleInputs[1], '薪资核算');

    await user.click(screen.getByText('转给默认同事'));
    await user.click(screen.getByRole('button', { name: '新建小队' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    const body = JSON.parse(String(call[1].body));
    expect(body.config.fallback_mode).toBe('default_member');
    // 切过去时自动预选第一位成员，避免用户漏选
    expect(body.config.default_member_agent_id).toBe('agent-a');
  });

  /**
   * 回归（用户原话「小队档案，调整和配置跳转的页面都是错误的」）：
   * 从档案页「去配置 / 调整」进来时带 focusSection，弹窗必须挂上可定位的
   * DOM 锚点。之前依赖 `Section`（函数组件）的 ref 透传，React 18 下
   * ref 拿不到、scrollTop 恒为 0；现在用 data-dispatch-section + 滚动容器
   * 原生 ref，两个锚点都得真实存在。
   */
  it.each(['members', 'fallback'] as const)(
    '带 focusSection=%s 打开时，弹窗带定位标记且目标段有 DOM 锚点',
    async (focusSection) => {
      renderEditor({ focusSection });
      const dialog = await screen.findByRole('dialog');
      expect(dialog.getAttribute('data-focus-section')).toBe(focusSection);
      expect(dialog.querySelector(`[data-dispatch-section="${focusSection}"]`)).toBeTruthy();
    },
  );
});

/**
 * 用户原话「小队头像搞几个可选的，不要只有一个」：编辑器里要有多个可选几何头像，
 * 选中的 key 走 `avatar_preset` 落到 AgentProfile.metadata_json，编辑时要回填。
 */
describe('DispatcherEditorDialog 小队头像', () => {
  function dispatcherFixture(overrides: Record<string, unknown> = {}) {
    return {
      id: 'agent_disp_1',
      tenant_id: 'tenant_demo',
      name: '人事服务台',
      description: null,
      kind: 'dispatcher',
      status: 'active',
      model_config_id: 'model-1',
      members: [],
      config: {
        fallback_mode: 'clarify',
        fallback_reply: '换个说法再试一次？',
        default_member_agent_id: null,
        offer_candidates: true,
        min_confidence: 0.6,
      },
      usage_count: 0,
      avatar_preset: null,
      ...overrides,
    } as unknown as DispatcherRead;
  }

  it('给出 6 个预设且默认选中「拓扑」', () => {
    renderEditor();

    // 「无法匹配时怎么办」那段也有 radio，所以只数头像这一组
    const group = screen.getByRole('radiogroup', { name: '选择小队头像' });
    expect(group.querySelectorAll('[role="radio"]')).toHaveLength(DISPATCHER_AVATAR_PRESETS.length);
    expect(screen.getByRole('radio', { name: '拓扑' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('radio', { name: '环轨' }).getAttribute('aria-checked')).toBe('false');
  });

  it('切换头像后提交的 avatar_preset 跟着变', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        id: 'agent_disp',
        tenant_id: 'tenant_demo',
        name: '人事服务台',
        kind: 'dispatcher',
        status: 'active',
        members: [],
        config: {},
        usage_count: 0,
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderEditor();

    await user.click(screen.getByRole('radio', { name: '守门' }));
    expect(screen.getByRole('radio', { name: '守门' }).getAttribute('aria-checked')).toBe('true');

    await user.type(screen.getByPlaceholderText('例如：人事服务台'), '人事服务台');
    await addMember(user, '小王');
    await addMember(user, '小李');
    const ruleInputs = screen.getAllByPlaceholderText(/员工咨询入职材料/);
    await user.type(ruleInputs[0], '入职流程');
    await user.type(ruleInputs[1], '薪资核算');

    await user.click(screen.getByRole('button', { name: '新建小队' }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(call[1].body)).avatar_preset).toBe('shield');
  });

  it('编辑已有小队时回填已保存的头像', () => {
    renderEditor({ dispatcher: dispatcherFixture({ avatar_preset: 'layers' }) });

    expect(screen.getByRole('radio', { name: '分层' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByRole('radio', { name: '拓扑' }).getAttribute('aria-checked')).toBe('false');
  });

  it('历史小队没有 avatar_preset 时回落到「拓扑」', () => {
    renderEditor({ dispatcher: dispatcherFixture() });

    expect(screen.getByRole('radio', { name: '拓扑' }).getAttribute('aria-checked')).toBe('true');
  });
});