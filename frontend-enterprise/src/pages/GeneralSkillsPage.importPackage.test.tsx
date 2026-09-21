// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { EnterpriseAuthUser } from '@/auth';
import { I18nProvider } from '@/i18n';
import { TooltipProvider } from '@/components/ui';
import { GeneralSkillNewPage } from './GeneralSkillsPage';

/**
 * zip / Markdown 包导入现在走 /import-package/preview-multipart（multipart 直传，
 * 双阶段进度：字节 0-95 → 服务端解析 96-99 → 完成 100）：解析结果填进编辑器表单，
 * 用户确认后手动保存——不再直接调 /import-package 落库并跳回列表。
 * 上传用 XHR（fetch 拿不到上传进度），所以这里桩 XMLHttpRequest。
 */

const overallAgent = {
  id: 'agent_overall',
  tenant_id: 'tenant_demo',
  name: '整体智能体',
  is_overall: true,
  status: 'active',
  metadata: {},
  resources: [],
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
};

const member: EnterpriseAuthUser = {
  id: 'user_member',
  tenant_id: 'tenant_demo',
  username: 'member',
  role: 'admin',
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

type FakeXhrOptions = { status?: number; response?: unknown };

/** 最小可用的 XHR 桩：记录 send 的 FormData，onload 返回 preview 响应，并触发两阶段 upload progress（50% → 100%）。 */
function stubXhr(expectedPath: string, response: unknown, status = 200) {
  const sentBodies: FormData[] = [];
  const sentUrls: string[] = [];
  class FakeXHR {
    status = 0;
    response: unknown = null;
    responseText = '';
    responseStatus = '';
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
    onprogress: ((event: ProgressEvent) => void) | null = null;
    private method = '';
    private url = '';
    private headers: Record<string, string> = {};

    open(method: string, url: string) {
      this.method = method;
      this.url = url;
    }

    setRequestHeader(key: string, value: string) {
      this.headers[key] = value;
    }

    addEventListener() {}

    send(body?: FormData) {
      sentBodies.push((body as FormData) || new FormData());
      sentUrls.push(this.url);
      expect(this.url).toContain(expectedPath);
      expect(this.method).toBe('POST');
      window.setTimeout(() => {
        this.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
        this.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 } as ProgressEvent);
        // 响应延后一个 tick：给 UI 一帧渲染「服务端解析中」的中间态
        window.setTimeout(() => {
          this.status = status;
          this.response = response;
          this.responseText = status >= 200 && status < 300 ? JSON.stringify(response ?? {}) : 'error';
          this.onload?.();
        }, 30);
      }, 0);
    }
  }
  vi.stubGlobal('XMLHttpRequest', FakeXHR as unknown as typeof XMLHttpRequest);
  return { sentBodies, sentUrls };
}

function renderEditor() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/enterprise/agents')) return jsonResponse([overallAgent]);
    if (url.includes('/api/enterprise/general-skills')) return jsonResponse([]);
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);

  render(
    <I18nProvider>
      <TooltipProvider>
        <MemoryRouter initialEntries={['/enterprise/general-skills/new']}>
          <GeneralSkillNewPage currentUser={member} />
        </MemoryRouter>
      </TooltipProvider>
    </I18nProvider>,
  );
  return fetchMock;
}

/** 等编辑器出现后，往 zip 输入框塞一个文件。 */
async function uploadZip(file: File) {
  const zipInput = await screen.findByTestId('general-skill-package-input');
  fireEvent.change(zipInput, { target: { files: [file] } });
}

function makeZipFile(): File {
  // 内容无所谓：XHR 已被桩掉，后端不会真解析它
  return new File(['PK\x03\x04 fake'], 'my-skill.zip', { type: 'application/zip' });
}

beforeEach(() => {
  window.localStorage.clear();
  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false, media: query, onchange: null,
      addEventListener: () => {}, removeEventListener: () => {},
      addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
    })) as typeof window.matchMedia;
  }
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe('GeneralSkillsPage zip 导入（multipart preview 后填表单）', () => {
  it('上传 zip 调 multipart preview 接口，解析结果填进表单，且不调 /import-package', async () => {
    const previewResponse = {
      filename: 'my-skill.zip',
      name: '邮差技能',
      slug: 'my-skill',
      description: '来自 zip 的描述',
      homepage: '',
      markdown: '---\nname: 邮差技能\n---\n\n# 邮差技能\n',
      files: [
        { path: 'SKILL.md', content: '---\nname: 邮差技能\n---\n\n# 邮差技能\n', size: 30, mime_type: 'text/markdown' },
        { path: 'scripts/run.py', content: "print('hi')\n", size: 12, mime_type: 'text/plain' },
      ],
      directories: ['scripts'],
    };
    const { sentBodies, sentUrls } = stubXhr('/api/enterprise/general-skills/import-package/preview-multipart', previewResponse);
    const fetchMock = renderEditor();
    await screen.findByText('技能文件');

    await uploadZip(makeZipFile());

    await waitFor(() => expect(sentBodies.length).toBe(1), { timeout: 3000 });
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes('/import-package"') || String(call[0]).endsWith('/import-package'))).toBe(false);
    // multipart 入口带 tenant_id query
    expect(sentUrls[0]).toContain('tenant_id=tenant_demo');

    // 表单被填充：名称进输入框，文件树出现 zip 里的脚本文件
    expect((await screen.findByLabelText('技能名称') as HTMLInputElement).value).toBe('邮差技能');
    await waitFor(() => {
      expect(screen.getByText('run.py')).toBeTruthy();
    });
    // 导入后停留在编辑表单（不跳列表）：基本信息卡与文件树都可交互
  });

  it('双阶段进度条：字节上传后进入“服务端解析中”，完成后消失', async () => {
    const previewResponse = {
      filename: 'my-skill.zip',
      name: '技能',
      markdown: '# 技能\n',
      files: [{ path: 'SKILL.md', content: '# 技能\n', size: 5, mime_type: 'text/markdown' }],
      directories: [],
    };
    stubXhr('/api/enterprise/general-skills/import-package/preview-multipart', previewResponse);
    renderEditor();
    await screen.findByText('技能文件');

    await uploadZip(makeZipFile());

    const bar = await screen.findByRole('progressbar', { name: '技能包上传进度' });
    expect(bar).toBeTruthy();
    // 100% 字节 → parsing 阶段文案（桩内两次 progress 同批触发，uploading 文案一闪而过）
    await waitFor(() => {
      expect(document.body.textContent).toContain('服务端解析中');
    });
    // 响应返回后进度条消失
    await waitFor(() => {
      expect(screen.queryByRole('progressbar', { name: '技能包上传进度' })).toBeNull();
    });
  });
});
