"""决策助手（Laya 决策头）的开放 API。

与企业端登录态接口 `POST /api/enterprise/laya/predict` 共用
`app.api.laya.execute_predict`——同一套校验、同一套上游调用，只是换一种认证：
这里用账号级全量密钥（`sd_live_*`，scope `decisions:run`），无需登录会话。

路径经 `main.py` 的 `app.mount("/api/v1", create_public_api_app())` 挂载后，
对外是 `POST /api/v1/decisions/predict`。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.laya import OPEN_API_SCOPE, LayaPredictRequest, execute_predict
from app.public_api.auth import PublicPrincipal, require_scopes

router = APIRouter(tags=["decisions"])


class LayaOpenPredictResponse(BaseModel):
    """开放 API 的响应：与企业端同构，但不回上游内网地址。"""

    answers: dict[str, Any]
    routing: dict[str, Any] | None = None
    elapsed_ms: float


@router.post(
    "/decisions/predict",
    response_model=LayaOpenPredictResponse,
    summary="Run Laya decision questions",
    description=(
        "把一组待判问题（noul / choice / score）连同决策背景交给 Laya 决策头，"
        "返回每个问题的判定结果、概率分布与置信度。"
    ),
)
def predict_decisions(
    request: LayaPredictRequest,
    principal: PublicPrincipal = Depends(require_scopes(OPEN_API_SCOPE)),
) -> LayaOpenPredictResponse:
    # principal 仅用于鉴权与审计（中间件从 request.state 取），请求本身与租户/员工无关。
    del principal
    result = execute_predict(request)
    return LayaOpenPredictResponse(
        answers=result.answers,
        routing=result.routing,
        elapsed_ms=result.elapsed_ms,
    )
