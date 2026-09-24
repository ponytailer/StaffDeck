import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, GitPullRequest, LoaderCircle, RotateCcw, Star } from 'lucide-react';

import {
  createAiReviewTask,
  fetchAiReviewMergeRequests,
  fetchAiReviewPresets,
  type AiReviewMergeRequest,
  type AiReviewPreset,
  type AiReviewWorkspace,
} from '../../api/aiReview';
import { ApiError } from '../../api/client';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, Input, notify, Textarea } from '@/components/ui';
import { cn } from '@/lib/utils';
import { validateTaskDraft } from './aiReviewModel';

/**
 * 发起评审任务弹窗：
 * - 默认带出从 MR 列表点进来的那条 PR/MR；「手动指定」时先在弹窗内挑一条
 * - 评审要求 = 自由文本 + 勾选的全局预设（标星预设默认勾上）
 * - 提交只是入队（rq），成功后任务出现在列表里等待轮询
 */
export default function CreateReviewTaskDialog({
  open,
  onClose,
  workspace,
  initialMr,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  workspace: AiReviewWorkspace | null;
  initialMr: AiReviewMergeRequest | null;
  onCreated: () => void;
}) {
  const [mr, setMr] = useState<AiReviewMergeRequest | null>(null);
  const [mrs, setMrs] = useState<AiReviewMergeRequest[] | null>(null);
  const [mrLoading, setMrLoading] = useState(false);
  const [mrError, setMrError] = useState<string | null>(null);

  const [presets, setPresets] = useState<AiReviewPreset[]>([]);
  const [selectedPresetIds, setSelectedPresetIds] = useState<string[]>([]);
  const [requirements, setRequirements] = useState('');
  const [manualNumber, setManualNumber] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const needPick = !initialMr || !initialMr.number;

  const reloadMr = useCallback(async () => {
    if (!workspace) return;
    setMrError(null);
    setMrLoading(true);
    try {
      // 弹窗里只做选择器：取首页 50 条即可
      const result = await fetchAiReviewMergeRequests(workspace.id, {
        page: 1,
        pageSize: 50,
        search: '',
      });
      setMrs(result.items);
    } catch (cause) {
      setMrError(cause instanceof ApiError || cause instanceof Error ? cause.message : '拉取 PR/MR 失败');
      setMrs([]);
    } finally {
      setMrLoading(false);
    }
  }, [workspace]);

  const reloadPresets = useCallback(async () => {
    try {
      const rows = await fetchAiReviewPresets();
      setPresets(rows);
      const defaultIds = rows.filter((row) => row.is_default).map((row) => row.id);
      setSelectedPresetIds(defaultIds);
    } catch {
      setPresets([]);
      setSelectedPresetIds([]);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setMr(initialMr && initialMr.number ? initialMr : null);
    setManualNumber('');
    setRequirements('');
    setSubmitting(false);
    void reloadMr();
    void reloadPresets();
  }, [open, initialMr, reloadMr, reloadPresets]);

  function pickManual() {
    const number = Number.parseInt(manualNumber.trim(), 10);
    if (!Number.isFinite(number) || number <= 0) {
      notify.error('请填写有效的 PR / MR 编号');
      return;
    }
    setMr({
      number,
      title: '',
      description: '',
      source_branch: '',
      target_branch: workspace?.default_branch ?? '',
      author: '',
      web_url: '',
      updated_at: '',
    });
  }

  function togglePreset(presetId: string) {
    setSelectedPresetIds((prev) =>
      prev.includes(presetId) ? prev.filter((id) => id !== presetId) : [...prev, presetId],
    );
  }

  const invalid = useMemo(
    () => validateTaskDraft(requirements, selectedPresetIds),
    [requirements, selectedPresetIds],
  );

  async function submit() {
    if (!workspace || !mr) return;
    if (invalid) {
      notify.error(invalid);
      return;
    }
    setSubmitting(true);
    try {
      const created = await createAiReviewTask({
        workspace_id: workspace.id,
        mr_number: mr.number,
        requirements: requirements.trim(),
        preset_ids: selectedPresetIds,
      });
      if (created.scheduled) {
        notify.success(`评审任务已提交（PR/MR #${mr.number}），完成后可在任务列表查看结果`);
      } else {
        notify.warning('任务已创建，但调度通道暂不可用，稍后会自动重试入队');
      }
      onCreated();
      onClose();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '提交失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby="ai-review-create-description"
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[680px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[24px] py-[20px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <GitPullRequest className="size-[17px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">发起评审</DialogTitle>
              <DialogDescription id="ai-review-create-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                {workspace ? `workspace「${workspace.name}」 · ` : ''}
                任务异步执行，提交后可以继续做别的
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[14px] overflow-y-auto px-[24px] py-[20px]">
          {!mr ? (
            <section className="rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] py-[12px]">
              <div className="text-[13px] font-semibold text-[#18181a]">选择要评审的 PR / MR</div>
              <div className="mt-[10px] flex items-center gap-[8px]">
                <Input
                  value={manualNumber}
                  placeholder="手动指定编号，例如 42"
                  onChange={(event) => setManualNumber(event.target.value)}
                  className="h-[32px] w-[180px] font-mono text-[12px]"
                />
                <Button
                  type="button"
                  onClick={pickManual}
                  className="h-[32px] shrink-0 rounded-[8px] border-[0.5px] border-[#cfe0ff] bg-[#f4f8ff] px-[12px] text-[12px] text-[#1a71ff] hover:bg-[#e9f1ff]"
                >
                  指定编号
                </Button>
                <span className="min-w-0 flex-1 truncate text-[11px] text-[#a3aaba]">或从下方列表挑选</span>
              </div>
              {mrLoading ? (
                <div className="mt-[10px] flex items-center gap-[8px] py-[10px] text-[12px] text-[#a3aaba]">
                  <LoaderCircle className="size-[13px] animate-spin" />
                  正在拉取 open 状态的 PR / MR…
                </div>
              ) : mrError ? (
                <div className="mt-[10px] flex flex-col items-start gap-[6px] rounded-[10px] border-[0.5px] border-[#f3d28b] bg-[#fff8e8] px-[10px] py-[8px] text-[11.5px] text-[#6f4500]">
                  <span>{mrError}</span>
                  <button
                    type="button"
                    onClick={() => void reloadMr()}
                    className="inline-flex items-center gap-[4px] rounded-[7px] border-[0.5px] border-[#e3d0a6] bg-white px-[9px] py-[2px] text-[11px] hover:bg-[#fdf4df]"
                  >
                    <RotateCcw className="size-[11px]" />
                    重试
                  </button>
                </div>
              ) : (
                <div className="mt-[10px] flex max-h-[220px] flex-col gap-[6px] overflow-y-auto">
                  {(mrs ?? []).length === 0 ? (
                    <span className="py-[8px] text-[11.5px] text-[#757f9c]">没有 open 状态的 PR / MR，请手动指定编号。</span>
                  ) : (
                    (mrs ?? []).map((item) => (
                      <button
                        key={item.number}
                        type="button"
                        onClick={() => setMr(item)}
                        className="flex flex-col items-start gap-[2px] rounded-[10px] border-[0.5px] border-[#eef0f4] bg-[#fafbfd] px-[10px] py-[8px] text-left transition-colors hover:border-[#cfe0ff] hover:bg-[#f4f8ff]"
                      >
                        <span className="w-full truncate text-[12.5px] font-medium text-[#18181a]">
                          #{item.number} {item.title}
                        </span>
                        <span className="truncate font-mono text-[11px] text-[#757f9c]">
                          {item.source_branch} → {item.target_branch}
                          {item.author ? ` · @${item.author}` : ''}
                        </span>
                      </button>
                    ))
                  )}
                </div>
              )}
            </section>
          ) : (
            <section className="rounded-[14px] border-[0.5px] border-[#cfe0ff] bg-[#f4f8ff] px-[14px] py-[12px]">
              <div className="flex items-center justify-between gap-[10px]">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-[6px]">
                    <span className="shrink-0 text-[12px] font-medium tabular-nums text-[#5b6273]">#{mr.number}</span>
                    <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-[#18181a]">
                      {mr.title || '（标题将以平台返回为准）'}
                    </span>
                  </div>
                  {(mr.description?.trim() || mr.source_branch) && (
                    <div className="mt-[4px] space-y-[3px]">
                      {mr.description?.trim() && (
                        <p className="line-clamp-2 text-[11.5px] leading-[17px] text-[#464c5e]">{mr.description}</p>
                      )}
                      <p className="truncate font-mono text-[11px] text-[#757f9c]">
                        {mr.source_branch || '…'} → {mr.target_branch || '…'}
                        {mr.author ? ` · @${mr.author}` : ''}
                      </p>
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  aria-label="重新选择 PR / MR"
                  onClick={() => setMr(null)}
                  className="h-[26px] shrink-0 rounded-[7px] border-[0.5px] border-[#cfe0ff] bg-white px-[9px] text-[11px] text-[#5b6273] transition-colors hover:bg-[#e9f1ff]"
                >
                  重选
                </button>
              </div>
            </section>
          )}

          {presets.length > 0 && (
            <section className="rounded-[14px] border-[0.5px] border-[#e3e7f1] bg-white px-[14px] py-[12px]">
              <div className="text-[13px] font-semibold text-[#18181a]">叠加全局评审要求</div>
              <div className="mt-[8px] flex flex-col gap-[6px]">
                {presets.map((preset) => {
                  const checked = selectedPresetIds.includes(preset.id);
                  return (
                    <button
                      key={preset.id}
                      type="button"
                      onClick={() => togglePreset(preset.id)}
                      className={cn(
                        'flex items-start gap-[8px] rounded-[10px] border-[0.5px] px-[10px] py-[8px] text-left transition-colors',
                        checked ? 'border-[#cfe0ff] bg-[#f4f8ff]' : 'border-[#eef0f4] bg-[#fafbfd] hover:bg-[#f6f6f6]',
                      )}
                    >
                      <span
                        className={cn(
                          'mt-[1px] grid size-[16px] shrink-0 place-items-center rounded-[5px] transition-colors',
                          checked ? 'bg-[#1a71ff] text-white' : 'border-[0.5px] border-[#d8dbe3] bg-white text-transparent',
                        )}
                      >
                        <Check className="size-[10px]" strokeWidth={3} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="inline-flex items-center gap-[5px] text-[12.5px] font-medium text-[#18181a]">
                          {preset.name}
                          {preset.is_default && (
                            <span className="inline-flex items-center gap-[3px] rounded-[5px] bg-[#fff7e8] px-[5px] py-[1px] text-[10px] text-[#8a4b00]">
                              <Star className="size-[9px] fill-current" />
                              默认
                            </span>
                          )}
                        </span>
                        <span className="mt-[2px] block line-clamp-2 text-[11px] leading-[16px] text-[#757f9c]">
                          {preset.content}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </section>
          )}

          <section className="flex flex-col gap-[6px]">
            <label className="text-[12px] font-medium text-[#464c5e]">
              本次评审要求
              <span className="ml-[6px] font-normal text-[#a3aaba]">将与勾选的预设一起冻结进任务</span>
            </label>
            <Textarea
              rows={4}
              value={requirements}
              placeholder="例如：重点看登录模块的并发处理与错误分支；给出具体修改建议"
              onChange={(event) => setRequirements(event.target.value)}
              className="min-h-[88px] resize-y text-[12.5px]"
            />
            {invalid && <span className="text-[11px] text-[#c0392b]">{invalid}</span>}
          </section>
        </div>

        <div className="flex items-center justify-end gap-[8px] border-t-[0.5px] border-[#eef0f4] bg-white px-[24px] py-[14px]">
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
            disabled={submitting || !mr || Boolean(invalid)}
            onClick={() => void submit()}
            className="h-[32px] gap-[6px] rounded-[9px] bg-[#18181a] px-[16px] text-[12px] text-white hover:bg-[#2b2b2e] disabled:opacity-50"
          >
            {submitting && <LoaderCircle className="size-[13px] animate-spin" />}
            创建评审任务
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
