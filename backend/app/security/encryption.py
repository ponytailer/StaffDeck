import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    secret = get_settings().app_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Secret cannot be decrypted with current APP_SECRET") from exc


def try_decrypt_secret(value: str | None) -> str | None:
    """容错版解密：失败（如历史数据用旧 APP_SECRET 加密）返回 None，不抛异常。

    用于只读/展示路径（列表、掩码、用量等），避免单条坏数据拖垮整个接口；
    写路径（保存、更新）仍使用严格版 decrypt_secret 以暴露配置问题。
    """
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}-****{value[-4:]}"

