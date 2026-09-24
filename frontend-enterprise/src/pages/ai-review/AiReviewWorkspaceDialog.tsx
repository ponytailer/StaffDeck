import { useEffect, useState } from 'react';
import { GitBranch, LoaderCircle } from 'lucide-react';

import { createAiReviewWorkspace, type AiReviewPlatform } from '../../api/aiReview';
import { ApiError } from '../../api/client';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, Input, notify } from '@/components/ui';
import { cn } from '@/lib/utils';

const PLATFORMS: { id: AiReviewPlatform; label: string; hint: string }[] = [
  { id: 'github', label: 'GitHub', hint: 'https://github.com/{owner}/{repo}' },
  { id: 'gitlab', label: 'GitLab', hint: '自托管实例也支持，填实例仓库地址即可' },
];

export default function AiReviewWorkspaceDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [platform, setPlatform] = useState<AiReviewPlatform>('github');
  const [repoUrl, setRepoUrl] = useState('');
  const [defaultBranch, setDefaultBranch] = useState('main');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    setName('');
    setPlatform('github');
    setRepoUrl('');
    setDefaultBranch('main');
    setSubmitting(false);
  }, [open]);

  function validate(): string | null {
    if (!name.trim()) return '请填写 workspace 名称';
    if (!repoUrl.trim()) return '请填写仓库克隆地址';
    const url = repoUrl.trim();
    if (platform === 'github' && !/^https:\/\/github\.com\/[^/]+\/[^/]+/.test(url)) {
      return 'GitHub 仓库地址应为 https://github.com/{owner}/{repo}';
    }
    if (platform === 'gitlab' && !/^https?:\/\/.+/.test(url)) {
      return 'GitLab 仓库地址应为 http(s)://{host}/{group}/{repo}（.git 后缀可省）';
    }
    return null;
  }

  async function submit() {
    const invalid = validate();
    if (invalid) {
      notify.error(invalid);
      return;
    }
    setSubmitting(true);
    try {
      await createAiReviewWorkspace({
        name: name.trim(),
        platform,
        repo_url: repoUrl.trim(),
        default_branch: defaultBranch.trim() || 'main',
      });
      notify.success(`已创建 workspace「${name.trim()}」`);
      onCreated();
      onClose();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '创建失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby="ai-review-workspace-description"
        className="w-[calc(100%-2rem)] max-w-[460px] gap-0 rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[22px] py-[18px]">
          <div className="flex items-center gap-[10px]">
            <span className="grid size-[34px] place-items-center rounded-[11px] bg-[#18181a] text-white">
              <GitBranch className="size-[16px]" />
            </span>
            <div>
              <DialogTitle className="text-[15px] font-semibold text-[#18181a]">新建 Workspace</DialogTitle>
              <DialogDescription id="ai-review-workspace-description" className="mt-[4px] text-[12px] text-[#757f9c]">
                一个 workspace 对应一个待评审仓库
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="flex flex-col gap-[12px] px-[22px] py-[18px]">
          <div className="flex flex-col gap-[6px]">
            <label className="text-[12px] font-medium text-[#464c5e]" htmlFor="ai-review-ws-name">名称</label>
            <Input
              id="ai-review-ws-name"
              value={name}
              placeholder="例如：open-code-review 主仓库"
              onChange={(event) => setName(event.target.value)}
              className="h-[34px] text-[13px]"
            />
          </div>

          <div className="flex flex-col gap-[6px]">
            <label className="text-[12px] font-medium text-[#464c5e]">代码平台</label>
            <div className="inline-flex w-fit gap-[2px] rounded-[10px] border-[0.5px] border-[#e3e7f1] bg-white p-[2px]">
              {PLATFORMS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setPlatform(item.id)}
                  className={cn(
                    'rounded-[8px] px-[12px] py-[4px] text-[12px] transition-colors',
                    platform === item.id
                      ? 'bg-[#18181a] text-white'
                      : 'text-[#5b6273] hover:bg-[#f6f6f6] hover:text-[#18181a]',
                  )}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <span className="text-[11px] text-[#a3aaba]">{PLATFORMS.find((item) => item.id === platform)?.hint}</span>
          </div>

          <div className="flex flex-col gap-[6px]">
            <label className="text-[12px] font-medium text-[#464c5e]">仓库克隆地址</label>
            <Input
              value={repoUrl}
              placeholder={platform === 'github' ? 'https://github.com/alibaba/open-code-review.git' : 'https://gitlab.example.com/group/repo.git'}
              onChange={(event) => setRepoUrl(event.target.value)}
              className="h-[34px] font-mono text-[12px]"
            />
          </div>

          <div className="flex flex-col gap-[6px]">
            <label className="text-[12px] font-medium text-[#464c5e]">默认分支</label>
            <Input
              value={defaultBranch}
              placeholder="main"
              onChange={(event) => setDefaultBranch(event.target.value)}
              className="h-[34px] font-mono text-[12px]"
            />
          </div>

          <div className="mt-[4px] flex items-center justify-end gap-[8px] border-t-[0.5px] border-[#eef0f4] pt-[14px]">
            <Button
              type="button"
              variant="ghost"
              onClick={onClose}
              className="h-[32px] rounded-[9px] px-[14px] text-[12px] text-[#5b6273] hover:bg-[#f6f6f6]"
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={submitting}
              onClick={() => void submit()}
              className="h-[32px] gap-[6px] rounded-[9px] bg-[#18181a] px-[16px] text-[12px] text-white hover:bg-[#2b2b2e]"
            >
              {submitting && <LoaderCircle className="size-[13px] animate-spin" />}
              创建
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
