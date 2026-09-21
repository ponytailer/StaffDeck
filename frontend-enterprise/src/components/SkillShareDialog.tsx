import { useEffect, useState } from 'react';
import { Share2 } from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui';
import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { api, TENANT_ID } from '@/api/client';
import { copyTextToClipboard } from '@/lib/clipboard';
import {
  generalSkillShareUrlForToken,
  shareHostFromEnv,
} from '@/lib/share-host';
import type { GeneralSkillRead } from '@/types';

type CreatedShare = { token: string };

/**
 * 技能分享弹窗：生成免登录的公开链接（访客可看描述 + 下载技能包）。
 * 链接 host 复用数字员工分享的 VITE_SHARE_HOST 口径；同一技能的链接幂等，
 * 重复打开弹窗不会再生成新 token。
 */
export function SkillShareDialog({
  skill,
  open,
  onClose,
}: {
  skill: GeneralSkillRead | null;
  open: boolean;
  onClose: () => void;
}) {
  const [allowCreate, setAllowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<{ token: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const shareUrl = created
    ? generalSkillShareUrlForToken(
        created.token,
        shareHostFromEnv(import.meta.env, window.location.origin),
      )
    : '';

  useEffect(() => {
    if (!open || !skill) {
      setCreated(null);
      setCopied(false);
      setAllowCreate(false);
      return;
    }
    // 打开即创建（幂等接口会复用已有 token）
    setAllowCreate(true);
  }, [open, skill]);

  useEffect(() => {
    if (!allowCreate || !skill) return;
    setAllowCreate(false);
    setCreating(true);
    api
      .post<CreatedShare>(`/api/enterprise/general-skills/${encodeURIComponent(skill.slug)}/share?tenant_id=${TENANT_ID}`)
      .then((row) => {
        setCreated(row);
        notify.success('分享链接已生成');
      })
      .catch((error) => {
        notify.error(error instanceof Error ? error.message : '生成分享链接失败');
        onClose();
      })
      .finally(() => setCreating(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allowCreate, skill]);

  async function copyLink() {
    if (!shareUrl) return;
    try {
      await copyTextToClipboard(shareUrl);
      setCopied(true);
      notify.success('链接已复制');
    } catch {
      setCopied(false);
      notify.error('自动复制受浏览器限制，请手动选中链接复制');
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next && !creating) onClose(); }}>
      <DialogContent
        aria-describedby="skill-share-description"
        data-i18n-ignore
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[520px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[26px] py-[22px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Share2 className="size-[18px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">分享技能 · {skill?.name || ''}</DialogTitle>
              <DialogDescription id="skill-share-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                生成的链接无需登录即可查看技能描述并下载技能包；技能停用后链接自动失效。
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[14px] overflow-y-auto px-[26px] py-[22px]">
          {creating && <p className="text-[13px] text-[#757f9c]">正在生成分享链接…</p>}
          {shareUrl && (
            <>
              <div className="rounded-[12px] border border-[#e3e7f1] bg-white px-[14px] py-[12px]">
                <code className="block break-all text-[12px] leading-[20px] text-[#18181a] select-all">{shareUrl}</code>
              </div>
              <div className="flex items-center justify-end gap-[8px]">
                <UIButton
                  className="h-[34px] rounded-[10px] bg-[#18181a] px-[18px] text-[12px] text-white hover:bg-[#303030]"
                  onClick={() => void copyLink()}
                >
                  {copied ? '已复制' : '复制链接'}
                </UIButton>
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
