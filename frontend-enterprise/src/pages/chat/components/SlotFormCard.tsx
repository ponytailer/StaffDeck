import { useMemo, useState } from 'react';

import StaffdeckIcon from '@/components/StaffdeckIcon';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/utils';

import {
  CHAT_SLOT_FORM_CARD_CLASS,
  CHAT_SLOT_FORM_CHOICE_ACTIVE_CLASS,
  CHAT_SLOT_FORM_CHOICE_CLASS,
  CHAT_SLOT_FORM_CHOICE_ROW_CLASS,
  CHAT_SLOT_FORM_ERROR_CLASS,
  CHAT_SLOT_FORM_FIELD_CLASS,
  CHAT_SLOT_FORM_FOOTER_CLASS,
  CHAT_SLOT_FORM_GRID_CLASS,
  CHAT_SLOT_FORM_HEADER_CLASS,
  CHAT_SLOT_FORM_HEADING_CLASS,
  CHAT_SLOT_FORM_HINT_CLASS,
  CHAT_SLOT_FORM_ICON_CLASS,
  CHAT_SLOT_FORM_KICKER_CLASS,
  CHAT_SLOT_FORM_LABEL_CLASS,
  CHAT_SLOT_FORM_REQUIRED_CLASS,
  CHAT_SLOT_FORM_SUBMITTED_CLASS,
  CHAT_SLOT_FORM_TITLE_CLASS,
} from '../chatPageStyles';
import type { A2UIField, A2UIForm } from '../chatTypes';

type SlotFormCardProps = {
  form: A2UIForm;
  onSubmit: (values: Record<string, unknown>) => void;
  disabled?: boolean;
};

function isEmptyValue(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  return typeof value === 'string' && value.trim() === '';
}

/** A2UI 表单卡片：后端下发字段描述，这里渲染成原生控件并回传结构化取值。 */
export default function SlotFormCard({ form, onSubmit, disabled = false }: SlotFormCardProps) {
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);

  const title = form.title || '请补充以下信息';
  const requiredFields = useMemo(
    () => form.fields.filter((field) => field.required !== false),
    [form.fields],
  );
  // 确认型表单（全部字段都是 confirm）：按钮即提交，不需要底部通用提交键。
  const allConfirm = form.fields.length > 0
    && form.fields.every((field) => field.type === 'confirm');

  if (submitted) {
    return (
      <div className={CHAT_SLOT_FORM_CARD_CLASS}>
        <div className={CHAT_SLOT_FORM_SUBMITTED_CLASS}>
          <StaffdeckIcon name="check" size={16} />
          已提交，正在继续处理
        </div>
      </div>
    );
  }

  const setValue = (field: A2UIField, next: unknown) => {
    setValues((current) => ({ ...current, [field.name]: next }));
    setErrors((current) => {
      if (!current[field.name]) return current;
      const rest = { ...current };
      delete rest[field.name];
      return rest;
    });
  };

  const handleSubmit = () => {
    if (disabled) return;
    const nextErrors: Record<string, string> = {};
    for (const field of requiredFields) {
      if (isEmptyValue(values[field.name])) {
        nextErrors[field.name] = field.type === 'text' || field.type === 'number' || field.type === 'date' || field.type === 'time' || field.type === 'datetime'
          ? '请填写'
          : '请选择';
      }
    }
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      return;
    }
    const payload: Record<string, unknown> = {};
    for (const field of form.fields) {
      const value = values[field.name];
      if (isEmptyValue(value)) continue;
      payload[field.name] = field.type === 'number' ? Number(value) : value;
    }
    setSubmitted(true);
    onSubmit(payload);
  };

  /** 确认按钮直提：带上当前已填的值 + 被点击的这一项，立即提交。 */
  const submitConfirm = (field: A2UIField, optionValue: string | number | boolean) => {
    if (disabled) return;
    const payload: Record<string, unknown> = {};
    for (const item of form.fields) {
      const value = item.name === field.name ? optionValue : values[item.name];
      if (isEmptyValue(value)) continue;
      payload[item.name] = item.type === 'number' ? Number(value) : value;
    }
    setSubmitted(true);
    onSubmit(payload);
  };

  const renderControl = (field: A2UIField) => {
    const value = values[field.name];
    if (field.type === 'confirm') {
      // 确认型字段：渲染成按钮，整表都是确认字段时点击即提交（不用再点提交键）。
      const options = field.options?.length
        ? field.options
        : [{ value: '确认', label: '确认' }, { value: '取消', label: '取消' }];
      return (
        <div className={CHAT_SLOT_FORM_CHOICE_ROW_CLASS}>
          {options.map((option, index) => (
            <button
              key={String(option.value)}
              type="button"
              disabled={disabled}
              className={cn(
                CHAT_SLOT_FORM_CHOICE_CLASS,
                index === 0 && 'border-primary/60 bg-primary/10 text-primary font-medium',
                value === option.value && CHAT_SLOT_FORM_CHOICE_ACTIVE_CLASS,
              )}
              onClick={() => (
                allConfirm
                  ? submitConfirm(field, option.value)
                  : setValue(field, option.value)
              )}
            >
              {option.label}
            </button>
          ))}
        </div>
      );
    }
    if (field.type === 'boolean') {
      const options = field.options?.length
        ? field.options
        : [{ value: true, label: '是' }, { value: false, label: '否' }];
      return (
        <div className={CHAT_SLOT_FORM_CHOICE_ROW_CLASS}>
          {options.map((option) => (
            <button
              key={String(option.value)}
              type="button"
              disabled={disabled}
              className={cn(
                CHAT_SLOT_FORM_CHOICE_CLASS,
                value === option.value && CHAT_SLOT_FORM_CHOICE_ACTIVE_CLASS,
              )}
              aria-pressed={value === option.value}
              onClick={() => setValue(field, option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>
      );
    }
    if (field.type === 'select') {
      const options = field.options ?? [];
      const selected = options.find((option) => String(option.value) === String(value ?? ''));
      return (
        <Select
          value={selected ? String(selected.value) : ''}
          onValueChange={(next) => {
            const match = options.find((option) => String(option.value) === next);
            setValue(field, match ? match.value : next);
          }}
        >
          <SelectTrigger disabled={disabled} className="h-[36px] text-[13px]">
            <SelectValue placeholder={field.placeholder || '请选择'} />
          </SelectTrigger>
          <SelectContent>
            {options.map((option) => (
              <SelectItem key={String(option.value)} value={String(option.value)}>
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
    const inputType = field.type === 'datetime'
      ? 'datetime-local'
      : field.type === 'number'
        ? 'number'
        : field.type;
    return (
      <Input
        id={`a2ui-${field.name}`}
        type={inputType}
        disabled={disabled}
        className="h-[36px] text-[13px]"
        placeholder={field.placeholder || `请输入${field.label}`}
        value={value === undefined || value === null ? '' : String(value)}
        onChange={(event) => setValue(field, event.target.value)}
      />
    );
  };

  return (
    <div className={CHAT_SLOT_FORM_CARD_CLASS}>
      <div className={CHAT_SLOT_FORM_HEADER_CLASS}>
        <span className={CHAT_SLOT_FORM_ICON_CLASS}>
          <StaffdeckIcon name="edit" size={16} />
        </span>
        <span className={CHAT_SLOT_FORM_HEADING_CLASS}>
          <span className={CHAT_SLOT_FORM_TITLE_CLASS}>{title}</span>
          {form.step_name && (
            <span className={CHAT_SLOT_FORM_KICKER_CLASS} data-i18n-ignore>
              {form.step_name}
            </span>
          )}
        </span>
      </div>

      <div className={CHAT_SLOT_FORM_GRID_CLASS}>
        {form.fields.map((field) => (
          <div className={CHAT_SLOT_FORM_FIELD_CLASS} key={field.name}>
            <label className={CHAT_SLOT_FORM_LABEL_CLASS} htmlFor={`a2ui-${field.name}`}>
              {field.label}
              {field.required !== false && <span className={CHAT_SLOT_FORM_REQUIRED_CLASS}>*</span>}
            </label>
            {renderControl(field)}
            {errors[field.name] && (
              <span className={CHAT_SLOT_FORM_ERROR_CLASS}>{errors[field.name]}</span>
            )}
          </div>
        ))}
      </div>

      <div className={CHAT_SLOT_FORM_FOOTER_CLASS}>
        <span className={CHAT_SLOT_FORM_HINT_CLASS}>
          {disabled
            ? '该表单已过期，请直接回复文字'
            : allConfirm
              ? '点击按钮即提交'
              : '填写后将直接作为结构化信息提交，无需再描述一遍'}
        </span>
        {!allConfirm && (
          <Button type="button" disabled={disabled} onClick={handleSubmit}>
            {form.submit_label || '提交'}
          </Button>
        )}
      </div>
    </div>
  );
}
