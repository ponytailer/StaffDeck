import { useState } from 'react';

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  notify,
} from '@/components/ui';
import { Eye, EyeOff, Lock } from 'lucide-react';

import { api } from '../api/client';

export type ChangePasswordDialogProps = {
  open: boolean;
  onClose: () => void;
};

type FieldKey = 'old_password' | 'new_password' | 'confirm_password';

export default function ChangePasswordDialog({ open, onClose }: ChangePasswordDialogProps) {
  const [values, setValues] = useState({
    old_password: '',
    new_password: '',
    confirm_password: '',
  });
  const [visible, setVisible] = useState<Record<FieldKey, boolean>>({
    old_password: false,
    new_password: false,
    confirm_password: false,
  });
  const [loading, setLoading] = useState(false);
  const [errors, setErrors] = useState<Partial<Record<FieldKey, string>>>({});

  function reset() {
    setValues({ old_password: '', new_password: '', confirm_password: '' });
    setVisible({ old_password: false, new_password: false, confirm_password: false });
    setErrors({});
  }

  function handleOpenChange(next: boolean) {
    if (!next) {
      if (!loading) {
        reset();
        onClose();
      }
    }
  }

  function validate(): boolean {
    const next: Partial<Record<FieldKey, string>> = {};
    if (!values.old_password.trim()) {
      next.old_password = '请输入当前密码';
    }
    if (values.new_password.length < 6) {
      next.new_password = '新密码至少需要 6 位';
    }
    if (values.new_password !== values.confirm_password) {
      next.confirm_password = '两次输入的新密码不一致';
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function submit(event?: React.FormEvent) {
    event?.preventDefault();
    if (loading || !validate()) return;
    setLoading(true);
    try {
      await api.post('/api/auth/me/change-password', {
        old_password: values.old_password,
        new_password: values.new_password,
      });
      notify.success('密码已修改，请使用新密码重新登录');
      reset();
      onClose();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '修改密码失败');
    } finally {
      setLoading(false);
    }
  }

  function toggle(field: FieldKey) {
    setVisible((prev) => ({ ...prev, [field]: !prev[field] }));
  }

  function renderField(key: FieldKey, label: string, placeholder: string) {
    const isConfirm = key === 'confirm_password';
    const inputType = visible[key] ? 'text' : 'password';
    return (
      <div className="space-y-2">
        <Label htmlFor={key} className="text-[13px] text-[#18181a]">
          {label}
        </Label>
        <div className="relative">
          <Input
            id={key}
            name={key}
            type={inputType}
            value={values[key]}
            placeholder={placeholder}
            autoComplete={isConfirm ? 'new-password' : key === 'old_password' ? 'current-password' : 'new-password'}
            aria-invalid={errors[key] ? 'true' : 'false'}
            onChange={(event) => {
              setValues((prev) => ({ ...prev, [key]: event.target.value }));
              if (errors[key]) {
                setErrors((prev) => ({ ...prev, [key]: undefined }));
              }
            }}
            className="h-[40px] rounded-[12px] border-[#e4e8ef] bg-white pr-10 text-[14px] text-[#18181a] placeholder:text-[#b0b8c8]"
          />
          <button
            type="button"
            tabIndex={-1}
            onClick={() => toggle(key)}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-[#a0a8bd] outline-none transition-colors hover:text-[#6b7280]"
            aria-label={visible[key] ? '隐藏密码' : '显示密码'}
          >
            {visible[key] ? <EyeOff className="size-[16px]" /> : <Eye className="size-[16px]" />}
          </button>
        </div>
        {errors[key] && <p className="text-[12px] text-[#d20b0b]">{errors[key]}</p>}
      </div>
    );
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        aria-describedby="change-password-description"
        className="w-[calc(100%-2rem)] max-w-[420px] rounded-[18px] border-0 bg-white p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[24px] py-[20px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[36px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Lock className="size-[16px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">修改密码</DialogTitle>
              <DialogDescription id="change-password-description" className="mt-[4px] text-[12px] text-[#757f9c]">
                修改当前账号的登录密码，修改后需使用新密码登录。
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <form onSubmit={submit} className="space-y-[18px] px-[24px] py-[22px]">
          {renderField('old_password', '当前密码', '请输入当前密码')}
          {renderField('new_password', '新密码', '至少 6 位字符')}
          {renderField('confirm_password', '确认新密码', '再次输入新密码')}

          <div className="flex justify-end gap-[10px] pt-[6px]">
            <Button
              type="button"
              variant="outline"
              disabled={loading}
              onClick={() => handleOpenChange(false)}
              className="h-[36px] rounded-[10px] border-[#e2e6ed] px-[16px] text-[13px] text-[#5e687c] hover:bg-[#f4f6f9]"
            >
              取消
            </Button>
            <Button
              type="submit"
              disabled={loading}
              className="h-[36px] rounded-[10px] bg-[#18181a] px-[16px] text-[13px] text-white hover:bg-[#2d2d33]"
            >
              {loading ? (
                <span className="mr-2 inline-block size-[14px] animate-spin rounded-full border-2 border-white/40 border-t-white" />
              ) : null}
              保存
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
