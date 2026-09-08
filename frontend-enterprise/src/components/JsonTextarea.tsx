import { useState } from 'react';
import { cn } from '@/lib/utils';

type JsonTextareaProps = {
  value: string;
  onChange: (value: string) => void;
  label: string;
  hint?: string;
  placeholder?: string;
  rows?: number;
  minHeight?: number;
};

/**
 * JSON 友好的多行输入框：
 * - Tab 键插入两个空格缩进（而不是跳出焦点）
 * - 失焦时合法 JSON 自动格式化（prettify）
 * - 实时校验：非法 JSON 时红边框 + 错误提示
 */
export function JsonTextarea({
  value,
  onChange,
  label,
  hint,
  placeholder,
  rows = 4,
  minHeight = 96,
}: JsonTextareaProps) {
  const [error, setError] = useState<string | null>(null);

  function validate(text: string): string | null {
    const trimmed = text.trim();
    if (!trimmed) return null; // 允许留空（视作 {}）
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        return '必须是 JSON 对象（以 { 开始）';
      }
      return null;
    } catch (e) {
      return e instanceof Error && e.message
        ? `JSON 语法错误：${e.message}`
        : 'JSON 语法错误';
    }
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Tab') return;
    event.preventDefault();
    const el = event.currentTarget;
    const { selectionStart, selectionEnd } = el;
    const next = `${value.slice(0, selectionStart)}  ${value.slice(selectionEnd)}`;
    onChange(next);
    // 恢复光标到插入点之后
    requestAnimationFrame(() => {
      el.selectionStart = el.selectionEnd = selectionStart + 2;
    });
  }

  function handleBlur() {
    const trimmed = value.trim();
    if (!trimmed) {
      setError(null);
      return;
    }
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setError('必须是 JSON 对象（以 { 开始）');
        return;
      }
      // 合法对象：格式化（保留原始字符串如果已是最优格式则不变）
      const pretty = JSON.stringify(parsed, null, 2);
      if (pretty !== value) onChange(pretty);
      setError(null);
    } catch {
      // 非法：保留原文让用户继续编辑，错误由 validate 状态提示
    }
  }

  function handleChange(event: React.ChangeEvent<HTMLTextAreaElement>) {
    const next = event.target.value;
    onChange(next);
    setError(validate(next));
  }

  return (
    <div>
      <label className="flex flex-col gap-[6px]">
        <span className="text-[12px] font-medium text-[#464c5e]">{label}</span>
        <textarea
          data-slot="json-textarea"
          rows={rows}
          value={value}
          placeholder={placeholder}
          spellCheck={false}
          onKeyDown={handleKeyDown}
          onBlur={handleBlur}
          onChange={handleChange}
          className={cn(
            'flex field-sizing-fixed w-full resize-y overflow-y-auto rounded-lg border bg-transparent px-2.5 py-2 font-mono text-[12px] leading-[1.6] transition-colors outline-none',
            'placeholder:text-muted-foreground focus-visible:ring-3',
            error
              ? 'border-[#e5484d] focus-visible:border-[#e5484d] focus-visible:ring-[#e5484d]/20'
              : 'border-input focus-visible:border-ring focus-visible:ring-ring/50',
          )}
          style={{ minHeight }}
        />
      </label>
      {(error || hint) && (
        <p
          className={cn(
            'mt-[4px] text-[12px] leading-[1.5]',
            error ? 'text-[#e5484d]' : 'text-[#858b9c]',
          )}
        >
          {error || hint}
        </p>
      )}
    </div>
  );
}
