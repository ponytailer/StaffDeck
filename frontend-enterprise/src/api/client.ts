import { getEnterpriseAuthSession } from '../auth';

const resolveApiBase = () => {
  if (import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL;
  }

  return '';
};

const API_BASE = resolveApiBase();

export const TENANT_ID = import.meta.env.VITE_TENANT_ID || 'tenant_demo';
export const SHOW_DEBUG = import.meta.env.VITE_SHOW_DEBUG === 'true';

export class ApiError extends Error {
  status: number;
  body: string;
  code?: string;

  constructor(status: number, body: string, statusText: string) {
    const parsed = parseErrorPayload(body);
    super(parsed.message || statusText || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    this.code = parsed.code;
  }
}

export function isAuthError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
      ...(options.headers || {}),
    },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : {}) as T;
}

async function keepalivePost<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    keepalive: true,
    headers: {
      'Content-Type': 'application/json',
      ...authHeader(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : {}) as T;
}

function authHeader(): Record<string, string> {
  const session = getEnterpriseAuthSession();
  return session?.token ? { Authorization: `Bearer ${session.token}` } : {};
}

/** fetch 拿不到上传进度；大包上传（如 zip 技能包）用 XHR 上报 onProgress。 */
function postXml<T>(
  path: string,
  body: unknown,
  onProgress?: (percent: number) => void,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}${path}`);
    xhr.responseType = 'json';
    const auth = getEnterpriseAuthSession();
    if (auth?.token) xhr.setRequestHeader('Authorization', `Bearer ${auth.token}`);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable && event.total > 0) {
        onProgress(Math.min(99, Math.round((event.loaded / event.total) * 99)));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        // 99 只表示字节发送完，服务端解析完成才算 100
        onProgress?.(100);
        resolve((xhr.response || {}) as T);
        return;
      }
      reject(new ApiError(xhr.status, typeof xhr.response === 'string' ? xhr.response : (xhr.responseText || ''), xhr.statusText || ''));
    };
    xhr.onerror = () => reject(new ApiError(0, '网络错误', ''));
    signal?.addEventListener('abort', () => {
      xhr.abort();
      reject(new DOMException('Aborted', 'AbortError'));
    });
    xhr.send(JSON.stringify(body));
  });
}

export type UploadProgressStage = 'uploading' | 'parsing';

/**
 * multipart 直传文件（带双阶段进度）。
 *
 * 字节上传阶段上报 0-95（真实比例）；字节发完切到 'parsing' 阶段（96-99 停留，
 * 由调用方展示“服务端解析中”文案）；收到响应后 100。
 * 避免大包（如 20MB zip）在服务端解析阶段长时间停在 99% 给人的死锁错觉。
 */
function uploadFileXml<T>(
  path: string,
  file: File,
  queryParams: string,
  onProgress?: (percent: number, stage: UploadProgressStage) => void,
  signal?: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${API_BASE}${path}${queryParams ? `?${queryParams}` : ''}`);
    xhr.responseType = 'json';
    const auth = getEnterpriseAuthSession();
    if (auth?.token) xhr.setRequestHeader('Authorization', `Bearer ${auth.token}`);
    // FormData 自动带 multipart boundary，不要手动设 Content-Type
    xhr.upload.onprogress = (event) => {
      if (!onProgress || !event.lengthComputable || event.total <= 0) return;
      const ratio = event.loaded / event.total;
      if (ratio >= 1) {
        onProgress(96, 'parsing');
      } else {
        onProgress(Math.round(ratio * 95), 'uploading');
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.(100, 'parsing');
        resolve((xhr.response || {}) as T);
        return;
      }
      reject(new ApiError(xhr.status, typeof xhr.response === 'string' ? xhr.response : (xhr.responseText || ''), xhr.statusText || ''));
    };
    xhr.onerror = () => reject(new ApiError(0, '网络错误', ''));
    signal?.addEventListener('abort', () => {
      xhr.abort();
      reject(new DOMException('Aborted', 'AbortError'));
    });
    const form = new FormData();
    form.append('file', file);
    xhr.send(form);
  });
}

export type DownloadProgress = {
  /** 0-100；响应无 Content-Length 时 percent 会停留在 -1，改用 loadedBytes 展示 */
  percent: number;
  receivedBytes: number;
  totalBytes: number;
};

/**
 * 下载大文件（zip 技能包等）用 XHR 上报下载进度：onprogress 里 loaded/total
 * 逐段回调。Content-Length 缺失时 percent 用 -1，调用方用 receivedBytes 兑底。
 */
function downloadBlobXml(
  path: string,
  onProgress?: (progress: DownloadProgress) => void,
  signal?: AbortSignal,
): Promise<Blob> {
  return new Promise<Blob>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('GET', `${API_BASE}${path}`);
    xhr.responseType = 'blob';
    const auth = getEnterpriseAuthSession();
    if (auth?.token) xhr.setRequestHeader('Authorization', `Bearer ${auth.token}`);
    xhr.onprogress = (event) => {
      if (!onProgress) return;
      const total = event.lengthComputable ? event.total : 0;
      onProgress({
        percent: total > 0 ? Math.min(99, Math.round((event.loaded / total) * 99)) : -1,
        receivedBytes: event.loaded,
        totalBytes: total,
      });
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress?.({ percent: 100, receivedBytes: (xhr.response as Blob)?.size || 0, totalBytes: (xhr.response as Blob)?.size || 0 });
        resolve(xhr.response as Blob);
        return;
      }
      const text = typeof xhr.response === 'string' ? xhr.response : (xhr.responseText || '');
      reject(new ApiError(xhr.status, text, xhr.statusText || ''));
    };
    xhr.onerror = () => reject(new ApiError(0, '网络错误', ''));
    signal?.addEventListener('abort', () => {
      xhr.abort();
      reject(new DOMException('Aborted', 'AbortError'));
    });
    xhr.send();
  });
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  postWithSignal: <T>(path: string, body: unknown, signal?: AbortSignal) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body), signal }),
  postWithProgress: <T>(
    path: string,
    body: unknown,
    onProgress?: (percent: number) => void,
    signal?: AbortSignal,
  ) => postXml<T>(path, body, onProgress, signal),
  uploadWithProgress: <T>(
    path: string,
    file: File,
    queryParams: string,
    onProgress?: (percent: number, stage: UploadProgressStage) => void,
    signal?: AbortSignal,
  ) => uploadFileXml<T>(path, file, queryParams, onProgress, signal),
  postKeepalive: <T>(path: string, body?: unknown) => keepalivePost<T>(path, body),
  put: <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
  blob: async (path: string) => {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: {
        ...authHeader(),
      },
    });
    if (!response.ok) {
      const text = await response.text();
      throw new ApiError(response.status, text, response.statusText);
    }
    return response.blob();
  },
  blobWithProgress: (path: string, onProgress?: (progress: DownloadProgress) => void, signal?: AbortSignal) =>
    downloadBlobXml(path, onProgress, signal),
  postBlob: async (path: string, body: unknown) => {
    const response = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeader(),
      },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new ApiError(response.status, text, response.statusText);
    }
    return response.blob();
  },
};

export async function uploadChatAttachments<T>(
  tenantId: string,
  files: File[],
  signal?: AbortSignal,
): Promise<T> {
  const form = new FormData();
  files.forEach((file) => form.append('files', file));
  const response = await fetch(`${API_BASE}/api/chat/attachments?tenant_id=${encodeURIComponent(tenantId)}`, {
    method: 'POST',
    headers: { ...authHeader() },
    body: form,
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<T>;
}

export type ChatAttachmentParseJob = {
  id: string;
  attachment_id: string;
  filename: string;
  status: 'queued' | 'parsing' | 'succeeded' | 'failed';
  stage: string;
  progress: number;
  error?: string | null;
  created_at?: string | null;
  finished_at?: string | null;
};

/** 轮询 PDF 附件云端解析（MinerU）进度。 */
export async function fetchChatAttachmentParseJob(
  tenantId: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<ChatAttachmentParseJob> {
  const response = await fetch(
    `${API_BASE}/api/chat/attachments/parse-jobs/${encodeURIComponent(jobId)}?tenant_id=${encodeURIComponent(tenantId)}`,
    { headers: { ...authHeader() }, signal },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<ChatAttachmentParseJob>;
}

export async function streamChatTurn(
  body: Record<string, unknown>,
  onEvent: (item: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return streamPost('/api/chat/stream', body, onEvent, signal);
}

export type StreamEvent = {
  event: string;
  data: Record<string, unknown>;
};

export async function streamPost(
  path: string,
  body: Record<string, unknown>,
  onEvent: (item: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeader() },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() || '';
    blocks.forEach((block) => {
      const parsed = parseSseBlock(block);
      if (parsed) onEvent(parsed);
    });
  }

  buffer += decoder.decode();
  const parsed = parseSseBlock(buffer);
  if (parsed) onEvent(parsed);
}

export async function streamGet(
  path: string,
  onEvent: (item: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { ...authHeader() }, signal });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() || '';
    blocks.forEach((block) => {
      const parsed = parseSseBlock(block);
      if (parsed) onEvent(parsed);
    });
  }

  buffer += decoder.decode();
  const parsed = parseSseBlock(buffer);
  if (parsed) onEvent(parsed);
}

function parseSseBlock(block: string): StreamEvent | null {
  const lines = block.split('\n').map((line) => line.trimEnd());
  const eventLine = lines.find((line) => line.startsWith('event:'));
  const dataLines = lines.filter((line) => line.startsWith('data:'));
  if (!eventLine || dataLines.length === 0) return null;
  const event = eventLine.replace(/^event:\s*/, '');
  const rawData = dataLines.map((line) => line.replace(/^data:\s*/, '')).join('\n');
  try {
    return { event, data: JSON.parse(rawData) as Record<string, unknown> };
  } catch {
    return { event, data: { raw: rawData } };
  }
}

type ParsedApiError = {
  message: string;
  code?: string;
};

const STABLE_ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_]+$/;

function stableErrorCode(value: unknown): string | undefined {
  return typeof value === 'string' && STABLE_ERROR_CODE_PATTERN.test(value)
    ? value
    : undefined;
}

function parseErrorPayload(text: string): ParsedApiError {
  if (!text) return { message: '' };
  try {
    const payload = JSON.parse(text) as {
      code?: unknown;
      detail?: unknown;
      message?: unknown;
      error?: unknown;
    };
    const detail = payload.detail ?? payload.message ?? payload.error;
    const topLevelCode = stableErrorCode(payload.code);
    if (typeof detail === 'string') {
      return { message: detail, code: topLevelCode ?? stableErrorCode(detail) };
    }
    if (Array.isArray(detail)) {
      return {
        message: detail
          .map(formatValidationDetail)
          .filter(Boolean)
          .join('；'),
        code: topLevelCode,
      };
    }
    if (detail && typeof detail === 'object') {
      const structured = detail as { code?: unknown; message?: unknown; detail?: unknown };
      const message = typeof structured.message === 'string'
        ? structured.message
        : typeof structured.detail === 'string'
          ? structured.detail
          : '';
      const code = stableErrorCode(structured.code) ?? topLevelCode;
      if (message || code) return { message: message || String(code), code };
    }
  } catch {
    return { message: text };
  }
  return { message: text };
}

function formatValidationDetail(item: unknown): string {
  if (typeof item === 'string') return item;
  if (!item || typeof item !== 'object') return '';

  const detail = item as { loc?: unknown; msg?: unknown };
  const message = typeof detail.msg === 'string' ? detail.msg : '';
  const location = Array.isArray(detail.loc)
    ? detail.loc.map((part) => String(part)).filter(Boolean).join('.')
    : '';

  if (location && message) return `${location}: ${message}`;
  return message;
}
