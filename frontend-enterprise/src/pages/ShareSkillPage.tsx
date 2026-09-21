// 分享技能公开页：免登录，访客凭 token 查看技能描述并下载技能包。
import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { Download, Share2 } from 'lucide-react';

import { Button as UIButton } from '@/components/ui/button';
import { notify } from '@/components/ui/app-toast';
import type { DownloadProgress } from '@/api/client';

type PublicSkillInfo = {
  token: string;
  slug: string;
  name: string;
  description?: string | null;
  homepage?: string | null;
  owner_name?: string | null;
  file_count: number;
  total_bytes: number;
  expires_at?: string | null;
};

function formatBytes(value: number): string {
  if (value >= 1_048_576) return `${(value / 1_048_576).toFixed(1)} MB`;
  if (value >= 1_024) return `${(value / 1_024).toFixed(0)} KB`;
  return `${value} B`;
}

export default function ShareSkillPage() {
  const { token = '' } = useParams();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [info, setInfo] = useState<PublicSkillInfo | null>(null);
  // 下载进度：percent 为 0-99；null = 无 Content-Length，只显示已接收体积
  const [downloadPercent, setDownloadPercent] = useState<number | null>(null);
  const [receivedBytes, setReceivedBytes] = useState(0);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/api/public/general-skill-shares/${encodeURIComponent(token)}`)
      .then(async (response) => {
        if (!response.ok) {
          const text = await response.text();
          throw new Error(text || `HTTP ${response.status}`);
        }
        return response.json() as Promise<PublicSkillInfo>;
      })
      .then((row) => { if (!cancelled) setInfo(row); })
      .catch((err) => { if (!cancelled) setError(err instanceof Error ? err.message : '加载失败'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [token]);

  async function download() {
    if (!info || downloading) return;
    setDownloading(true);
    setDownloadPercent(0);
    setReceivedBytes(0);
    try {
      const blob = await new Promise<Blob>((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('GET', `/api/public/general-skill-shares/${encodeURIComponent(token)}/package`);
        xhr.responseType = 'blob';
        xhr.onprogress = (event) => {
          const total = event.lengthComputable ? event.total : 0;
          setReceivedBytes(event.loaded);
          setDownloadPercent(total > 0 ? Math.min(99, Math.round((event.loaded / total) * 99)) : -1);
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve(xhr.response as Blob);
            return;
          }
          reject(new Error(`HTTP ${xhr.status}`));
        };
        xhr.onerror = () => reject(new Error('网络错误'));
        xhr.send();
      });
      // 收到响应 = 下载完成；交给浏览器保存
      setDownloadPercent(100);
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${info?.slug || 'skill'}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      notify.success('技能包已下载');
    } catch (err) {
      notify.error(err instanceof Error ? err.message : '技能包下载失败');
    } finally {
      setDownloading(false);
      setDownloadPercent(null);
    }
  }

  return (
    <div className="min-h-dvh bg-[#f6f7fa] px-[24px] py-[40px] max-[900px]:px-[16px]" data-i18n-ignore>
      <div className="mx-auto flex w-full max-w-[760px] flex-col gap-[20px]">
        <header className="flex items-center gap-[10px]">
          <span className="grid size-[34px] place-items-center rounded-[12px] bg-[#18181a] text-white">
            <Share2 className="size-[16px]" />
          </span>
          <p className="text-[14px] font-medium text-[#464C5E]">技能分享</p>
        </header>

        {loading && <p className="rounded-[16px] bg-white p-[24px] text-center text-[13px] text-[#757f9c]">加载中…</p>}

        {error && (
          <div className="rounded-[16px] bg-white px-[24px] py-[26px] text-center">
            <p className="text-[14px] font-medium text-[#18181a]">链接无法打开</p>
            <p className="mt-[6px] text-[12px] text-[#858b9c]">{error || '分享链接不存在或已失效'}</p>
          </div>
        )}

        {info && (
          <main className="flex min-h-[560px] flex-col rounded-[20px] bg-white p-[36px] shadow-[0_8px_24px_rgba(0,0,0,0.06)] max-[900px]:min-h-0 max-[900px]:p-[28px]">
            <h1 className="text-[20px] font-semibold leading-[28px] text-[#18181a]">{info.name}</h1>
            <p className="mt-[4px] text-[12px] text-[#858b9c]">
              {info.slug}
              {info.owner_name ? ` · 创建者 ${info.owner_name}` : ''}
              {info.total_bytes > 0 ? ` · ${formatBytes(info.total_bytes)}` : ''}
            </p>

            {info.description && (
              <p className="mt-[14px] whitespace-pre-wrap text-[13px] leading-[1.7] text-[#464C5E]">{info.description}</p>
            )}

            {info.homepage && (
              <a href={info.homepage} target="_blank" rel="noreferrer" className="mt-[8px] inline-block text-[12px] text-[#1a71ff] hover:underline">
                主页链接 ↗
              </a>
            )}

            <div className="flex-1" />

            {downloading && (
              <div aria-live="polite" className="mt-[12px]">
                <div
                  role="progressbar"
                  aria-label="技能包下载进度"
                  aria-valuenow={downloadPercent ?? undefined}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  className="h-[6px] w-full overflow-hidden rounded-full bg-[#eef1f7]"
                >
                  <div
                    className={downloadPercent === null ? 'h-full w-1/3 animate-pulse rounded-full bg-[#18181a]' : 'h-full rounded-full bg-[#18181a] transition-[width] duration-150'}
                    style={downloadPercent !== null ? { width: `${downloadPercent}%` } : undefined}
                  />
                </div>
                <p className="mt-[6px] text-[12px] text-[#757f9c]">
                  {downloadPercent !== null ? `${downloadPercent}%` : `已接收 ${formatBytes(receivedBytes)}`}
                </p>
              </div>
            )}

            <footer className="mt-[24px] flex items-center gap-[12px] border-t border-[#eef0f4] pt-[20px]">
              <UIButton
                disabled={downloading}
                onClick={() => void download()}
                className="inline-flex h-[40px] items-center gap-[8px] rounded-[12px] bg-[#18181a] px-[20px] text-[13px] text-white hover:bg-[#303030] disabled:opacity-60"
              >
                <Download className={downloading ? 'animate-pulse' : ''} />
                {downloading ? '下载中…' : '下载技能包 (.zip)'}
              </UIButton>
              <a
                href={`/enterprise/platform/general-skills?q=${encodeURIComponent(info.name || info.slug)}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex h-[40px] items-center rounded-[12px] border-[0.5px] border-[#e3e7f1] bg-white px-[16px] text-[13px] text-[#464C5E] transition-colors hover:border-[#cbd3e6] hover:bg-[#f6f7fa]"
              >
                去技能广场查看 ↗
              </a>
              {info.total_bytes > 0 && <span className="ml-[4px] text-[12px] text-[#757f9c]">共 {formatBytes(info.total_bytes)}</span>}
            </footer>
          </main>
        )}
      </div>
    </div>
  );
}
