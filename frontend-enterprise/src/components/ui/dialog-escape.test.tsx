// @vitest-environment jsdom
//
// 回归：Esc 关闭弹窗/抽屉。
//
// 背景：dialog.tsx / alert-dialog.tsx / sheet.tsx 三个原语曾经在
// `onEscapeKeyDown` 里无条件 `event.preventDefault()`，而全仓库没有任何调用方传这个
// prop —— 结果是 Radix 默认的「Esc 关闭」被永久禁用，任何弹窗和抽屉都只能点遮罩或
// 按钮关闭。这里把三个原语的 Esc 行为钉住。

import { useState } from 'react';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ConfirmDialog } from '@/components/ConfirmDialog';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet';

afterEach(cleanup);

function EscDialog({ onChange }: { onChange: (open: boolean) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        onChange(next);
      }}
    >
      <DialogContent>
        <DialogTitle>标题</DialogTitle>
      </DialogContent>
    </Dialog>
  );
}

describe('Esc 关闭弹窗', () => {
  it('Dialog：按 Esc 会请求关闭', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<EscDialog onChange={onChange} />);

    expect(await screen.findByText('标题')).toBeTruthy();
    await user.keyboard('{Escape}');

    expect(onChange).toHaveBeenCalledWith(false);
  });

  it('Sheet：按 Esc 会请求关闭', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <Sheet
          open={open}
          onOpenChange={(next) => {
            setOpen(next);
            onChange(next);
          }}
        >
          <SheetContent>
            <SheetTitle>抽屉标题</SheetTitle>
          </SheetContent>
        </Sheet>
      );
    }
    render(<Harness />);

    expect(await screen.findByText('抽屉标题')).toBeTruthy();
    await user.keyboard('{Escape}');

    expect(onChange).toHaveBeenCalledWith(false);
  });

  it('ConfirmDialog：按 Esc 等同取消', async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    const onConfirm = vi.fn();
    render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="删除该会话？"
        onConfirm={onConfirm}
      />,
    );

    await user.keyboard('{Escape}');

    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('ConfirmDialog：loading 时按 Esc 不关闭（进行中的删除不可被中断）', async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <ConfirmDialog
        open
        loading
        onOpenChange={onOpenChange}
        title="删除该会话？"
        onConfirm={() => {}}
      />,
    );

    await user.keyboard('{Escape}');

    expect(onOpenChange).not.toHaveBeenCalled();
  });
});
