import { ApiError, authHeader, API_BASE_URL } from './client';

/**
 * AI 文档审阅（Agent 广场 `doc-review` 能力）的前端 API。
 *
 * blocks 的唯一事实来源在**前端**：应用/撤销修改都发生在本地状态里，
 * 导出时随请求带回，服务端在原文档对象上做段落级文本替换（保留原格式）。
 * review / chat / export 直接用 client.ts 的 api.post / api.postBlob。
 */

export type DocBlock = {
  id: string;
  text: string;
  kind: 'heading' | 'paragraph' | 'list';
  level: number;
  in_table: boolean;
  /** 修订追踪模式下，AI 修改以 Word 批注形式附在本段（不改 text）；空/缺省 = 无批注。 */
  comment?: string;
};

export type DocIssue = {
  id: string;
  type: string;
  block_ids: string[];
  title: string;
  detail: string;
  /** [{block_id, new_text}] —— 可以落成文字修改的建议；为空表示只有说明。 */
  fixes: Array<{ block_id: string; new_text: string }>;
};

export type DocParseResult = {
  doc_id: string;
  doc_name: string;
  title: string;
  blocks: DocBlock[];
};

export type DocReviewResult = {
  issues: DocIssue[];
  reviewed_blocks: number;
  notes: string[];
};

export type DocAction = { block_id: string; new_text: string; reason: string };

export type DocChatResult = {
  reply: string;
  actions: DocAction[];
};

export type DocChatMessage = { role: 'user' | 'assistant'; content: string };

/** 上传 .docx 并解析。multipart 走 fetch（api.post 系列都是 JSON body，覆盖不了 FormData）。 */
export async function parseDocx(
  tenantId: string,
  file: File,
  signal?: AbortSignal,
): Promise<DocParseResult> {
  const form = new FormData();
  form.append('tenant_id', tenantId);
  form.append('file', file);
  const response = await fetch(`${API_BASE_URL}/api/enterprise/agent-apps/doc:parse`, {
    method: 'POST',
    headers: { ...authHeader() },
    body: form,
    signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ApiError(response.status, text, response.statusText);
  }
  return response.json() as Promise<DocParseResult>;
}
