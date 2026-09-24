import { useCallback, useEffect, useState } from 'react';
import { Bot, FileCode2, LoaderCircle, MessageSquare, RotateCcw, SendToBack } from 'lucide-react';

import {
  fetchAiReviewTaskDetail,
  syncAiReviewTaskToPlatform,
  type AiReviewTaskDetail,
} from '../../api/aiReview';
import { ApiError } from '../../api/client';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { notify } from '@/components/ui/app-toast';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui';
import { cn } from '@/lib/utils';
import { commentRange, formatElapsed, formatTokens, sortComments } from './aiReviewModel';

function SummaryChip({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <span className="inline-flex items-center gap-[5px] rounded-[8px] bg-[#f4f5f8] px-[10px] py-[4px] text-[11.5px] text-[#464c5e]">
      <span className="text-[#a3aaba]">{label}</span>
      <span className="font-medium tabular-nums text-[#18181a]">{value}</span>
    </span>
  );
}

function CommentCard({ comment }: { comment: AiReviewTaskDetail['result_json'][number] }) {
  const path = comment.path || '(未知文件)';
  const range = commentRange(comment);
  return (
    <div className="flex flex-col gap-[8px] rounded-[14px] border-[0.5px] border-[#eef0f4] bg-white px-[14px] py-[12px]">
      <div className="flex flex-wrap items-center gap-[8px]">
        <FileCode2 className="size-[13px] shrink-0 text-[#5b6273]" />
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] font-medium text-[#18181a]" title={path}>
          {path}
        </span>
        <span className="shrink-0 rounded-[6px] bg-[#eef2ff] px-[7px] py-[2px] font-mono text-[10.5px] font-medium tabular-nums text-[#4f46e5]">
          {range}
        </span>
      </div>
      {comment.content?.trim() && (
        <p className="text-[12.5px] leading-[19px] text-[#2b3242]">{comment.content}</p>
      )}
      {comment.existing_code?.trim() && (
        <div>
          <div className="mb-[3px] text-[10.5px] font-medium uppercase tracking-wide text-[#c0392b]">现有代码</div>
          <pre className="max-h-[140px] overflow-auto rounded-[10px] bg-[#fdf1f0] p-[10px] font-mono text-[11px] leading-[17px] text-[#8f2c20]">
            {comment.existing_code}
          </pre>
        </div>
      )}
      {comment.suggestion_code?.trim() && (
        <div>
          <div className="mb-[3px] text-[10.5px] font-medium uppercase tracking-wide text-[#1a7f4b]">建议修改</div>
          <pre className="max-h-[140px] overflow-auto rounded-[10px] bg-[#eef8f3] p-[10px] font-mono text-[11px] leading-[17px] text-[#1c6b45]">
            {comment.suggestion_code}
          </pre>
        </div>
      )}
      {comment.thinking?.trim() && (
        <details className="group">
          <summary className="cursor-pointer select-none text-[11px] text-[#a3aaba] transition-colors hover:text-[#757f9c]">
            评审思路
          </summary>
          <p className="mt-[6px] rounded-[10px] bg-[#fafbfd] p-[10px] text-[11.5px] leading-[18px] text-[#757f9c]">
            {comment.thinking}
          </p>
        </details>
      )}
    </div>
  );
}

function formatSyncTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value.endsWith('Z') || value.includes('+') ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export default function ReviewResultDialog({
  taskId,
  onClose,
}: {
  taskId: string | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<AiReviewTaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [confirmSync, setConfirmSync] = useState(false);
  const [syncing, setSyncing] = useState(false);

  const reload = useCallback(async () => {
    if (!taskId) return;
    setError(null);
    setLoading(true);
    try {
      setDetail(await fetchAiReviewTaskDetail(taskId));
    } catch (cause) {
      setDetail(null);
      setError(cause instanceof Error ? cause.message : '加载评审结果失败');
    } finally {
      setLoading(false);
    }
  }, [taskId]);

  useEffect(() => {
    if (!taskId) {
      setDetail(null);
      setError(null);
      return;
    }
    void reload();
  }, [taskId, reload]);

  async function doSync() {
    if (!taskId) return;
    setSyncing(true);
    try {
      const result = await syncAiReviewTaskToPlatform(taskId);
      notify.success('评审结果已回写到 PR / MR 评论区');
      if (result.platform_sync_url) {
        window.open(result.platform_sync_url, '_blank', 'noopener');
      }
      setConfirmSync(false);
      await reload();
    } catch (cause) {
      notify.error(cause instanceof ApiError || cause instanceof Error ? cause.message : '回写失败');
    } finally {
      setSyncing(false);
    }
  }

  const comments = detail ? sortComments(detail.result_json) : [];
  const summary = detail?.summary_json ?? {};
  const canSync = Boolean(detail && comments.length > 0);

  return (
    <Dialog open={taskId !== null} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby="ai-review-result-description"
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[760px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[24px] py-[20px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Bot className="size-[17px]" />
            </span>
            <div className="min-w-0 flex-1">
              <DialogTitle className="truncate text-[16px] font-semibold text-[#18181a]">
                {detail ? `评审结果 · #${detail.mr_number} ${detail.mr_title}` : '评审结果'}
              </DialogTitle>
              <DialogDescription id="ai-review-result-description" className="mt-[5px] truncate text-[12px] text-[#757f9c]">
                {detail ? `${detail.source_branch} → ${detail.target_branch}` : '任务结果与逐行评审意见'}
              </DialogDescription>
            </div>
            {canSync && (
              <div className="shrink-0">
                <button
                  type="button"
                  onClick={() => setConfirmSync(true)}
                  disabled={syncing}
                  className="inline-flex h-[32px] items-center gap-[6px] rounded-[9px] border-[0.5px] border-[#cfe0ff] bg-[#f4f8ff] px-[12px] text-[12px] font-medium text-[#1a71ff] transition-colors hover:bg-[#e9f1ff] disabled:opacity-50"
                >
                  <SendToBack className="size-[13px]" />
                  回写到 PR / MR
                </button>
              </div>
            )}
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto px-[24px] py-[20px]">
          {loading ? (
            <div className="flex items-center justify-center gap-[8px] py-[60px] text-[12px] text-[#a3aaba]">
              <LoaderCircle className="size-[14px] animate-spin" />
              正在加载评审结果…
            </div>
          ) : error ? (
            <div className="flex flex-col items-center gap-[10px] py-[60px] text-center">
              <p className="text-[12.5px] text-[#c0392b]">{error}</p>
              <button
                type="button"
                onClick={() => void reload()}
                className="inline-flex items-center gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[12px] py-[4px] text-[12px] text-[#464c5e] hover:bg-[#f6f6f6]"
              >
                <RotateCcw className="size-[12px]" />
                重试
              </button>
            </div>
          ) : detail ? (
            <div className="flex flex-col gap-[14px]">
              <div className="flex flex-wrap items-center gap-[8px]">
                <SummaryChip label="状态" value={String(summary.ocr_status || '')} />
                <SummaryChip label="模型" value={String(summary.model || '')} />
                <SummaryChip label="评审文件" value={summary.files_reviewed !== undefined ? String(summary.files_reviewed) : ''} />
                <SummaryChip label="评论数" value={String(comments.length)} />
                <SummaryChip label="Tokens" value={summary.total_tokens !== undefined ? formatTokens(Number(summary.total_tokens)) : ''} />
                <SummaryChip label="耗时" value={formatElapsed(summary.elapsed as number | string | undefined)} />
                {detail.platform_synced_at && (
                  <a
                    href={detail.platform_sync_url || detail.web_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-[5px] rounded-[8px] bg-[#e9f7ef] px-[10px] py-[4px] text-[11.5px] text-[#1a7f4b] transition-colors hover:bg-[#ddf2e5]"
                  >
                    <SendToBack className="size-[11px]" />
                    已回写 {formatSyncTime(detail.platform_synced_at)} · 查看评论
                  </a>
                )}
              </div>

              {detail.requirements?.trim() && (
                <div className="rounded-[12px] bg-[#fafbfd] px-[14px] py-[10px]">
                  <div className="text-[11px] font-medium text-[#757f9c]">本次评审要求（快照）</div>
                  <p className="mt-[4px] whitespace-pre-wrap text-[11.5px] leading-[18px] text-[#5b6273]">
                    {detail.requirements}
                  </p>
                </div>
              )}

              <div className="flex items-center gap-[6px] text-[13px] font-semibold text-[#18181a]">
                <MessageSquare className="size-[14px] text-[#5b6273]" />
                评审意见
                <span className="text-[11.5px] font-normal text-[#a3aaba]">共 {comments.length} 条</span>
              </div>

              {comments.length === 0 ? (
                <div className="rounded-[12px] border-[0.5px] border-dashed border-[#e3e7f1] bg-[#fafbfd] px-[14px] py-[18px] text-[12px] leading-[18px] text-[#757f9c]">
                  这次评审没有产出行级意见——可能变更很小，或模型认为没有值得指摘的点。
                </div>
              ) : (
                <div className="flex flex-col gap-[8px]">
                  {comments.map((comment, index) => (
                    <CommentCard key={`${comment.path}-${comment.start_line}-${index}`} comment={comment} />
                  ))}
                </div>
              )}
            </div>
          ) : null}
        </div>

        <ConfirmDialog
          open={confirmSync}
          onOpenChange={(next) => setConfirmSync(next)}
          title={
            detail ? (
              <>
                将评审结果回写到 <strong>#{detail.mr_number} {detail.mr_title}</strong> 评论区？
              </>
            ) : (
              '将评审结果回写到 PR / MR 评论区？'
            )
          }
          description="会在平台上新增一条汇总评论（含每条意见的文件、行号与建议代码），不影响现有评论；同一任务可重复回写。"
          confirmText="回写"
          cancelText="取消"
          destructive={false}
          loading={syncing}
          onConfirm={() => void doSync()}
        />
      </DialogContent>
    </Dialog>
  );
}
