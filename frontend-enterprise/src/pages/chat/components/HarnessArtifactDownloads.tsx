import { useEffect, useState } from 'react';

import StaffdeckIcon from '@/components/StaffdeckIcon';
import { notify } from '@/components/ui/app-toast';
import { api, type DownloadProgress } from '@/api/client';
import type { HarnessWorkspaceArtifact } from '@/types';

import {
  CHAT_ARTIFACTS_CLASS,
  CHAT_ARTIFACT_BUTTON_CLASS,
  CHAT_ARTIFACT_COPY_CLASS,
  CHAT_ARTIFACT_HEADING_CLASS,
  CHAT_ARTIFACT_ICON_CLASS,
  CHAT_ARTIFACT_IMAGE_CARD_CLASS,
  CHAT_ARTIFACT_IMAGE_CLASS,
  CHAT_ARTIFACT_IMAGE_DOWNLOAD_CLASS,
  CHAT_ARTIFACT_IMAGE_FOOTER_CLASS,
  CHAT_ARTIFACT_IMAGE_LINK_CLASS,
  CHAT_ARTIFACT_IMAGE_PLACEHOLDER_CLASS,
  CHAT_ARTIFACT_LIST_CLASS,
  CHAT_ARTIFACT_META_CLASS,
  CHAT_ARTIFACT_NAME_CLASS,
} from '../chatPageStyles';

type HarnessArtifactDownloadsProps = {
  artifacts: HarnessWorkspaceArtifact[];
  tenantId: string;
  sessionId: string;
};

export default function HarnessArtifactDownloads({
  artifacts,
  tenantId,
  sessionId,
}: HarnessArtifactDownloadsProps) {
  const [downloading, setDownloading] = useState('');
  // 打包下载：真实下载进度（XHR onprogress），percent=-1 时显示已接收体积
  const [zipProgress, setZipProgress] = useState<{ percent: number; received: number } | null>(null);

  if (artifacts.length === 0) return null;

  async function downloadTaskZip() {
    if (!sessionId || !tenantId || downloading) return;
    setDownloading('__task_zip__');
    setZipProgress({ percent: 0, received: 0 });
    try {
      const blob = await api.blobWithProgress(
        `/api/chat/sessions/${encodeURIComponent(sessionId)}/artifacts/`
          + `${encodeURIComponent(artifacts[0].task_frame_id)}/zip?tenant_id=${encodeURIComponent(tenantId)}`,
        ({ percent, receivedBytes }: DownloadProgress) => {
          setZipProgress({ percent, received: receivedBytes });
        },
      );
      const objectUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = `task-artifacts-${artifacts[0].task_frame_id}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(objectUrl);
      notify.success('已打包下载全部生成文件');
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '打包下载失败');
    } finally {
      setDownloading('');
      setZipProgress(null);
    }
  }

  async function downloadArtifact(artifact: HarnessWorkspaceArtifact) {
    const identity = `${artifact.task_frame_id}\u001f${artifact.path}`;
    const filename = artifactFilename(artifact.display_name || artifact.path);
    setDownloading(identity);
    try {
      const blob = await api.blob(artifactApiPath(artifact, tenantId, sessionId));
      const objectUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(objectUrl);
      notify.success(`已下载文件：${filename}`);
    } catch (error) {
      notify.error(error instanceof Error ? error.message : '文件下载失败');
    } finally {
      setDownloading('');
    }
  }

  return (
    <div className={CHAT_ARTIFACTS_CLASS} aria-label="生成文件">
      <div className={CHAT_ARTIFACT_HEADING_CLASS}>
        <StaffdeckIcon name="folder" size={14} />
        <span>生成文件</span>
        {artifacts.length > 1 && (
          <button
            type="button"
            className="ml-auto inline-flex cursor-pointer items-center gap-[4px] rounded-[8px] border-[0.5px] border-[#e3e7f1] bg-white px-[10px] py-[4px] text-[11px] text-[#464C5E] transition-colors hover:border-[#c9d2e3] hover:bg-[#f6f7fa] disabled:cursor-not-allowed disabled:opacity-60"
            disabled={Boolean(downloading) || !sessionId || !tenantId}
            aria-label="打包下载全部生成文件"
            onClick={() => void downloadTaskZip()}
          >
            <StaffdeckIcon name="download" size={12} />
            打包下载 ({artifacts.length})
          </button>
        )}
      </div>
      {downloading === '__task_zip__' && zipProgress && (
        <div aria-live="polite" className="mb-[8px]">
          <div
            role="progressbar"
            aria-label="打包下载进度"
            aria-valuenow={zipProgress.percent > 0 ? zipProgress.percent : undefined}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-[5px] w-full overflow-hidden rounded-full bg-[#eef1f7]"
          >
            <div
              className={zipProgress.percent < 0 ? 'h-full w-1/3 animate-pulse rounded-full bg-[#18181a]' : 'h-full rounded-full bg-[#18181a] transition-[width] duration-150'}
              style={zipProgress.percent >= 0 ? { width: `${zipProgress.percent}%` } : undefined}
            />
          </div>
          <p className="mt-[4px] text-[11px] text-[#858b9c]">
            {zipProgress.percent >= 0
              ? `${zipProgress.percent}%`
              : `已接收 ${(zipProgress.received / 1_048_576).toFixed(1)} MB`}
          </p>
        </div>
      )}
      <div className={CHAT_ARTIFACT_LIST_CLASS}>
        {artifacts.map((artifact) => {
          const identity = `${artifact.task_frame_id}\u001f${artifact.path}`;
          const filename = artifactFilename(artifact.display_name || artifact.path);
          const isDownloading = downloading === identity;
          if (isImageArtifact(artifact)) {
            return (
              <ArtifactImagePreview
                artifact={artifact}
                identity={identity}
                filename={filename}
                isDownloading={isDownloading}
                key={identity}
                tenantId={tenantId}
                sessionId={sessionId}
                onDownload={() => void downloadArtifact(artifact)}
              />
            );
          }
          return (
            <button
              type="button"
              className={CHAT_ARTIFACT_BUTTON_CLASS}
              key={identity}
              disabled={isDownloading || !sessionId || !tenantId}
              aria-label={`下载文件 ${filename}`}
              aria-busy={isDownloading}
              onClick={() => void downloadArtifact(artifact)}
            >
              <span className={CHAT_ARTIFACT_ICON_CLASS}>
                <StaffdeckIcon name="file" size={17} />
              </span>
              <span className={CHAT_ARTIFACT_COPY_CLASS}>
                <span className={CHAT_ARTIFACT_NAME_CLASS} data-i18n-ignore>
                  {filename}
                </span>
                <span className={CHAT_ARTIFACT_META_CLASS}>
                  {isDownloading ? '下载中' : artifactMeta(artifact)}
                </span>
              </span>
              <StaffdeckIcon name="download" size={16} />
            </button>
          );
        })}
      </div>
    </div>
  );
}

type ArtifactImagePreviewProps = {
  artifact: HarnessWorkspaceArtifact;
  identity: string;
  filename: string;
  isDownloading: boolean;
  tenantId: string;
  sessionId: string;
  onDownload: () => void;
};

function ArtifactImagePreview({
  artifact,
  identity,
  filename,
  isDownloading,
  tenantId,
  sessionId,
  onDownload,
}: ArtifactImagePreviewProps) {
  const [previewUrl, setPreviewUrl] = useState('');
  const [previewFailed, setPreviewFailed] = useState(false);

  useEffect(() => {
    if (!tenantId || !sessionId) return undefined;
    let disposed = false;
    let objectUrl = '';
    setPreviewUrl('');
    setPreviewFailed(false);

    void api.blob(artifactApiPath(artifact, tenantId, sessionId))
      .then((blob) => {
        if (disposed) return;
        objectUrl = window.URL.createObjectURL(blob);
        setPreviewUrl(objectUrl);
      })
      .catch(() => {
        if (!disposed) setPreviewFailed(true);
      });

    return () => {
      disposed = true;
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [artifact.path, artifact.task_frame_id, identity, sessionId, tenantId]);

  return (
    <figure className={CHAT_ARTIFACT_IMAGE_CARD_CLASS}>
      {previewUrl ? (
        <a
          className={CHAT_ARTIFACT_IMAGE_LINK_CLASS}
          href={previewUrl}
          target="_blank"
          rel="noreferrer"
          aria-label={`查看图片 ${filename}`}
        >
          <img
            className={CHAT_ARTIFACT_IMAGE_CLASS}
            src={previewUrl}
            alt={filename}
            loading="lazy"
            decoding="async"
          />
        </a>
      ) : (
        <div className={CHAT_ARTIFACT_IMAGE_PLACEHOLDER_CLASS} aria-live="polite">
          {previewFailed ? '图片预览不可用，可下载查看' : '正在加载图片…'}
        </div>
      )}
      <figcaption className={CHAT_ARTIFACT_IMAGE_FOOTER_CLASS}>
        <span className={CHAT_ARTIFACT_COPY_CLASS}>
          <span className={CHAT_ARTIFACT_NAME_CLASS} data-i18n-ignore>{filename}</span>
          <span className={CHAT_ARTIFACT_META_CLASS}>{artifactMeta(artifact)}</span>
        </span>
        <button
          type="button"
          className={CHAT_ARTIFACT_IMAGE_DOWNLOAD_CLASS}
          disabled={isDownloading || !sessionId || !tenantId}
          aria-label={`下载图片 ${filename}`}
          aria-busy={isDownloading}
          onClick={onDownload}
        >
          <StaffdeckIcon name="download" size={16} />
        </button>
      </figcaption>
    </figure>
  );
}

function artifactApiPath(
  artifact: HarnessWorkspaceArtifact,
  tenantId: string,
  sessionId: string,
): string {
  const query = new URLSearchParams({
    tenant_id: tenantId,
    path: artifact.path,
  });
  return `/api/chat/sessions/${encodeURIComponent(sessionId)}/artifacts/`
    + `${encodeURIComponent(artifact.task_frame_id)}?${query.toString()}`;
}

function artifactFilename(path: string): string {
  const filename = path.replace(/\\/g, '/').split('/').pop()?.trim() || '';
  return filename.replace(/[\u0000-\u001f\u007f]/g, '').slice(0, 180) || 'artifact';
}

function isImageArtifact(artifact: HarnessWorkspaceArtifact): boolean {
  const contentType = artifact.content_type?.toLowerCase().split(';')[0].trim();
  if (contentType?.startsWith('image/')) return true;
  return /\.(?:apng|avif|bmp|gif|jpe?g|png|svg|webp)$/i.test(
    artifact.display_name || artifact.path,
  );
}

function artifactMeta(artifact: HarnessWorkspaceArtifact): string {
  const size = formatArtifactSize(artifact.size);
  const description = artifact.description?.trim();
  if (description && size) return `${description} · ${size}`;
  if (description) return description;
  return size ? `生成文件 · ${size}` : '生成文件';
}

function formatArtifactSize(size: number | null | undefined): string {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}
