// @vitest-environment jsdom

// 员工转让弹窗：候选过滤、确认按钮的启用条件、请求体、成功/失败回调。
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { notify } from '@/components/ui/app-toast';
import { I18nProvider } from '@/i18n';
import type { EnterpriseAuthUser } from '@/auth';
import type { AgentProfileRead } from '@/types';

import EmployeeTransferDialog from './EmployeeTransferDialog';

const currentUser: EnterpriseAuthUser = {
  id: 'user-owner',
  username: 'owner',
  display_name: '张 Owner',
  tenant_id: 'tenant_demo',
  role: 'member',
};

const agent: AgentProfileRead = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '小艾',
  is_overall: false,
  status: 'active',
  metadata: { owner_user_id: 'user-owner', owner_username: 'owner' },
  resources: [
    { resource_type: 'knowledge_base', resource_id: 'kb-1', status: 'active' },
    { resource_type: 'knowledge_base', resource_id: 'kb-2', status: 'active' },
    { resource_type: 'skill', resource_id: 'sop-1', status: 'active' },
    { resource_type: 'tool', resource_id: 'tool-1', status: 'inactive' },
  ] as AgentProfileRead['resources'],
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
};

const members = [
  { id: 'user-owner', username: 'owner', display_name: '张 Owner', role: 'member', source: 'web' },
  { id: 'user-lisi', username: 'lisi', display_name: '李四', department: '研发', role: 'member', source: 'web' },
  { id: 'user-admin', username: 'admin', display_name: '王管理', role: 'admin', source: 'web' },
  { id: 'user-guest', username: 'guest', display_name: '访客', role: 'member', source: 'share' },
];

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function errorResponse(status: number, message: string): Response {
  return {
    ok: false,
    status,
    statusText: 'Error',
    text: async () => JSON.stringify({ detail: message }),
  } as Response;
}

function stubFetch(transferResponse: Response = jsonResponse({
  agent: { ...agent, metadata: { ...agent.metadata, owner_user_id: 'user-lisi' } },
  transferred_resources: 3,
  transferred_breakdown: { knowledge_base: 2, skill: 1 },
  owner_username: 'lisi',
})) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes('/api/auth/users')) return jsonResponse(members);
    if (url.includes('/transfer')) return transferResponse;
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function renderDialog(overrides: Partial<Parameters<typeof EmployeeTransferDialog>[0]> = {}) {
  const onClose = vi.fn();
  const onTransferred = vi.fn();
  render(
    <I18nProvider>
      <EmployeeTransferDialog
        agent={agent}
        open
        currentUser={currentUser}
        onClose={onClose}
        onTransferred={onTransferred}
        {...overrides}
      />
    </I18nProvider>,
  );
  return { onClose, onTransferred };
}

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  vi.spyOn(notify, 'success').mockImplementation(() => 'toast' as never);
  vi.spyOn(notify, 'error').mockImplementation(() => 'toast' as never);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('EmployeeTransferDialog', () => {
  it('候选里只剩其他正式成员：当前 owner 与分享访客都不出现', async () => {
    stubFetch();
    renderDialog();

    expect(await screen.findByText('李四')).toBeTruthy();
    expect(screen.getByText('王管理')).toBeTruthy();
    expect(screen.queryByText('访客')).toBeNull();
    // 当前 owner 自己不在候选里（弹窗标题里会出现名字，所以按 radiogroup 内查）
    const group = screen.getByRole('radiogroup', { name: '选择接收成员' });
    expect(group.textContent).not.toContain('张 Owner');
  });

  it('未选人时确认按钮禁用，选中后启用并在按钮左侧标出目标成员', async () => {
    stubFetch();
    renderDialog();

    const confirm = await screen.findByRole('button', { name: '确认转让' });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('请选择接收成员')).toBeTruthy();

    await userEvent.click(await screen.findByRole('radio', { name: /李四/ }));
    expect((screen.getByRole('button', { name: '确认转让' }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText('转给 李四')).toBeTruthy();
  });

  it('确认后按契约提交，并把服务端返回的员工交回列表', async () => {
    const fetchMock = stubFetch();
    const { onTransferred, onClose } = renderDialog();

    await userEvent.click(await screen.findByRole('radio', { name: /李四/ }));
    await userEvent.click(screen.getByRole('button', { name: '确认转让' }));

    await waitFor(() => expect(onTransferred).toHaveBeenCalledTimes(1));
    const transferCall = fetchMock.mock.calls.find(([url]) => String(url).includes('/transfer'));
    expect(transferCall).toBeTruthy();
    expect(String(transferCall?.[0])).toContain('/api/enterprise/agents/agent-1/transfer');
    expect(JSON.parse(String(transferCall?.[1]?.body))).toEqual({
      tenant_id: 'tenant_demo',
      owner_user_id: 'user-lisi',
    });
    expect(onTransferred.mock.calls[0][0].metadata.owner_user_id).toBe('user-lisi');
    expect(onClose).toHaveBeenCalled();
    // 提示里要带上「连同 N 项资源」，否则用户会以为只是换了个名字
    expect(notify.success).toHaveBeenCalledWith(expect.stringContaining('3 项资源'));
  });

  it('失败时留在弹窗里报错，不回调成功', async () => {
    stubFetch(errorResponse(403, '只有创建者或管理员可以转让'));
    const { onTransferred, onClose } = renderDialog();

    await userEvent.click(await screen.findByRole('radio', { name: /李四/ }));
    await userEvent.click(screen.getByRole('button', { name: '确认转让' }));

    expect(await screen.findByText('只有创建者或管理员可以转让')).toBeTruthy();
    expect(onTransferred).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });
});
