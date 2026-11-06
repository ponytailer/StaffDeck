// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import { notify } from '@/components/ui/app-toast';

import { api } from '../api/client';
import MyStaticAssetsPanel, { type StaticAssetRead } from './MyStaticAssetsPanel';

vi.mock('../api/client', () => ({
  TENANT_ID: 'tenant_demo',
  // 同源部署：绝对地址由当前 origin 拼，验证「库里只存相对路径」的约定
  API_BASE_URL: '',
  api: {
    get: vi.fn(),
    delete: vi.fn(),
    uploadWithProgress: vi.fn(),
  },
}));

const mockedApi = vi.mocked(api);

function makeAsset(overrides: Partial<StaticAssetRead> = {}): StaticAssetRead {
  return {
    id: 'sasset_1',
    filename: 'a.txt',
    content_type: 'text/plain',
    size: 2048,
    sha256: 'a'.repeat(64),
    status: 'active',
    download_count: 3,
    created_at: '2026-10-01T08:00:00Z',
    path: '/f/tok123/a.txt',
    ...overrides,
  };
}

function renderPanel(props: { searchTerm?: string } = {}) {
  return render(
    <I18nProvider>
      <MyStaticAssetsPanel {...props} />
    </I18nProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(notify, 'error').mockReturnValue(0);
  vi.spyOn(notify, 'success').mockReturnValue(0);
  mockedApi.get.mockResolvedValue({
    items: [makeAsset()],
    used_bytes: 2048,
    user_quota_bytes: 200 * 1024 * 1024,
    max_file_bytes: 20 * 1024 * 1024,
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe('MyStaticAssetsPanel', () => {
  it('渲染文件列表，并把相对路径拼成可复制的绝对地址', async () => {
    renderPanel();

    expect((await screen.findAllByText('a.txt')).length).toBeGreaterThan(0);
    // 后端只回 /f/{token}/{filename}，域名由前端补
    const expected = `${window.location.origin}/f/tok123/a.txt`;
    expect(screen.getAllByText(expected).length).toBeGreaterThan(0);
    expect(screen.getByText(/下载 3 次/)).toBeTruthy();
  });

  it('展示已用空间与单文件上限，并提示链接公开可访问', async () => {
    renderPanel();
    await screen.findAllByText('a.txt');

    expect(screen.getByText(/已用 2\.0 KB \/ 200 MB/)).toBeTruthy();
    expect(screen.getByText(/单个文件最大 20 MB/)).toBeTruthy();
    // 公开下载是固有代价，必须在入口处提示
    expect(screen.getByText(/链接免登录即可访问/)).toBeTruthy();
  });

  it('空态给出用途说明，区分「没有文件」与「搜索无结果」', async () => {
    mockedApi.get.mockResolvedValue({
      items: [],
      used_bytes: 0,
      user_quota_bytes: 200 * 1024 * 1024,
      max_file_bytes: 20 * 1024 * 1024,
    });
    const { unmount } = renderPanel();
    expect(await screen.findByText('还没有上传过文件')).toBeTruthy();
    unmount();

    renderPanel({ searchTerm: '不存在' });
    expect(await screen.findByText('没有匹配的文件')).toBeTruthy();
  });

  it('超过单文件上限时直接拦截，不发起上传', async () => {
    mockedApi.get.mockResolvedValue({
      items: [],
      used_bytes: 0,
      user_quota_bytes: 200 * 1024 * 1024,
      max_file_bytes: 10,
    });
    renderPanel();
    await screen.findByText('还没有上传过文件');

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const big = new File(['x'.repeat(64)], 'big.bin', { type: 'application/octet-stream' });
    fireEvent.change(input, { target: { files: [big] } });

    await waitFor(() => expect(notify.error).toHaveBeenCalled());
    expect(mockedApi.uploadWithProgress).not.toHaveBeenCalled();
  });

  it('上传成功后把新文件插到列表最前', async () => {
    mockedApi.get.mockResolvedValue({
      items: [],
      used_bytes: 0,
      user_quota_bytes: 200 * 1024 * 1024,
      max_file_bytes: 20 * 1024 * 1024,
    });
    mockedApi.uploadWithProgress.mockResolvedValue(
      makeAsset({ id: 'sasset_new', filename: 'new.pdf', path: '/f/newtok/new.pdf' }),
    );
    renderPanel();
    await screen.findByText('还没有上传过文件');

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File(['abc'], 'new.pdf', { type: 'application/pdf' })] },
    });

    expect((await screen.findAllByText('new.pdf')).length).toBeGreaterThan(0);
    expect(mockedApi.uploadWithProgress).toHaveBeenCalledTimes(1);
    expect(notify.success).toHaveBeenCalled();
  });

  it('删除前必须确认，确认后才调接口并从列表移除', async () => {
    renderPanel();
    await screen.findAllByText('a.txt');

    fireEvent.click(screen.getAllByLabelText('删除 a.txt')[0]);
    expect(mockedApi.delete).not.toHaveBeenCalled();

    const confirm = await screen.findByRole('button', { name: '删除' });
    mockedApi.delete.mockResolvedValue(makeAsset());
    fireEvent.click(confirm);

    await waitFor(() => expect(mockedApi.delete).toHaveBeenCalledTimes(1));
    expect(String(mockedApi.delete.mock.calls[0][0])).toContain('/api/enterprise/static-assets/sasset_1');
  });
});
