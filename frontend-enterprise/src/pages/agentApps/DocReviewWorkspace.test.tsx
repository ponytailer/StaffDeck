// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { I18nProvider } from '@/i18n';
import type { EnterpriseAuthUser } from '@/auth';
import type { ModelConfigRead } from '@/types';

import DocReviewWorkspace from './DocReviewWorkspace';
import { parseDocx, type DocParseResult } from '@/api/docReview';

const user: EnterpriseAuthUser = {
  id: 'user-1',
  username: 'member',
  tenant_id: 'tenant_demo',
  role: 'member',
};

vi.mock('@/api/docReview', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/docReview')>();
  return {
    ...original,
    parseDocx: vi.fn(),
  };
});

const mockedParseDocx = vi.mocked(parseDocx);

/** docx-preview 的替身：jsdom 里不做真实渲染，只验证「当前版本」被送进渲染管线。 */
const { renderAsyncMock } = vi.hoisted(() => ({ renderAsyncMock: vi.fn() }));
vi.mock('docx-preview', () => ({ renderAsync: renderAsyncMock }));

/** api.post / api.postBlob 的统一替身：按路径在各用例里 mockImplementation。 */
const { postMock, postBlobMock } = vi.hoisted(() => ({ postMock: vi.fn(), postBlobMock: vi.fn() }));
vi.mock('@/api/client', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/client')>();
  return {
    ...original,
    post: postMock,
    postBlob: postBlobMock,
    api: { ...original.api, post: postMock, postBlob: postBlobMock },
  };
});

function modelConfig(overrides: Partial<ModelConfigRead> = {}): ModelConfigRead {
  return {
    id: 'model-1',
    tenant_id: 'tenant_demo',
    name: '我的 flash',
    provider: 'openai_compatible',
    api_protocol: 'openai_chat_completions',
    base_url: 'https://example.com',
    api_key_masked: 'sk-***',
    model: 'deepseek-v4-flash',
    temperature: 0.2,
    max_output_tokens: 8192,
    extra_body: {},
    custom_headers: {},
    protocol_options: {},
    legacy_unmapped_options: {},
    trust_status: 'verified',
    verification_attempt_status: 'idle',
    config_revision: 1,
    security_revision: 1,
    is_default: true,
    is_intent_recognition: false,
    enabled: true,
    updated_at: '2026-09-01T00:00:00Z',
  } as ModelConfigRead;
}

const entry = {
  id: 'doc-reviewer',
  name: 'AI 文档审阅',
  updatedAt: '2026-09-29',
  summary: '',
  description: '',
  author: '',
  updated_at: '',
  category: '',
  tags: [],
  entry: 'doc-reviewer',
  capability: 'doc-review',
  adminOnly: false,
  status: 'active' as const,
  prompt: '',
};

const PARSED: DocParseResult = {
  doc_id: 'doc-1',
  doc_name: '服务协议.docx',
  title: '服务协议',
  blocks: [
    { id: 'p0', text: '服务协议', kind: 'heading', level: 1, in_table: false },
    { id: 'p1', text: '甲方：上海某某科技有限公司。', kind: 'paragraph', level: 0, in_table: false },
    { id: 'p2', text: '乙方负责违约责任的约定存在错别字：违约为本。', kind: 'paragraph', level: 0, in_table: false },
  ],
};

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    text: async () => JSON.stringify(body ?? {}),
  } as Response;
}

function renderWorkspace() {
  return render(
    <I18nProvider>
      <MemoryRouter>
        <DocReviewWorkspace entry={entry} currentUser={user} />
      </MemoryRouter>
    </I18nProvider>,
  );
}

function pickFile() {
  const input = document.querySelector('[data-testid="doc-import-input"]') as HTMLInputElement;
  // jsdom 的 FileList 只读：用 defineProperty 注入，再触发 change
  Object.defineProperty(input, 'files', { value: [new File(['x'], '服务协议.docx')], configurable: true });
  fireEvent.change(input);
}

/** 最近一次送进 doc:export 的 blocks（右栏渲染快照的内容来源）。 */
function lastExportBlocks(): Array<{ id: string; text: string; comment?: string }> {
  const calls = postBlobMock.mock.calls.filter(([path]) => String(path).includes('doc:export'));
  const latest = calls[calls.length - 1];
  return (latest?.[1] as { blocks: Array<{ id: string; text: string; comment?: string }> })?.blocks ?? [];
}

/** 修订追踪开关现在是 Switch 组件（默认开启）：点击切换开/关。 */
function toggleTrackChanges() {
  fireEvent.click(screen.getByRole('switch', { name: '修订追踪开关' }));
}

beforeEach(() => {
  // api.get（模型列表）走真实 fetch 链路，给一个可达响应；api.post/postBlob 用替身
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/api/enterprise/model-configs')) return jsonResponse([modelConfig()]);
      return jsonResponse({});
    }),
  );
  postMock.mockReset();
  postMock.mockResolvedValue({});
  postBlobMock.mockReset();
  postBlobMock.mockResolvedValue(new Blob(['x']));
  renderAsyncMock.mockReset();
  renderAsyncMock.mockResolvedValue(undefined);
  mockedParseDocx.mockReset();
  mockedParseDocx.mockResolvedValue(PARSED);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('DocReviewWorkspace', () => {
  it('初始显示上传区，解析后把当前版本送进 docx-preview 渲染', async () => {
    renderWorkspace();
    expect(screen.getByText('上传 Word 文档')).toBeTruthy();

    pickFile();
    await waitFor(() => expect(screen.getByText('一键审阅')).toBeTruthy());
    expect(screen.getByText('服务协议.docx')).toBeTruthy();
    expect(screen.getByText('导出 Word')).toBeTruthy();

    // 右栏走 doc:export（原文）→ docx-preview 渲染管线
    await waitFor(() => expect(renderAsyncMock).toHaveBeenCalled());
    const exported = lastExportBlocks();
    expect(exported).toHaveLength(3);
    expect(exported[2].text).toContain('违约为本');
  });

  it('修订追踪关闭时：一键审阅应用修改替换原文，可在修改列表里撤销', async () => {
    postMock.mockImplementation(async (path: string) => {
      if (path.includes('doc:review')) {
        return {
          issues: [
            {
              id: 'i0',
              type: '错别字',
              block_ids: ['p2'],
              title: '「违约为本」应为「违约为准」',
              detail: '',
              fixes: [{ block_id: 'p2', new_text: '乙方负责违约责任的约定以本协议为准。' }],
            },
          ],
          reviewed_blocks: 3,
          notes: [],
        };
      }
      return {};
    });

    renderWorkspace();
    pickFile();
    await waitFor(() => expect(screen.getByRole('switch', { name: '修订追踪开关' })).toBeTruthy());
    // 默认修订追踪开启：先关掉，走「直接替换原文」路径
    toggleTrackChanges();

    fireEvent.click(screen.getByText('一键审阅'));
    await waitFor(() => expect(screen.getByText('「违约为本」应为「违约为准」')).toBeTruthy());

    fireEvent.click(screen.getByText('应用修改'));
    // 状态栏出现「已修改」，且重渲染的 blocks 携带新文本、无批注
    await waitFor(() => expect(screen.getByText(/已修改 1 段/)).toBeTruthy());
    await waitFor(() => expect(lastExportBlocks()[2].text).toBe('乙方负责违约责任的约定以本协议为准。'));
    expect(lastExportBlocks()[2].comment).toBeFalsy();

    // 展开修改列表 → 单段撤销 → 回到原文
    fireEvent.click(screen.getByText(/已修改 1 段/));
    fireEvent.click(screen.getByLabelText('撤销第 p2 段修改'));
    await waitFor(() => expect(screen.queryByText(/已修改 \d+ 段/)).toBeNull());
    await waitFor(() => expect(lastExportBlocks()[2].text).toContain('违约为本'));
  });

  it('修订追踪开启时：应用审阅修改不改原文，落成 Word 批注', async () => {
    postMock.mockImplementation(async (path: string) => {
      if (path.includes('doc:review')) {
        return {
          issues: [
            {
              id: 'i0',
              type: '错别字',
              block_ids: ['p2'],
              title: '「违约为本」应为「违约为准」',
              detail: '',
              fixes: [{ block_id: 'p2', new_text: '乙方负责违约责任的约定以本协议为准。' }],
            },
          ],
          reviewed_blocks: 3,
          notes: [],
        };
      }
      return {};
    });

    renderWorkspace();
    pickFile();
    await waitFor(() => expect(screen.getByText('一键审阅')).toBeTruthy());
    // 默认修订追踪开启，不动开关

    fireEvent.click(screen.getByText('一键审阅'));
    await waitFor(() => expect(screen.getByText('应用修改')).toBeTruthy());
    fireEvent.click(screen.getByText('应用修改'));

    // 徽标显示「已加批注」，右栏重渲染的 blocks：原文未动 + 批注带上建议
    await waitFor(() => expect(screen.getByText('已加批注')).toBeTruthy());
    await waitFor(() => {
      const exported = lastExportBlocks();
      expect(exported[2].text).toContain('违约为本');
      expect(exported[2].comment).toContain('错别字');
      expect(exported[2].comment).toContain('建议改为：乙方负责违约责任的约定以本协议为准。');
    });
    // 右栏状态栏出现批注计数
    await waitFor(() => expect(screen.getByText(/批注 1 条/)).toBeTruthy());
  });

  it('恢复全部原文后，已应用的审阅项回滚为可再次应用', async () => {
    postMock.mockImplementation(async (path: string) => {
      if (path.includes('doc:review')) {
        return {
          issues: [
            {
              id: 'i0',
              type: '错别字',
              block_ids: ['p2'],
              title: '「违约为本」应为「违约为准」',
              detail: '',
              fixes: [{ block_id: 'p2', new_text: '乙方负责违约责任的约定以本协议为准。' }],
            },
          ],
          reviewed_blocks: 3,
          notes: [],
        };
      }
      return {};
    });

    renderWorkspace();
    pickFile();
    await waitFor(() => expect(screen.getByText('一键审阅')).toBeTruthy());
    // 走「替换原文」路径：先关闭修订追踪
    toggleTrackChanges();

    fireEvent.click(screen.getByText('一键审阅'));
    await waitFor(() => expect(screen.getByText('应用修改')).toBeTruthy());
    fireEvent.click(screen.getByText('应用修改'));
    await waitFor(() => expect(screen.getByText(/已修改 1 段/)).toBeTruthy());
    // 应用后卡片进入「已应用」态，按钮消失
    expect(screen.queryByText('应用修改')).toBeNull();

    // 恢复全部原文：审阅项状态回滚，「应用修改」重新可点
    fireEvent.click(screen.getByText('恢复全部原文'));
    await waitFor(() => expect(screen.queryByText(/已修改 \d+ 段/)).toBeNull());
    const applyAgain = await screen.findByText('应用修改');
    fireEvent.click(applyAgain);
    await waitFor(() => expect(screen.getByText(/已修改 1 段/)).toBeTruthy());
    await waitFor(() => expect(lastExportBlocks()[2].text).toBe('乙方负责违约责任的约定以本协议为准。'));
  });

  it('修订追踪开启时，对话修改确认后落成批注（原文不动）', async () => {
    postMock.mockImplementation(async (path: string) => {
      if (path.includes('doc:chat')) {
        return {
          reply: '已整理总结。',
          actions: [{ block_id: 'p1', new_text: '甲方：上海某某科技有限公司（总结落段）。', reason: '归纳总结' }],
        };
      }
      return {};
    });

    renderWorkspace();
    pickFile();
    await waitFor(() => expect(screen.getByText('发送')).toBeTruthy());

    const textarea = screen.getByPlaceholderText(/描述修改、写作要求/);
    fireEvent.change(textarea, { target: { value: '把甲方的信息归纳成一句话' } });
    fireEvent.click(screen.getByText('发送'));

    // 修订追踪默认开启：建议先出现在左栏待确认区，正文未变
    await waitFor(() => expect(screen.getByText(/建议：归纳总结/)).toBeTruthy());
    expect(lastExportBlocks()[1].text).toBe('甲方：上海某某科技有限公司。');

    // 确认应用：原文不动，批注落上
    fireEvent.click(screen.getByText('应用', { exact: true }));
    await waitFor(() => expect(screen.getByText(/批注 1 条/)).toBeTruthy());
    await waitFor(() => {
      const exported = lastExportBlocks();
      expect(exported[1].text).toBe('甲方：上海某某科技有限公司。');
      expect(exported[1].comment).toContain('归纳总结');
      expect(exported[1].comment).toContain('建议改为');
    });
  });

  it('修订追踪关闭时，对话修改直接替换原文并触发重渲染', async () => {
    postMock.mockImplementation(async (path: string) => {
      if (path.includes('doc:chat')) {
        return {
          reply: '已修正。',
          actions: [{ block_id: 'p2', new_text: '乙方负责违约责任的约定以本协议为准。', reason: '修正错别字' }],
        };
      }
      return {};
    });

    renderWorkspace();
    pickFile();
    await waitFor(() => expect(screen.getByRole('switch', { name: '修订追踪开关' })).toBeTruthy());

    // 关闭修订追踪
    toggleTrackChanges();

    const textarea = screen.getByPlaceholderText(/描述修改、写作要求/);
    fireEvent.change(textarea, { target: { value: '把错别字改掉' } });
    fireEvent.click(screen.getByText('发送'));

    await waitFor(() => expect(screen.getByText(/已修改 1 段/)).toBeTruthy());
    await waitFor(() => expect(lastExportBlocks()[2].text).toBe('乙方负责违约责任的约定以本协议为准。'));
  });

  it('多次审阅的 issue 不串状态：应用第二批的项不影响第一批', async () => {
    let run = 0;
    postMock.mockImplementation(async (path: string) => {
      if (path.includes('doc:review')) {
        run += 1;
        return {
          // 两批的 id 都是 i0（后端按批次重编号），复现撞 id 场景
          issues: [
            {
              id: 'i0',
              type: '错别字',
              block_ids: ['p2'],
              title: `第 ${run} 批的问题`,
              detail: '',
              fixes: [{ block_id: 'p2', new_text: `第 ${run} 批修改后的文本。` }],
            },
          ],
          reviewed_blocks: 3,
          notes: [],
        };
      }
      return {};
    });

    renderWorkspace();
    pickFile();
    await waitFor(() => expect(screen.getByText('一键审阅')).toBeTruthy());
    toggleTrackChanges(); // 直接替换原文路径

    fireEvent.click(screen.getByText('一键审阅'));
    await waitFor(() => expect(screen.getByText('第 1 批的问题')).toBeTruthy());
    fireEvent.click(screen.getByText('一键审阅'));
    await waitFor(() => expect(screen.getByText('第 2 批的问题')).toBeTruthy());

    // 两批各有一个「应用修改」：只点第二批的
    const applyButtons = screen.getAllByText('应用修改');
    expect(applyButtons).toHaveLength(2);
    fireEvent.click(applyButtons[1]);

    await waitFor(() => expect(screen.getByText(/已修改 1 段/)).toBeTruthy());
    // 第一批的审阅项保持「待处理」，按钮仍在
    expect(screen.getAllByText('应用修改')).toHaveLength(1);
    expect(lastExportBlocks()[2].text).toBe('第 2 批修改后的文本。');
  });

  it('未关联段落的意见（如数据缺失）标题不可点定位', async () => {
    postMock.mockImplementation(async (path: string) => {
      if (path.includes('doc:review')) {
        return {
          issues: [
            {
              id: 'i0',
              type: '数据缺失',
              block_ids: [],
              title: '缺少服务费用明细',
              detail: '文档未包含费用相关章节',
              fixes: [],
            },
          ],
          reviewed_blocks: 3,
          notes: [],
        };
      }
      return {};
    });

    renderWorkspace();
    pickFile();
    await waitFor(() => expect(screen.getByText('一键审阅')).toBeTruthy());

    fireEvent.click(screen.getByText('一键审阅'));
    await waitFor(() => expect(screen.getByText('缺少服务费用明细')).toBeTruthy());
    // 全局性意见：没有定位按钮，点击标题也不应触发跳转/报错
    expect(screen.queryByTitle('点击定位到文档对应位置')).toBeNull();
    fireEvent.click(screen.getByText('缺少服务费用明细'));
    expect(screen.queryByText('应用修改')).toBeNull();
  });
});
