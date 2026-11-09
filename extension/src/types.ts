/** 与后端 schema 对齐的类型（backend/app/agents/schema.py 等） */

export type UserRead = {
  id: string;
  tenant_id: string;
  username: string;
  display_name: string | null;
  department: string | null;
  role: 'admin' | 'member';
  email: string | null;
  avatar_url: string | null;
};

export type LoginResponse = {
  token: string;
  user: UserRead;
  expires_at: number | null;
};

export type AgentProfileRead = {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  is_overall: boolean;
  status: string;
  kind: string; // standard | dispatcher
  usage_count: number;
  free_model_enabled: boolean;
  free_model_remaining: number | null;
  created_at: string;
  updated_at: string;
};

export type StreamEvent = {
  event: string;
  data: Record<string, unknown>;
};
