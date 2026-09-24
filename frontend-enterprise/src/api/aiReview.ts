import { api, TENANT_ID } from './client';

/**
 * AI Reviewer（代码评审）的后端调用。
 *
 * 异步模型：POST /tasks 只是入队（rq），列表轮询状态，详情拿行级评论。
 * 凭证接口永远只回掩码（token_set / token_last4），前端拿不到全量 token。
 */

export type AiReviewPlatform = 'github' | 'gitlab';

export type AiReviewCredentialSummary = {
  platform: AiReviewPlatform;
  base_url: string;
  token_set: boolean;
  token_last4: string;
};

export type AiReviewWorkspace = {
  id: string;
  name: string;
  platform: AiReviewPlatform;
  repo_url: string;
  repo_path: string;
  default_branch: string;
  created_at: string | null;
  created_by: string;
  task_count?: number;
};

export type AiReviewMergeRequest = {
  number: number;
  title: string;
  description: string;
  source_branch: string;
  target_branch: string;
  author: string;
  web_url: string;
  updated_at: string;
};

export type AiReviewPreset = {
  id: string;
  name: string;
  content: string;
  is_default: boolean;
  created_at: string | null;
  updated_at: string | null;
};

export type AiReviewTaskStatus = 'queued' | 'running' | 'succeeded' | 'failed';

export type AiReviewTaskSummary = {
  id: string;
  workspace_id: string;
  workspace_name: string;
  status: AiReviewTaskStatus;
  mr_number: number;
  mr_title: string;
  source_branch: string;
  target_branch: string;
  author: string;
  web_url: string;
  requirements: string;
  error: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  platform_synced_at: string | null;
  platform_sync_url: string;
  has_result: boolean;
};

export type AiReviewComment = {
  path?: string;
  content?: string;
  start_line?: number | null;
  end_line?: number | null;
  existing_code?: string | null;
  suggestion_code?: string | null;
  thinking?: string | null;
};

export type AiReviewTaskDetail = AiReviewTaskSummary & {
  mr_description: string;
  result_json: AiReviewComment[];
  summary_json: {
    ocr_status?: string;
    model?: string;
    session_id?: string;
    files_reviewed?: number;
    comments?: number;
    total_tokens?: number;
    elapsed?: number | string;
    [key: string]: unknown;
  };
};

export function fetchAiReviewCredentials(): Promise<AiReviewCredentialSummary[]> {
  return api.get<AiReviewCredentialSummary[]>(`/api/enterprise/ai-review/credentials?tenant_id=${TENANT_ID}`);
}

export function saveAiReviewCredential(
  platform: AiReviewPlatform,
  token: string,
  baseUrl = '',
): Promise<AiReviewCredentialSummary> {
  return api.put<AiReviewCredentialSummary>('/api/enterprise/ai-review/credentials', {
    tenant_id: TENANT_ID,
    platform,
    token,
    base_url: baseUrl,
  });
}

export function fetchAiReviewWorkspaces(): Promise<AiReviewWorkspace[]> {
  return api.get<AiReviewWorkspace[]>(`/api/enterprise/ai-review/workspaces?tenant_id=${TENANT_ID}`);
}

export function createAiReviewWorkspace(body: {
  name: string;
  platform: AiReviewPlatform;
  repo_url: string;
  default_branch: string;
}): Promise<AiReviewWorkspace> {
  return api.post<AiReviewWorkspace>('/api/enterprise/ai-review/workspaces', {
    tenant_id: TENANT_ID,
    ...body,
  });
}

export function deleteAiReviewWorkspace(workspaceId: string): Promise<{ deleted: boolean }> {
  return api.delete<{ deleted: boolean }>(
    `/api/enterprise/ai-review/workspaces/${workspaceId}?tenant_id=${TENANT_ID}`,
  );
}

/** PR/MR 列表：服务端分页 + 搜索；total 为 null 表示仓库 open 量超过拉取上限 */
export type AiReviewMergeRequestPage = {
  items: AiReviewMergeRequest[];
  total: number | null;
  page: number;
  page_size: number;
  has_more: boolean;
};

export type MrListQuery = { page: number; pageSize: number; search: string };

export function fetchAiReviewMergeRequests(
  workspaceId: string,
  query: MrListQuery = { page: 1, pageSize: 20, search: '' },
): Promise<AiReviewMergeRequestPage> {
  const searchPart = query.search.trim()
    ? `&search=${encodeURIComponent(query.search.trim())}`
    : '';
  return api.get<AiReviewMergeRequestPage>(
    `/api/enterprise/ai-review/workspaces/${workspaceId}/merge-requests?tenant_id=${TENANT_ID}&page=${query.page}&page_size=${query.pageSize}${searchPart}`,
  );
}

export function fetchAiReviewPresets(): Promise<AiReviewPreset[]> {
  return api.get<AiReviewPreset[]>(`/api/enterprise/ai-review/presets?tenant_id=${TENANT_ID}`);
}

export function createAiReviewPreset(body: { name: string; content: string; is_default: boolean }): Promise<AiReviewPreset> {
  return api.post<AiReviewPreset>('/api/enterprise/ai-review/presets', { tenant_id: TENANT_ID, ...body });
}

export function updateAiReviewPreset(
  presetId: string,
  body: { name?: string; content?: string; is_default?: boolean },
): Promise<AiReviewPreset> {
  return api.put<AiReviewPreset>(`/api/enterprise/ai-review/presets/${presetId}?tenant_id=${TENANT_ID}`, body);
}

export function deleteAiReviewPreset(presetId: string): Promise<{ deleted: boolean }> {
  return api.delete<{ deleted: boolean }>(
    `/api/enterprise/ai-review/presets/${presetId}?tenant_id=${TENANT_ID}`,
  );
}

/** 自定义评审规则（ocr rule.json）：include / exclude / rules[{path, rule, merge_system_rule}] */
export type AiReviewRuleFileConfig = {
  include?: string[];
  exclude?: string[];
  rules?: { path: string; rule: string; merge_system_rule?: boolean }[];
};

export type AiReviewRuleFileState = {
  exists: boolean;
  name: string;
  content: AiReviewRuleFileConfig | null;
  updated_at?: string | null;
  sample?: AiReviewRuleFileConfig;
};

export function fetchAiReviewRuleFile(): Promise<AiReviewRuleFileState> {
  return api.get<AiReviewRuleFileState>(`/api/enterprise/ai-review/rules-file?tenant_id=${TENANT_ID}`);
}

export function saveAiReviewRuleFile(
  name: string,
  content: string,
): Promise<AiReviewRuleFileState> {
  return api.put<AiReviewRuleFileState>('/api/enterprise/ai-review/rules-file', {
    tenant_id: TENANT_ID,
    name,
    content,
  });
}

export function deleteAiReviewRuleFile(): Promise<{ deleted: boolean }> {
  return api.delete<{ deleted: boolean }>(
    `/api/enterprise/ai-review/rules-file?tenant_id=${TENANT_ID}`,
  );
}

export type AiReviewTaskPage = {
  items: AiReviewTaskSummary[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
};

export type TaskListQuery = { page: number; pageSize: number; search: string };

export function fetchAiReviewTasks(
  workspaceId?: string,
  query: TaskListQuery = { page: 1, pageSize: 20, search: '' },
): Promise<AiReviewTaskPage> {
  const workspacePart = workspaceId ? `&workspace_id=${encodeURIComponent(workspaceId)}` : '';
  const searchPart = query.search.trim()
    ? `&search=${encodeURIComponent(query.search.trim())}`
    : '';
  return api.get<AiReviewTaskPage>(
    `/api/enterprise/ai-review/tasks?tenant_id=${TENANT_ID}&page=${query.page}&page_size=${query.pageSize}${searchPart}${workspacePart}`,
  );
}

export function fetchAiReviewTaskDetail(taskId: string): Promise<AiReviewTaskDetail> {
  return api.get<AiReviewTaskDetail>(
    `/api/enterprise/ai-review/tasks/${taskId}?tenant_id=${TENANT_ID}`,
  );
}

export function createAiReviewTask(body: {
  workspace_id: string;
  mr_number: number;
  requirements: string;
  preset_ids: string[];
}): Promise<AiReviewTaskSummary & { scheduled: boolean }> {
  return api.post<AiReviewTaskSummary & { scheduled: boolean }>(
    '/api/enterprise/ai-review/tasks',
    { tenant_id: TENANT_ID, ...body },
  );
}

export function retryAiReviewTask(taskId: string): Promise<AiReviewTaskSummary & { scheduled: boolean }> {
  return api.post<AiReviewTaskSummary & { scheduled: boolean }>(
    `/api/enterprise/ai-review/tasks/${taskId}/retry?tenant_id=${TENANT_ID}`,
  );
}

export function deleteAiReviewTask(taskId: string): Promise<{ deleted: boolean }> {
  return api.delete<{ deleted: boolean }>(
    `/api/enterprise/ai-review/tasks/${taskId}?tenant_id=${TENANT_ID}`,
  );
}

/** 把已完成的评审结果回写到 PR/MR 评论区（GitHub issue comment / GitLab note） */
export function syncAiReviewTaskToPlatform(
  taskId: string,
): Promise<{ synced: boolean; platform_synced_at: string | null; platform_sync_url: string }> {
  return api.post<{ synced: boolean; platform_synced_at: string | null; platform_sync_url: string }>(
    `/api/enterprise/ai-review/tasks/${taskId}/sync-to-platform`,
    { tenant_id: TENANT_ID },
  );
}
