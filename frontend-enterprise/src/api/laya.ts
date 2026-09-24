import { api } from './client';
import type { LayaPredictResult, LayaQuestionSpec } from '../pages/decision/decisionForm';

/**
 * 决策助手（Laya）的后端代理调用。
 *
 * 不直连上游：目标服务未开 CORS，浏览器 fetch 会被预检拦掉；地址也不应进前端产物。
 */
export type LayaPredictBody = {
  state: { background: string };
  questions: Record<string, LayaQuestionSpec>;
  model?: string;
};

export type LayaHealth = {
  reachable: boolean;
  upstream_url: string;
  status?: string | null;
  message?: string | null;
};

/** 「API 接入」示例所需的接入信息。`public_base_url` 来自后端 PUBLIC_BASE_URL，可能为空。 */
export type LayaApiAccess = {
  required_scope: string;
  endpoint_path: string;
  docs_path: string;
  public_base_url: string;
};

export function predictLayaQuestions(body: LayaPredictBody, signal?: AbortSignal): Promise<LayaPredictResult> {
  return api.postWithSignal<LayaPredictResult>('/api/enterprise/laya/predict', body, signal);
}

export function checkLayaHealth(): Promise<LayaHealth> {
  return api.get<LayaHealth>('/api/enterprise/laya/health');
}

export function fetchLayaApiAccess(): Promise<LayaApiAccess> {
  return api.get<LayaApiAccess>('/api/enterprise/laya/api-access');
}
