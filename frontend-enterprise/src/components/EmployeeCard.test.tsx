// @vitest-environment jsdom

// 员工卡片右上角菜单：只验证「员工转让」这个新入口的可见性与触发，
// 其余菜单项的既有行为不在本文件覆盖。
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { AgentProfileRead } from '@/types';

import EmployeeCard from './EmployeeCard';

const agent: AgentProfileRead = {
  id: 'agent-1',
  tenant_id: 'tenant_demo',
  name: '胡鹏飞Jerry',
  is_overall: false,
  status: 'active',
  metadata: { owner_user_id: 'user-1', role_name: '研发' },
  resources: [],
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
};

function renderCard(overrides: Partial<React.ComponentProps<typeof EmployeeCard>> = {}) {
  const onTransfer = vi.fn();
  render(
    <I18nProvider>
      <EmployeeCard
        employee={agent}
        canManage
        onOpen={() => {}}
        onStatus={() => {}}
        onGallery={() => {}}
        onDelete={() => {}}
        onAvatar={() => {}}
        onEdit={() => {}}
        onChat={() => {}}
        onTransfer={onTransfer}
        {...overrides}
      />
    </I18nProvider>,
  );
  return { onTransfer };
}

beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.releasePointerCapture = () => {};
  Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('EmployeeCard 员工转让入口', () => {
  it('有管理权时菜单里出现「员工转让」，点击回调一次', async () => {
    const { onTransfer } = renderCard();

    await userEvent.click(screen.getByRole('button', { name: '员工操作' }));
    const item = await screen.findByRole('menuitem', { name: /员工转让/ });
    await userEvent.click(item);

    expect(onTransfer).toHaveBeenCalledTimes(1);
  });

  it('没有管理权时该项禁用（不隐藏，用户能看见「为什么不能点」）', async () => {
    const { onTransfer } = renderCard({ canManage: false });

    await userEvent.click(screen.getByRole('button', { name: '员工操作' }));
    const item = await screen.findByRole('menuitem', { name: /员工转让/ });
    expect(item.getAttribute('data-disabled')).not.toBeNull();

    await userEvent.click(item);
    expect(onTransfer).not.toHaveBeenCalled();
  });

  it('开放广场（overall）卡片不显示转让：它属于全租户，没有 owner 可换', async () => {
    renderCard({
      employee: { ...agent, is_overall: true, name: '开放广场' },
      canManage: true,
    });

    await userEvent.click(screen.getByRole('button', { name: '员工操作' }));
    expect(await screen.findByRole('menuitem', { name: /删除/ })).toBeTruthy();
    expect(screen.queryByRole('menuitem', { name: /员工转让/ })).toBeNull();
  });
});
