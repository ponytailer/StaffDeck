// @vitest-environment jsdom

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import PlatformEmployeeCard from './PlatformEmployeeCard';

function renderCard(onUnpublish?: () => void, freeBadge = false) {
  const onOpen = vi.fn();
  render(
    <PlatformEmployeeCard
      avatar={<span>avatar</span>}
      name="Finance Employee"
      role="Finance"
      description="Handles finance workflows"
      stats={[{ label: 'SOP', value: 2 }]}
      freeBadge={freeBadge}
      onOpen={onOpen}
      onUnpublish={onUnpublish}
    />,
  );
  return { onOpen };
}

describe('PlatformEmployeeCard gallery governance', () => {
  it('does not expose the unpublish action without admin callback', () => {
    renderCard();
    expect(screen.queryByRole('button', { name: '从广场下线' })).toBeNull();
  });

  it('runs the unpublish action without opening employee details', async () => {
    const user = userEvent.setup();
    const onUnpublish = vi.fn();
    const { onOpen } = renderCard(onUnpublish);

    await user.click(screen.getByRole('button', { name: '从广场下线' }));

    expect(onUnpublish).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('免费员工显示「免费」徽标，普通员工不显示', () => {
    renderCard(undefined, true);
    expect(screen.getByText('免费')).toBeTruthy();

    renderCard(undefined, false);
    // 上一张卡片的徽标还在，但这一张没有自己的徽标（用 getAllByText 计数区分）
    expect(screen.getAllByText('免费')).toHaveLength(1);
  });
});
