// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';

import type { A2UIForm } from '../chatTypes';
import SlotFormCard from './SlotFormCard';

/** 表单控件来自 ui/*，依赖 I18nProvider（Input 会翻译 placeholder）。 */
function renderCard(element: ReactElement) {
  return render(<I18nProvider>{element}</I18nProvider>);
}

// 项目 vitest 未开 globals，RTL 的自动清理不生效，必须显式 cleanup，
// 否则前一个用例的 DOM 会残留 → getByText 命中多个元素。
afterEach(cleanup);

const form: A2UIForm = {
  kind: 'slot_form',
  step_name: '收集证明需求信息',
  title: '请补充以下信息',
  submit_label: '提交',
  fields: [
    {
      name: 'employee_id',
      label: '员工工号',
      type: 'text',
      required: true,
      placeholder: '请输入员工工号',
    },
    {
      name: 'include_income',
      label: '是否含收入项',
      type: 'boolean',
      required: true,
      options: [
        { value: true, label: '是' },
        { value: false, label: '否' },
      ],
    },
    {
      name: 'amount',
      label: '金额',
      type: 'number',
      required: false,
    },
  ],
};

describe('SlotFormCard', () => {
  it('渲染后端下发的字段与中文标签', () => {
    renderCard(<SlotFormCard form={form} onSubmit={vi.fn()} />);

    expect(screen.getByText('请补充以下信息')).toBeTruthy();
    expect(screen.getByText('收集证明需求信息')).toBeTruthy();
    expect(screen.getByLabelText(/员工工号/)).toBeTruthy();
    expect(screen.getByText('是否含收入项')).toBeTruthy();
    expect(screen.getByText('是')).toBeTruthy();
    expect(screen.getByText('否')).toBeTruthy();
  });

  it('必填未填时拦住提交并给出提示', () => {
    const onSubmit = vi.fn();
    renderCard(<SlotFormCard form={form} onSubmit={onSubmit} />);

    fireEvent.click(screen.getByRole('button', { name: '提交' }));

    expect(onSubmit).not.toHaveBeenCalled();
    // 文本字段与布尔字段都要提示（布尔没有「空文本」这回事）
    expect(screen.getByText('请填写')).toBeTruthy();
    expect(screen.getByText('请选择')).toBeTruthy();
  });

  it('提交时回传结构化取值，数字字段转成数字', () => {
    const onSubmit = vi.fn();
    renderCard(<SlotFormCard form={form} onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText(/员工工号/), { target: { value: '3012' } });
    fireEvent.click(screen.getByText('是'));
    fireEvent.change(screen.getByLabelText(/金额/), { target: { value: '120' } });
    fireEvent.click(screen.getByRole('button', { name: '提交' }));

    expect(onSubmit).toHaveBeenCalledWith({
      employee_id: '3012',
      include_income: true,
      amount: 120,
    });
    expect(screen.getByText('已提交，正在继续处理')).toBeTruthy();
  });

  it('「否」也是有效取值，不能被当成没填', () => {
    const onSubmit = vi.fn();
    renderCard(<SlotFormCard form={form} onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText(/员工工号/), { target: { value: '3012' } });
    fireEvent.click(screen.getByText('否'));
    fireEvent.click(screen.getByRole('button', { name: '提交' }));

    expect(onSubmit).toHaveBeenCalledWith({ employee_id: '3012', include_income: false });
  });

  it('过期表单（disabled）不触发提交', () => {
    const onSubmit = vi.fn();
    renderCard(<SlotFormCard form={form} onSubmit={onSubmit} disabled />);

    fireEvent.click(screen.getByText('是'));
    fireEvent.click(screen.getByRole('button', { name: '提交' }));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText('该表单已过期，请直接回复文字')).toBeTruthy();
  });
});
