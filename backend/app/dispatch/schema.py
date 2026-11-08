"""调度入口（Dispatcher）的对外契约。

调度入口是一种全新的 Agent 类型（``AgentProfile.kind == "dispatcher"``）：
只做「识别 + 选人」，不配知识库 / 技能 / 工具，是会话的唯一入口。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

DispatchAction = Literal["route", "clarify", "reply"]
FallbackMode = Literal["clarify", "reply", "default_member"]

# 调度入口的成员数量上下限。改这里必须同步前端 MAX_MEMBERS / MIN_MEMBERS
# （docs/dispatcher-config-demo.html）与编辑器里的计数文案。
#
# 下限为什么是 2：只有 1 个成员时「路由」没有任何判断成分（只能是它），
# 调度入口就退化成了一层多余的转发 —— 这种需求直接建一个普通员工更合适。
MAX_DISPATCH_MEMBERS = 3
MIN_DISPATCH_MEMBERS = 2

DEFAULT_FALLBACK_REPLY = (
    "这个问题暂时没有对应的同事能处理，可以换个说法，或者联系管理员帮你安排。"
)
DEFAULT_CLARIFY_TEXT = "你想咨询的是哪方面的问题？我这边可以转给对应的同事。"


class DispatcherMemberInput(BaseModel):
    """一个成员的配置：绑谁 + 什么情况转给他。"""

    member_agent_id: str
    # 路由准确率的全部来源。支持多行；「不要转给他 / 转给小李」这类负向约束最有效。
    routing_rule: str = ""
    example_queries: list[str] = Field(default_factory=list)
    sort_order: int = 0


class DispatcherMemberRead(BaseModel):
    member_agent_id: str
    routing_rule: str = ""
    example_queries: list[str] = Field(default_factory=list)
    sort_order: int = 0
    status: str = "active"
    # 冗余的展示字段，省前端再查一次花名册
    agent_name: str = ""
    agent_description: Optional[str] = None
    agent_status: str = "active"
    free_model_enabled: bool = False
    # 该成员是否配了自己的模型（决定子 turn 用谁的模型）
    has_own_model: bool = False


class DispatchConfig(BaseModel):
    """无法路由时的兜底策略。存在 ``AgentProfile.metadata_json["dispatch_config"]``。"""

    fallback_mode: FallbackMode = "clarify"
    fallback_reply: str = DEFAULT_FALLBACK_REPLY
    default_member_agent_id: Optional[str] = None
    offer_candidates: bool = True
    # 低于这个置信度就按 fallback 处理，绝不硬猜
    min_confidence: float = 0.5


class DispatchRouteDecision(BaseModel):
    """路由模型的输出（也是落 ``dispatch_decision`` 事件的契约）。"""

    action: DispatchAction = "clarify"
    member_agent_id: Optional[str] = None
    confidence: float = 0.0
    # 一句话意图，写进转接记录，供下一轮路由参考
    intent: Optional[str] = None
    # 改写后能独立成立的完整问题（成员看不到父会话历史，指代必须消解）
    query: Optional[str] = None
    reason: Optional[str] = None
    # action=reply 时的固定话术
    reply: Optional[str] = None
    # 单成员 / 无成员短路，不消耗 LLM
    fast_path: bool = False


class DispatcherCreateRequest(BaseModel):
    tenant_id: str
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    # 必填：没有模型连建都建不出来（免费额度链路的第一道闸，见方案 §3）
    model_config_id: Optional[str] = None
    members: list[DispatcherMemberInput] = Field(default_factory=list)
    config: DispatchConfig = Field(default_factory=DispatchConfig)
    # 小队头像预设（前端 DISPATCHER_AVATAR_PRESETS 的 key）。不传 = 用默认图形。
    avatar_preset: Optional[str] = None


class DispatcherUpdateRequest(BaseModel):
    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    # 更新时不传 = 沿用原绑定；显式传 null 会被拒绝（不允许把模型摘掉）
    model_config_id: Optional[str] = None
    members: Optional[list[DispatcherMemberInput]] = None
    config: Optional[DispatchConfig] = None
    # 不传 = 沿用；传空串 = 清掉、回落默认图形。
    avatar_preset: Optional[str] = None


class DispatchPreviewRequest(BaseModel):
    """配置页「试跑规则」：还没保存的草稿也能验。

    成员只传 id + 规则，不落库；``model_config_id`` 为空时用租户默认模型。
    """

    tenant_id: str
    message: str
    members: list[DispatcherMemberInput] = Field(default_factory=list)
    config: DispatchConfig = Field(default_factory=DispatchConfig)
    model_config_id: Optional[str] = None
    # 编辑已有入口时传，仅用于把「已删除成员」的占位排除掉
    dispatcher_id: Optional[str] = None


class DispatchPreviewResponse(BaseModel):
    decision: DispatchRouteDecision
    # 命中的成员展示信息（decision.member_agent_id 对前端不友好）
    member_name: Optional[str] = None
    member_description: Optional[str] = None
    # 各成员的简短说明，clarify 时前端要列候选
    candidates: list[dict[str, str]] = Field(default_factory=list)


class DispatcherRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    kind: str = "dispatcher"
    status: str = "active"
    model_config_id: Optional[str] = None
    model_name: Optional[str] = None
    members: list[DispatcherMemberRead] = Field(default_factory=list)
    config: DispatchConfig = Field(default_factory=DispatchConfig)
    usage_count: int = 0
    # 是否已发布到开放广场的「员工小队」。发布/下架只改 metadata，不动成员与模型。
    published_to_gallery: bool = False
    # 小队头像预设；缺省时前端用默认图形。
    avatar_preset: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None