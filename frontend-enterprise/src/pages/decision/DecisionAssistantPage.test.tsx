// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const predictLayaQuestions = vi.fn();
const checkLayaHealth = vi.fn();
const fetchLayaApiAccess = vi.fn();
const notifyError = vi.fn();
const notifySuccess = vi.fn();

vi.mock('@/components/ui/app-toast', () => ({
  notify: {
    success: (...args: unknown[]) => notifySuccess(...args),
    error: (...args: unknown[]) => notifyError(...args),
    warning: vi.fn(),
    info: vi.fn(),
    loading: vi.fn(),
    dismiss: vi.fn(),
  },
}));

vi.mock('../../api/client', () => ({
  TENANT_ID: 'tenant_demo',
  ApiError: class ApiError extends Error {
    status = 500;
  },
  api: {
    get: vi.fn(() => Promise.resolve({})),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

vi.mock('../../api/laya', () => ({
  predictLayaQuestions: (...args: unknown[]) => predictLayaQuestions(...args),
  checkLayaHealth: () => checkLayaHealth(),
  fetchLayaApiAccess: () => fetchLayaApiAccess(),
}));

const copyTextToClipboard = vi.fn();
vi.mock('@/lib/clipboard', () => ({
  copyTextToClipboard: (...args: unknown[]) => copyTextToClipboard(...args),
}));

import { I18nProvider } from '@/i18n';

import DecisionAssistantPage from './DecisionAssistantPage';

/** 后端 /api/enterprise/laya/api-access 的返回值；host 由 .env 的 PUBLIC_BASE_URL 决定。 */
const API_ACCESS = {
  required_scope: 'decisions:run',
  endpoint_path: '/api/v1/decisions/predict',
  docs_path: '/api/v1/docs',
  public_base_url: 'https://decision.example.com',
};
const CONFIGURED_ENDPOINT = `${API_ACCESS.public_base_url}${API_ACCESS.endpoint_path}`;

const BACKGROUND_PLACEHOLDER =
  '例如：客户来电反馈账单被重复扣款，情绪激动，明确要求退款并威胁投诉';
const NOUL_PLACEHOLDER = '如：用户是否明确要求退款？';
const CHOICE_PLACEHOLDER = '如：这条工单应由哪个部门处理？';
const SCORE_PLACEHOLDER = '如：这条工单的紧急程度是多少？';
const OPTION_PLACEHOLDER = '选项名，如 billing';
const LEVEL_PLACEHOLDER = '档位文案，如 很急，涉及钱 / 违约 / 取消订阅';

function renderPage() {
  return render(
    <I18nProvider>
      <DecisionAssistantPage />
    </I18nProvider>,
  );
}

function fillBackground(value = '客户要求退款并威胁投诉') {
  fireEvent.change(screen.getByPlaceholderText(BACKGROUND_PLACEHOLDER), { target: { value } });
}

function values(placeholder: string): string[] {
  return (screen.getAllByPlaceholderText(placeholder) as HTMLInputElement[]).map((input) => input.value);
}

function setValue(placeholder: string, value: string, index = 0) {
  fireEvent.change(screen.getAllByPlaceholderText(placeholder)[index], { target: { value } });
}

beforeEach(() => {
  predictLayaQuestions.mockReset();
  notifyError.mockReset();
  notifySuccess.mockReset();
  checkLayaHealth.mockReset();
  checkLayaHealth.mockResolvedValue({ reachable: true, upstream_url: 'http://laya.local/health' });
  fetchLayaApiAccess.mockReset();
  fetchLayaApiAccess.mockResolvedValue({ ...API_ACCESS });
});

afterEach(() => {
  cleanup();
});

describe('DecisionAssistantPage', () => {
  it('starts with one 是非 question and supports adding / removing decision items', async () => {
    renderPage();
    expect(screen.getAllByPlaceholderText(NOUL_PLACEHOLDER)).toHaveLength(1);

    fireEvent.click(screen.getByText('添加决策内容'));
    expect(screen.getAllByPlaceholderText(NOUL_PLACEHOLDER)).toHaveLength(2);

    fireEvent.click(screen.getAllByLabelText('删除该决策内容')[1]);
    expect(screen.getAllByPlaceholderText(NOUL_PLACEHOLDER)).toHaveLength(1);
  });

  it('never renders the random request key on the form', () => {
    renderPage();
    fireEvent.click(screen.getByText('选择 · 分类命中'));
    expect(document.querySelector('code')).toBeNull();
    expect(screen.queryByText(/^q_[a-z0-9]{8}$/)).toBeNull();
  });

  it('switches a question to 选择 · 分类命中 and exposes two option rows', () => {
    renderPage();
    fireEvent.click(screen.getByText('选择 · 分类命中'));
    expect(screen.getAllByPlaceholderText(OPTION_PLACEHOLDER)).toHaveLength(2);
    // 「适用说明」列已下线：每个选项只有一行输入
    expect(screen.queryByPlaceholderText('适用说明，如 账单、扣款、发票、退款、支付')).toBeNull();

    fireEvent.click(screen.getByText('添加选项'));
    expect(screen.getAllByPlaceholderText(OPTION_PLACEHOLDER)).toHaveLength(3);

    // 少于两行时禁止删除，保证每个问题至少有 2 个选项
    fireEvent.click(screen.getAllByLabelText('删除该选项')[2]);
    expect(screen.getAllByPlaceholderText(OPTION_PLACEHOLDER)).toHaveLength(2);
    expect(screen.getAllByLabelText('删除该选项')[0]).toHaveProperty('disabled', true);
  });

  it('keeps each flavour draft isolated when toggling the question type', () => {
    renderPage();
    fireEvent.click(screen.getByText('添加决策内容'));
    setValue(NOUL_PLACEHOLDER, 'Q1-是否要明确退款？', 0);

    // 第二张卡切到「分类命中」并填写
    fireEvent.click(screen.getAllByText('选择 · 分类命中')[1]);
    setValue(CHOICE_PLACEHOLDER, '这条工单应由哪个部门处理？');
    setValue(OPTION_PLACEHOLDER, 'billing', 0);
    setValue(OPTION_PLACEHOLDER, 'technical', 1);

    // 切到「程度评分」：分类的选项名不能被搬成档位文案
    fireEvent.click(screen.getAllByText('选择 · 程度评分')[1]);
    expect(values(LEVEL_PLACEHOLDER)).toEqual(['', '']);
    expect((screen.getByPlaceholderText(SCORE_PLACEHOLDER) as HTMLInputElement).value).toBe(
      '这条工单应由哪个部门处理？',
    );
    setValue(LEVEL_PLACEHOLDER, '不急，普通咨询', 0);
    setValue(LEVEL_PLACEHOLDER, '立刻处理，已有投诉风险', 1);

    // 切到「是非」：选项区收起，但两份草稿都还在
    fireEvent.click(screen.getAllByText('是非（二选一）')[1]);
    expect(screen.queryAllByPlaceholderText(OPTION_PLACEHOLDER)).toHaveLength(0);
    expect(screen.queryAllByPlaceholderText(LEVEL_PLACEHOLDER)).toHaveLength(0);

    // 来回切换，各自的内容原样回来，互不覆盖
    fireEvent.click(screen.getAllByText('选择 · 分类命中')[1]);
    expect(values(OPTION_PLACEHOLDER)).toEqual(['billing', 'technical']);
    fireEvent.click(screen.getAllByText('选择 · 程度评分')[1]);
    expect(values(LEVEL_PLACEHOLDER)).toEqual(['不急，普通咨询', '立刻处理，已有投诉风险']);

    // 第一张卡（是非）的内容没有被牵连
    expect((screen.getByPlaceholderText(NOUL_PLACEHOLDER) as HTMLInputElement).value).toBe(
      'Q1-是否要明确退款？',
    );
  });

  it('blocks submission and reports the reason when the form is incomplete', async () => {
    renderPage();
    fireEvent.click(screen.getByText('开始决策'));

    await waitFor(() => expect(screen.getByText('请先填写决策背景')).toBeDefined());
    expect(notifyError).toHaveBeenCalledWith('请先填写决策背景');
    expect(predictLayaQuestions).not.toHaveBeenCalled();
  });

  it('mirrors the form with 待决策 rows before submission and replaces them with answers', async () => {
    predictLayaQuestions.mockImplementation((body: { questions: Record<string, unknown> }) =>
      Promise.resolve({
        answers: Object.fromEntries(
          Object.keys(body.questions).map((key) => [key, { type: 'noul', noul: 0.9, confidence: 0.9 }]),
        ),
        elapsed_ms: 120,
      }),
    );

    renderPage();
    // 左表单 1 条 → 右栏 1 条待决策
    expect(screen.getAllByText('待决策')).toHaveLength(1);
    fireEvent.click(screen.getByText('添加决策内容'));
    expect(screen.getAllByText('待决策')).toHaveLength(2);

    fillBackground();
    setValue(NOUL_PLACEHOLDER, '用户是否明确要求退款？', 0);
    setValue(NOUL_PLACEHOLDER, '是否涉及投诉风险？', 1);
    fireEvent.click(screen.getByText('开始决策'));

    // getAllByText 在 0 命中时会抛错，断言「清空」必须用 queryAllByText
    await waitFor(() => expect(screen.queryAllByText('待决策')).toHaveLength(0));
    // 两条问题都拿到答案，一一对应而非合并成一条
    expect(screen.getAllByText('置信度 高 90.0%')).toHaveLength(2);
    expect(screen.getByText('是否涉及投诉风险？')).toBeDefined();
  });

  it('posts the form as a Laya questions schema and renders the verdicts', async () => {
    // 上游按请求里的 key 回包，mock 也照此回显，避免用写死的 key 掩盖对齐问题
    predictLayaQuestions.mockImplementation((body: { questions: Record<string, unknown> }) => {
      const [key] = Object.keys(body.questions);
      return Promise.resolve({
        answers: { [key]: { type: 'noul', noul: 0.9961, confidence: 0.9961 } },
        routing: { model: 'multilingual' },
        elapsed_ms: 421.4,
        upstream_url: 'http://8.153.146.109:8080/predict',
      });
    });

    renderPage();
    fillBackground();
    setValue(NOUL_PLACEHOLDER, '用户是否明确要求退款？');
    fireEvent.click(screen.getByText('开始决策'));

    await waitFor(() => expect(predictLayaQuestions).toHaveBeenCalledTimes(1));
    const [body, signal] = predictLayaQuestions.mock.calls[0];
    expect(body.state).toEqual({ background: '客户要求退款并威胁投诉' });
    const keys = Object.keys(body.questions);
    expect(keys).toHaveLength(1);
    expect(keys[0]).toMatch(/^q_/);
    expect(body.questions[keys[0]]).toEqual({
      type: 'noul',
      instructions: '用户是否明确要求退款？',
    });
    expect(signal).toBeInstanceOf(AbortSignal);

    // 「是」既是结论正文，也是概率分布里的档位名 —— 只有结论正文是 <p>
    await waitFor(() => expect(screen.getByText('置信度 高 99.6%')).toBeDefined());
    expect(screen.getAllByText('是').filter((node) => node.tagName === 'P')).toHaveLength(1);
    expect(screen.getByText('模型 multilingual')).toBeDefined();
    expect(screen.getByText('耗时 421 ms')).toBeDefined();
  });

  it('keeps the choice options aligned with the returned answer', async () => {
    predictLayaQuestions.mockImplementation((body: { questions: Record<string, unknown> }) => {
      const [key] = Object.keys(body.questions);
      return Promise.resolve({
        answers: {
          [key]: {
            type: 'choice',
            choice: 'billing',
            probabilities: { billing: 0.9949, technical: 0.0051 },
            confidence: 0.9738,
          },
        },
        routing: { model: 'multilingual' },
        elapsed_ms: 300,
      });
    });

    renderPage();
    fillBackground();
    fireEvent.click(screen.getByText('选择 · 分类命中'));
    setValue(CHOICE_PLACEHOLDER, '这条工单应由哪个部门处理？');
    setValue(OPTION_PLACEHOLDER, 'billing', 0);
    setValue(OPTION_PLACEHOLDER, 'technical', 1);
    fireEvent.click(screen.getByText('开始决策'));

    await waitFor(() => expect(predictLayaQuestions).toHaveBeenCalledTimes(1));
    const [body] = predictLayaQuestions.mock.calls[0];
    const [key] = Object.keys(body.questions);
    expect(body.questions[key]).toEqual({
      type: 'choice',
      instructions: '这条工单应由哪个部门处理？',
      criteria: { billing: 'billing', technical: 'technical' },
    });

    // 结论正文（<p>）是命中的选项名；分布条里也有同名文本，需按标签筛
    await waitFor(() => expect(screen.getByText('命中选项 billing')).toBeDefined());
    expect(screen.getAllByText('billing').filter((node) => node.tagName === 'P')).toHaveLength(1);
  });

  it('drops the stale verdict of a question whose type changed', async () => {
    predictLayaQuestions.mockImplementation((body: { questions: Record<string, unknown> }) => {
      const [key] = Object.keys(body.questions);
      return Promise.resolve({
        answers: {
          [key]: {
            type: 'choice',
            choice: 'billing',
            probabilities: { billing: 0.9, technical: 0.1 },
            confidence: 0.99,
          },
        },
        elapsed_ms: 210,
      });
    });

    renderPage();
    fillBackground();
    fireEvent.click(screen.getByText('选择 · 分类命中'));
    setValue(CHOICE_PLACEHOLDER, '这条工单应由哪个部门处理？');
    setValue(OPTION_PLACEHOLDER, 'billing', 0);
    setValue(OPTION_PLACEHOLDER, 'technical', 1);
    fireEvent.click(screen.getByText('开始决策'));

    await waitFor(() => expect(screen.getByText('命中选项 billing')).toBeDefined());

    // 题型改成是非后，分类的旧结论不能再挂在它下面
    fireEvent.click(screen.getByText('是非（二选一）'));
    expect(screen.queryByText('命中选项 billing')).toBeNull();
    expect(screen.getAllByText('待决策')).toHaveLength(1);
  });

  it('hides the routing block from the raw response viewer', async () => {
    predictLayaQuestions.mockResolvedValue({
      answers: { q_replay: { type: 'noul', noul: 0.9, confidence: 0.9 } },
      routing: {
        model: 'multilingual',
        repo: 'convaiinnovations/laya/multilingual',
        reason: 'non-Latin script',
        detection: { script: 'han', non_latin_fraction: 1 },
      },
      elapsed_ms: 88,
      upstream_url: 'http://laya.local/predict',
    });

    renderPage();
    fillBackground();
    setValue(NOUL_PLACEHOLDER, '用户是否明确要求退款？');
    fireEvent.click(screen.getByText('开始决策'));
    await waitFor(() => expect(screen.getByText('耗时 88 ms')).toBeDefined());

    fireEvent.click(screen.getByText('查看原始返回'));
    const raw = document.querySelector('details pre')?.textContent ?? '';
    expect(raw).not.toContain('routing');
    expect(raw).not.toContain('multilingual');
    expect(raw).not.toContain('script');
    // 结论与耗时仍然保留
    expect(raw).toContain('"noul": 0.9');
    expect(raw).toContain('"elapsed_ms": 88');
    expect(raw).toContain('http://laya.local/predict');
  });

  it('surfaces backend failures inline', async () => {
    predictLayaQuestions.mockRejectedValue(new Error('无法连接 Laya 决策服务'));

    renderPage();
    fillBackground();
    setValue(NOUL_PLACEHOLDER, '用户是否明确要求退款？');
    fireEvent.click(screen.getByText('开始决策'));

    await waitFor(() => expect(notifyError).toHaveBeenCalledWith('无法连接 Laya 决策服务'));
    expect(screen.getByText('无法连接 Laya 决策服务')).toBeDefined();
  });

  it('warns when the decision service is unreachable', async () => {
    checkLayaHealth.mockResolvedValue({
      reachable: false,
      upstream_url: 'http://8.153.146.109:8080/health',
      message: 'connect refused',
    });

    renderPage();
    await waitFor(() => expect(screen.getByText(/决策服务当前不可达/)).toBeDefined());
  });

  it('loads the official ticket sample into the form', async () => {
    renderPage();
    fireEvent.click(screen.getByText('填入示例'));

    expect(screen.getByText('决策内容')).toBeDefined();
    await waitFor(() => expect(screen.getAllByPlaceholderText(OPTION_PLACEHOLDER)).toHaveLength(4));
    expect(screen.getAllByPlaceholderText(NOUL_PLACEHOLDER)).toHaveLength(2);
    expect(
      (screen.getByPlaceholderText(BACKGROUND_PLACEHOLDER) as HTMLTextAreaElement).value,
    ).toContain('重复扣款');
  });
});

describe('DecisionAssistantPage · API 接入', () => {
  function openApiDialog() {
    fireEvent.click(screen.getByText('API 接入'));
  }

  function sampleCode(language: 'curl' | 'python' | 'node' = 'curl'): string {
    return document.querySelector(`[data-testid="decision-api-sample-${language}"]`)?.textContent ?? '';
  }

  it('opens a dialog that documents the open API endpoint and scope', () => {
    renderPage();
    openApiDialog();

    expect(screen.getByText('API 接入 · 决策助手')).toBeDefined();
    expect(screen.getAllByText('decisions:run').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\/api\/v1\/decisions\/predict/).length).toBeGreaterThan(0);
    // 完整接口文档入口已下线，弹窗里不再暴露 Swagger 链接
    expect(screen.queryByText(/接口文档/)).toBeNull();

    const code = sampleCode();
    expect(code).toContain('curl -X POST');
    expect(code).toContain('Authorization: Bearer sd_live_REPLACE_WITH_YOUR_KEY');
    expect(code).toContain('"questions"');
  });

  it('takes the endpoint host from the server config instead of the browser origin', async () => {
    renderPage();
    openApiDialog();

    await waitFor(() => expect(sampleCode()).toContain(CONFIGURED_ENDPOINT));
    expect(sampleCode()).not.toContain('localhost');
    expect(screen.getAllByText(CONFIGURED_ENDPOINT).length).toBeGreaterThan(0);
  });

  it('falls back to the current origin when the access config cannot be loaded', async () => {
    fetchLayaApiAccess.mockRejectedValue(new Error('offline'));

    renderPage();
    openApiDialog();

    await waitFor(() =>
      expect(sampleCode()).toContain(`${window.location.origin}/api/v1/decisions/predict`),
    );
  });

  it('builds the sample body from the current form once it is complete', () => {
    renderPage();
    fillBackground('客户要求退款并威胁投诉');
    setValue(NOUL_PLACEHOLDER, '用户是否明确要求退款？');
    openApiDialog();

    const code = sampleCode();
    expect(code).toContain('客户要求退款并威胁投诉');
    expect(code).toContain('用户是否明确要求退款？');
  });

  it('falls back to the official sample while the form is incomplete', () => {
    renderPage();
    openApiDialog();

    expect(
      screen.getByText('当前表单未填写完整，示例先用官方工单场景；填好后可切回「当前表单」。'),
    ).toBeDefined();
    // 未填写的表单不会是示例来源：背景里没有用户输入
    expect(sampleCode()).not.toContain('客户要求退款');
  });

  it('switches languages and copies the shown sample', async () => {
    // Radix 的 TabsTrigger 由 mouseDown 驱动切换，fireEvent.click 不会派发该事件
    const user = userEvent.setup();
    copyTextToClipboard.mockReset();
    copyTextToClipboard.mockResolvedValue(undefined);
    renderPage();
    openApiDialog();

    await user.click(screen.getByText('Python'));
    await waitFor(() => expect(sampleCode('python')).toContain('import requests'));
    expect(sampleCode('python')).toContain('requests.post');
    expect(sampleCode('python')).not.toContain('curl -X POST');

    await user.click(screen.getByText('复制'));
    await waitFor(() => expect(copyTextToClipboard).toHaveBeenCalledTimes(1));
    expect(copyTextToClipboard.mock.calls[0][0]).toContain('import requests');
  });
});

describe('DecisionAssistantPage · 导入 JSON', () => {
  const IMPORT_SAMPLE = JSON.stringify({
    state: { background: '客户要求退款并威胁投诉' },
    questions: {
      refund_requested: { type: 'noul', instructions: '用户是否明确要求退款？' },
      department: {
        type: 'choice',
        instructions: '这条工单应由哪个部门处理？',
        criteria: { billing: '账单、扣款', technical: 'Bug、报错' },
      },
    },
  });

  function fileInput(): HTMLInputElement {
    // 页面头部（头像上传）也有一个 file input，必须按 testid 精确取
    return document.querySelector('[data-testid="decision-import-input"]') as HTMLInputElement;
  }

  /** jsdom 的 input.files 只读，必须先 defineProperty 再派发 change（userEvent 对 hidden input 不派发）。 */
  function uploadJson(content: string, name = 'decision.json') {
    const input = fileInput();
    const file = new File([content], name, { type: 'application/json' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    fireEvent.change(input);
  }

  it('fills the form from an imported json file', async () => {
    renderPage();

    uploadJson(IMPORT_SAMPLE);

    await waitFor(() =>
      expect((screen.getByPlaceholderText(BACKGROUND_PLACEHOLDER) as HTMLTextAreaElement).value).toBe(
        '客户要求退款并威胁投诉',
      ),
    );
    expect(screen.getAllByText('是非').length).toBeGreaterThan(0);
    expect(screen.getAllByText('选择 · 分类命中').length).toBeGreaterThan(0);
    expect(screen.getAllByPlaceholderText(NOUL_PLACEHOLDER)).toHaveLength(1);
    expect(values(OPTION_PLACEHOLDER)).toEqual(['billing', 'technical']);
    expect(notifySuccess).toHaveBeenCalledWith('已导入 2 条决策内容');
    expect(notifyError).not.toHaveBeenCalled();
  });

  it('keeps the form untouched and reports why an import failed', async () => {
    renderPage();
    fillBackground('原有背景');
    setValue(NOUL_PLACEHOLDER, '原有问题');

    uploadJson('{ not json', 'broken.json');

    await waitFor(() => expect(notifyError).toHaveBeenCalledWith('JSON 解析失败，请检查文件内容是否为合法 JSON。'));
    expect((screen.getByPlaceholderText(BACKGROUND_PLACEHOLDER) as HTMLTextAreaElement).value).toBe('原有背景');
    expect(values(NOUL_PLACEHOLDER)).toEqual(['原有问题']);
  });

  it('explains the importable json format from the tips button', async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(screen.getByLabelText('查看可导入的 JSON 格式'));
    await waitFor(() => expect(screen.getByText('可导入的 JSON 格式')).toBeDefined());

    const sample = Array.from(document.querySelectorAll('pre')).map((node) => node.textContent ?? '').join('\n');
    expect(sample).toContain('"questions"');
    expect(sample).toContain('"noul"');
    expect(sample).toContain('"choice"');
  });
});
