import { api, TENANT_ID, type DownloadProgress } from '../api/client';

/**
 * 下载通用技能包（zip）。
 *
 * `agentId` 省略或传 null 时按「开放广场」作用域请求；传了则按该员工的绑定
 * 可见性校验。技能列表页与开放广场共用同一份实现，避免两处各写一遍 blob 下载。
 *
 * 大包（几十 MB 级）下载可能持续几十秒：传 `onProgress` 时走 XHR 下载进度
 * （0-99 循环 + 100 完成），调用方可展示进度条；不传保持旧的 fetch blob 行为。
 */
export async function downloadGeneralSkillPackage({
  slug,
  agentId,
  onProgress,
}: {
  slug: string;
  agentId?: string | null;
  onProgress?: (progress: DownloadProgress) => void;
}): Promise<void> {
  const agentSuffix = agentId ? `&agent_id=${encodeURIComponent(agentId)}` : '';
  const path = `/api/enterprise/general-skills/${encodeURIComponent(slug)}/package?tenant_id=${TENANT_ID}${agentSuffix}`;
  const blob = onProgress
    ? await api.blobWithProgress(path, onProgress)
    : await api.blob(path);
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${slug}.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}
