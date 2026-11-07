import { useEffect, useMemo, useRef, useState } from 'react';
import { HardDrive, Link2, RefreshCw, Trash2, Upload } from 'lucide-react';

import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import { copyTextToClipboard } from '@/lib/clipboard';
import {
  MOBILE_CARD_CLASS,
  OUTLINE_ACTION_BUTTON_SM_CLASS,
  formatDateTime,
} from '@/lib/enterprise-ui';

import IconRefresh from '../assets/icons/refresh.svg?react';
import IconTrash from '../assets/icons/trash.svg?react';
import { API_BASE_URL, api, TENANT_ID } from '../api/client';
import { ConfirmDialog } from './ConfirmDialog';
import { DataTable, type DataTableColumn } from './DataTable';

export type StaticAssetRead = {
  id: string;
  filename: string;
  content_type: string;
  size: number;
  sha256: string;
  status: string;
  download_count: number;
  created_at: string;
  /** 相对下载路径，绝对地址由 `assetUrl` 拼。 */
  path: string;
};

type StaticAssetListPayload = {
  items: StaticAssetRead[];
  used_bytes: number;
  user_quota_bytes: number;
  max_file_bytes: number;
};

/**
 * 下载地址拼装。
 *
 * 后端只回相对路径 `/f/{token}/{filename}`，域名由前端补 —— 换域名、换端口后
 * 历史链接自动跟着变。后端若落库绝对 URL，等于把域名写死进数据里。
 */
function assetUrl(row: StaticAssetRead): string {
  const origin = API_BASE_URL || window.location.origin;
  return `${origin.replace(/\/+$/, '')}${row.path}`;
}

/**
 * 展示用的地址：把路径里的中文解码回来。
 *
 * 复制出去仍然用编码后的 `assetUrl`（任何客户端都能正确解析），但界面上不能直接
 * 摆一串 `%E6%A8%A1%E6%9D%BF.xlsx` —— 用户没法核对这是哪个文件。
 */
function displayUrl(row: StaticAssetRead): string {
  try {
    return decodeURIComponent(assetUrl(row));
  } catch {
    return assetUrl(row);
  }
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

/**
 * 「我的静态资源」。
 *
 * 和知识库的分工要在这里讲清楚，否则用户会问「我传到知识库了为什么还要传这」：
 * 知识库把文件拆成段落喂给模型**读**；这里是把原文件原样给用户**下**，
 * 正文不进检索，只提供一个稳定地址（可以粘进知识库，让员工回答时把链接回给用户）。
 *
 * 下载地址是公开的（免登录），所以必须在上传区显式提示不要放敏感文件 ——
 * 拿到链接的人都能下载，这是这个功能的固有代价，不是缺陷。
 */
export default function MyStaticAssetsPanel({
  searchTerm = '',
  embedded = false,
}: {
  searchTerm?: string;
  /** 作为独立 tab 内容渲染时置 true：去掉顶部间距，由外层容器决定留白。 */
  embedded?: boolean;
}) {
  const [rows, setRows] = useState<StaticAssetRead[]>([]);
  const [usage, setUsage] = useState({ used: 0, quota: 0, maxFile: 0 });
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<StaticAssetRead | null>(null);
  const [deleting, setDeleting] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function load() {
    setLoading(true);
    return api
      .get<StaticAssetListPayload>(
        `/api/enterprise/static-assets?tenant_id=${TENANT_ID}&mine=1`,
      )
      .then((payload) => {
        setRows(payload.items || []);
        setUsage({
          used: payload.used_bytes || 0,
          quota: payload.user_quota_bytes || 0,
          maxFile: payload.max_file_bytes || 0,
        });
      })
      .catch((error) => notify.error(error instanceof Error ? error.message : '加载静态资源失败'))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleFiles(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    if (usage.maxFile > 0 && file.size > usage.maxFile) {
      notify.error(`单个文件不能超过 ${formatBytes(usage.maxFile)}`);
      return;
    }
    setUploading(true);
    setProgress(0);
    try {
      const created = await api.uploadWithProgress<StaticAssetRead>(
        '/api/enterprise/static-assets',
        file,
        `tenant_id=${encodeURIComponent(TENANT_ID)}`,
        (percent) => setProgress(percent),
      );
      setRows((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      notify.success('上传完成，可复制链接使用');
      void load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '上传失败');
    } finally {
      setUploading(false);
      setProgress(0);
      if (inputRef.current) inputRef.current.value = '';
    }
  }

  async function copyLink(row: StaticAssetRead) {
    try {
      await copyTextToClipboard(assetUrl(row));
      notify.success('链接已复制');
    } catch {
      // copyTextToClipboard 失败时抛错（Clipboard API 被 webview 拒绝、非安全上下文等）
      notify.error('复制失败，请手动选中链接复制');
    }
  }

  async function confirmDelete() {
    const target = deleteTarget;
    if (!target) return;
    setDeleting(true);
    try {
      await api.delete<StaticAssetRead>(
        `/api/enterprise/static-assets/${encodeURIComponent(target.id)}?tenant_id=${TENANT_ID}`,
      );
      setRows((current) => current.filter((item) => item.id !== target.id));
      setDeleteTarget(null);
      notify.success('已删除，原链接将失效');
      void load();
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  }

  const filteredRows = useMemo(() => {
    const keyword = searchTerm.trim().toLowerCase();
    if (!keyword) return rows;
    return rows.filter((row) => row.filename.toLowerCase().includes(keyword));
  }, [rows, searchTerm]);

  const usedRatio = usage.quota > 0 ? Math.min(1, usage.used / usage.quota) : 0;

  const columns: DataTableColumn<StaticAssetRead>[] = [
    {
      key: 'filename',
      title: '文件',
      render: (row) => (
        <div className="flex min-w-0 flex-col gap-[2px]">
          <span className="truncate font-medium leading-[18px] text-[#18181a]" title={row.filename}>
            {row.filename}
          </span>
          <span className="truncate text-[#858b9c]" title={assetUrl(row)}>
            {displayUrl(row)}
          </span>
        </div>
      ),
    },
    {
      key: 'size',
      title: '大小',
      width: 90,
      render: (row) => formatBytes(row.size),
    },
    {
      key: 'downloads',
      title: '下载',
      width: 80,
      render: (row) => `${row.download_count} 次`,
    },
    {
      key: 'created',
      title: '上传时间',
      width: 170,
      render: (row) => formatDateTime(row.created_at),
    },
    {
      key: 'actions',
      title: '操作',
      width: 150,
      align: 'right',
      render: (row) => (
        <div className="flex items-center justify-end gap-[6px]">
          <UIButton
            variant="outline"
            className={OUTLINE_ACTION_BUTTON_SM_CLASS}
            onClick={() => void copyLink(row)}
          >
            <Link2 className="size-[13px]" />
            复制链接
          </UIButton>
          <button
            type="button"
            aria-label={`删除 ${row.filename}`}
            onClick={() => setDeleteTarget(row)}
            className="grid size-[26px] cursor-pointer place-items-center rounded-[8px] border-0 bg-transparent text-[#98a2b3] transition-colors hover:bg-[#fdecec] hover:text-[#d64545]"
          >
            <Trash2 className="size-[14px]" />
          </button>
        </div>
      ),
    },
  ];

  function renderMobileCard(row: StaticAssetRead) {
    return (
      <article className={MOBILE_CARD_CLASS} key={row.id}>
        <div className="flex min-w-0 items-start justify-between gap-[10px]">
          <div className="min-w-0">
            <strong className="block truncate text-[14px] font-semibold text-[#18181a]">{row.filename}</strong>
            <span className="mt-[2px] block truncate text-[12px] text-[#858b9c]">{displayUrl(row)}</span>
          </div>
          <button
            type="button"
            aria-label={`删除 ${row.filename}`}
            onClick={() => setDeleteTarget(row)}
            className="grid size-[26px] shrink-0 cursor-pointer place-items-center rounded-[8px] border-0 bg-transparent text-[#98a2b3]"
          >
            <IconTrash className="size-[14px]" />
          </button>
        </div>
        <div className="mt-[10px] flex items-center justify-between gap-[10px] text-[12px] text-[#858b9c]">
          <span>{formatBytes(row.size)} · 下载 {row.download_count} 次</span>
          <span>{formatDateTime(row.created_at)}</span>
        </div>
        <UIButton
          variant="outline"
          className={`mt-[10px] ${OUTLINE_ACTION_BUTTON_SM_CLASS}`}
          onClick={() => void copyLink(row)}
        >
          <Link2 className="size-[13px]" />
          复制链接
        </UIButton>
      </article>
    );
  }

  return (
    <section aria-label="我的静态资源" className={embedded ? undefined : 'mt-[40px]'}>
      <div className="mb-[14px] flex flex-wrap items-baseline gap-[8px]">
        <h2 className="text-[16px] font-semibold leading-[22px] text-[#18181a]">我的静态资源</h2>
        <span className="text-[12px] text-[#858b9c]">{rows.length} 个</span>
        <button
          type="button"
          onClick={() => void load()}
          className="ml-auto inline-flex cursor-pointer items-center gap-[4px] border-0 bg-transparent p-0 text-[12px] text-[#757f9c] transition-colors hover:text-[#18181a]"
        >
          <IconRefresh className="size-[14px] shrink-0" />
          刷新
        </button>
      </div>

      <p className="mb-[12px] flex flex-wrap items-center gap-x-[10px] gap-y-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
        <span className="inline-flex items-center gap-[4px]">
          <HardDrive className="size-[12px]" />
          已用 {formatBytes(usage.used)} / {formatBytes(usage.quota)}
        </span>
        <span>单个文件最大 {formatBytes(usage.maxFile)}</span>
        {/* 公开下载是这个功能的固有代价，必须在入口处讲明白，而不是等出事。 */}
        <span className="text-[#d19a3d]">链接免登录即可访问，请勿上传敏感文件</span>
      </p>

      <div className="mb-[14px] flex flex-wrap items-center gap-[10px]">
        <input
          ref={inputRef}
          type="file"
          className="hidden"
          onChange={(event) => void handleFiles(event.target.files)}
        />
        <UIButton
          variant="outline"
          className={OUTLINE_ACTION_BUTTON_SM_CLASS}
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
        >
          <Upload className="size-[13px]" />
          {uploading ? `上传中 ${progress}%` : '上传文件'}
        </UIButton>
        <span className="text-[11px] text-[#a7adbb]">
          上传后得到一个固定地址，可粘进知识库，让员工回答时把链接回给用户
        </span>
      </div>

      {usage.quota > 0 && usedRatio >= 0.8 && (
        <p className="mb-[12px] rounded-[12px] bg-[#fdf6e8] px-[14px] py-[10px] text-[11px] leading-[17px] text-[#a9762a]">
          空间即将用满（{formatBytes(usage.used)} / {formatBytes(usage.quota)}），请先清理不再使用的文件。
        </p>
      )}

      {loading && !rows.length ? (
        <p className="rounded-[16px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[24px] text-center text-[12px] text-[#a7adbb]">
          加载中…
        </p>
      ) : filteredRows.length ? (
        <>
          <div className="hidden md:block">
            <DataTable
              columns={columns}
              data={filteredRows}
              rowKey={(row) => row.id}
              aria-label="我的静态资源列表"
            />
          </div>
          <div className="grid gap-[12px] md:hidden">
            {filteredRows.map((row) => renderMobileCard(row))}
          </div>
        </>
      ) : (
        <div className="flex flex-col items-center rounded-[20px] border border-dashed border-[#e4e9f2] bg-[#fbfcfe] px-[24px] py-[26px] text-center">
          <span className="grid size-[34px] place-items-center rounded-[12px] bg-white text-[#98a2b3] shadow-[0_1px_8px_rgba(70,76,94,0.06)] ring-1 ring-[#edf1f6]">
            <HardDrive className="size-[16px] shrink-0" />
          </span>
          <p className="mt-[12px] text-[14px] font-medium leading-[20px] text-[#7f879a]">
            {searchTerm.trim() ? '没有匹配的文件' : '还没有上传过文件'}
          </p>
          {!searchTerm.trim() && (
            <p className="mt-[4px] text-[11px] leading-[17px] text-[#a7adbb]">
              上传说明书、模板、附件等静态文件，得到一个可以直接发给用户的下载地址
            </p>
          )}
        </div>
      )}

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
        loading={deleting}
        title={`删除「${deleteTarget ? deleteTarget.filename : ''}」？`}
        description="删除后原下载链接立即失效（访问会返回 410），已经发出或粘贴到知识库的链接也会失效。"
        confirmText="删除"
        onConfirm={() => void confirmDelete()}
      />
    </section>
  );
}
