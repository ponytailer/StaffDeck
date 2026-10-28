import { api } from './client';

/**
 * 个人知识库的「API 接入」信息。
 *
 * 个人库对外的检索接口挂在 `/api/v1` 下（账号级 API 密钥），页面只负责展示与复制，
 * 真正的实现在 `backend/app/public_api/resources.py`。
 */
export type KnowledgeApiAccess = {
  required_scope: string;
  endpoint_path: string;
  docs_path: string;
  public_base_url: string;
};

export function fetchKnowledgeApiAccess(): Promise<KnowledgeApiAccess> {
  return api.get<KnowledgeApiAccess>('/api/enterprise/knowledge-bases/api-access');
}