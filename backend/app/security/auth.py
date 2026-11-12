from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.db.models import User, utc_now
from app.object_cache import cached_model, invalidate_key, store_model


TOKEN_TTL_SECONDS = 60 * 60 * 24 * 14

# 登录页「记住账号」勾选后的长效凭证：30 天内免登。
# 免登窗口 = token 有效期，服务端是唯一权威（前端只负责按 expires_at 提前回登录页），
# 所以时长只在这里改一处。
REMEMBER_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30

security = HTTPBearer(auto_error=False)

# 用户对象 Redis 缓存 TTL（秒）：写路径（改资料/角色/密码/删除/LDAP 同步）
# 必须调用 invalidate_user_cache 主动失效，否则最长该窗口内读到旧值
USER_CACHE_TTL_SECONDS = 30

# 「最近更新」= 最近活跃时间：用户带着有效 token 访问任意接口就刷新 users.updated_at。
# 30 分钟节流：命中窗口零写库零缓存失效，高频请求不会因此变慢；
# 真正的资料编辑（PUT /users/{id} 等）仍无条件覆盖 updated_at，口径一致。
USER_ACTIVITY_TOUCH_INTERVAL_SECONDS = 30 * 60


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode('utf-8')}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        _algo, salt, _digest = stored_hash.split("$", 2)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    candidate = f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode('utf-8')}"
    return hmac.compare_digest(candidate, stored_hash)


def create_access_token(user: User, *, ttl_seconds: int | None = None) -> str:
    """签发站内登录凭证。ttl_seconds 缺省走默认 14 天（既有行为不变）。"""
    return _encode_token(
        {
            "tenant_id": user.tenant_id,
            "user_id": user.id,
            "username": user.username,
            "exp": int(time.time()) + (TOKEN_TTL_SECONDS if ttl_seconds is None else ttl_seconds),
        }
    )


def access_token_ttl_seconds(*, remember: bool = False) -> int:
    """登录凭证有效期：「记住账号」= 30 天，否则默认 14 天。"""
    return REMEMBER_TOKEN_TTL_SECONDS if remember else TOKEN_TTL_SECONDS


def create_login_token(user: User, *, remember: bool = False) -> tuple[str, int]:
    """签发登录凭证，返回 (token, 绝对过期时间戳)。

    过期时间一并回给前端：前端据此在窗口内直接免登、过期后无需先发一次注定 401
    的 /api/auth/me 就能回到登录页。
    """
    ttl_seconds = access_token_ttl_seconds(remember=remember)
    return create_access_token(user, ttl_seconds=ttl_seconds), int(time.time()) + ttl_seconds


# 分享访客 token 的最长存活时间:即使分享本身永久有效,单次浏览器会话也不会拿到超长凭证
SHARE_TOKEN_MAX_TTL_SECONDS = 60 * 60 * 12


@dataclass(frozen=True)
class ShareScope:
    """分享链接访客的受限身份上下文(从 access token 的 claim 解出)。"""

    share_id: str
    share_token: str
    tenant_id: str
    agent_id: str
    model_config_id: str
    visitor_id: str


def create_share_access_token(
    visitor: User,
    *,
    share_id: str,
    share_token: str,
    agent_id: str,
    model_config_id: str,
    ttl_seconds: int | None = None,
) -> str:
    """签发分享访客 token:身份仍是普通 User 行(会话隔离靠它),但带 `share` claim。

    带该 claim 的请求会被主应用的中间件限制在分享白名单路径内。
    """
    ttl = SHARE_TOKEN_MAX_TTL_SECONDS if ttl_seconds is None else max(60, int(ttl_seconds))
    ttl = min(ttl, SHARE_TOKEN_MAX_TTL_SECONDS)
    return _encode_token(
        {
            "tenant_id": visitor.tenant_id,
            "user_id": visitor.id,
            "username": visitor.username,
            "exp": int(time.time()) + ttl,
            "share": 1,
            "share_id": share_id,
            "share_token": share_token,
            "share_agent_id": agent_id,
            "share_model_config_id": model_config_id,
        }
    )


def share_scope_from_payload(payload: dict[str, Any]) -> ShareScope | None:
    if not payload.get("share"):
        return None
    share_id = str(payload.get("share_id") or "")
    visitor_id = str(payload.get("user_id") or "")
    if not share_id or not visitor_id:
        return None
    return ShareScope(
        share_id=share_id,
        share_token=str(payload.get("share_token") or ""),
        tenant_id=str(payload.get("tenant_id") or ""),
        agent_id=str(payload.get("share_agent_id") or ""),
        model_config_id=str(payload.get("share_model_config_id") or ""),
        visitor_id=visitor_id,
    )


def read_token_payload(token: str | None) -> dict[str, Any] | None:
    """宽松解析 token payload:签名/过期异常一律返回 None(调用方按匿名处理)。"""
    if not token:
        return None
    try:
        return _decode_token(token)
    except HTTPException:
        return None


def get_share_scope(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> ShareScope | None:
    """当前请求是否来自分享访客令牌:站内登录令牌一律返回 None。"""
    payload = read_token_payload(credentials.credentials if credentials else None)
    if payload is None:
        return None
    return share_scope_from_payload(payload)


def _encode_token(payload: dict[str, Any]) -> str:
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body)
    return f"{body}.{signature}"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_session),
) -> User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = _decode_token(credentials.credentials)
    user_id = str(payload.get("user_id", ""))
    # 每请求一次 db.get(User) 是全站最高频查询：Redis 可用时走 30s 缓存，
    # 未命中/不可用回退 DB（行为不变）；写路径负责主动失效
    cache_key = f"auth:user:{user_id}"
    # password_hash 不落 Redis：缓存重建时以空串占位
    user = cached_model(
        cache_key, User, ttl_seconds=USER_CACHE_TTL_SECONDS,
        hydrate={"password_hash": ""},
    )
    if user is None:
        user = db.get(User, user_id)
        if user:
            store_model(
                cache_key, user, ttl_seconds=USER_CACHE_TTL_SECONDS,
                exclude_fields={"password_hash"},
            )
    if not user or user.tenant_id != payload.get("tenant_id"):
        raise HTTPException(status_code=401, detail="Invalid user token")
    # 活跃打点：绝不影响主流程，失败时回滚并放行（统计口径而已，不值得让请求 500）
    try:
        _touch_user_activity(db, user)
    except Exception:  # noqa: BLE001 —— 打点失败不能挡业务
        db.rollback()
    return user


def _touch_user_activity(db: Session, user: User) -> None:
    """把 users.updated_at 当「最近活跃」刷新（30 分钟节流）。

    节流判定先用认证缓存对象里的 updated_at（TTL 只有 30s，足够准），
    命中窗口直接 return —— 大多数请求在此零成本通过。
    注意：缓存对象是 detached 的，不能 db.add（会被当 INSERT 触发主键冲突），
    必须直读 DB 再改；写前对 DB 值二次确认，多进程部署时也不会重复刷。
    """
    now = utc_now()
    last = user.updated_at
    if last is not None and (now - last).total_seconds() < USER_ACTIVITY_TOUCH_INTERVAL_SECONDS:
        return
    db_user = db.get(User, user.id)
    if not db_user:
        return
    db_last = db_user.updated_at
    if db_last is not None and (now - db_last).total_seconds() < USER_ACTIVITY_TOUCH_INTERVAL_SECONDS:
        return
    db_user.updated_at = now
    db.add(db_user)
    db.commit()
    invalidate_user_cache(db_user.id)


def invalidate_user_cache(user_id: str | None) -> None:
    """用户资料/角色/密码变更或删除后失效其缓存（跨进程生效）。"""
    if user_id:
        invalidate_key(f"auth:user:{user_id}")


def ensure_current_user_tenant(tenant_id: str, current_user: User) -> None:
    if not isinstance(current_user, User):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def require_current_tenant(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> User:
    ensure_current_user_tenant(tenant_id, current_user)
    return current_user


def _decode_token(token: str) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if not hmac.compare_digest(_sign(body), signature):
        raise HTTPException(status_code=401, detail="Invalid token signature")
    try:
        payload = json.loads(base64.urlsafe_b64decode(_pad_b64(body)).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token payload") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(status_code=401, detail="Token expired")
    return payload


def _sign(body: str) -> str:
    secret = get_settings().app_secret.encode("utf-8")
    return _b64(hmac.new(secret, body.encode("utf-8"), hashlib.sha256).digest())


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _pad_b64(value: str) -> bytes:
    return (value + "=" * (-len(value) % 4)).encode("utf-8")
