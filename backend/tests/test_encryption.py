"""encryption 模块单测：重点覆盖 APP_SECRET 变更后历史数据解密的容错行为。"""

import base64
import hashlib

from cryptography.fernet import Fernet

from app.security.encryption import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    try_decrypt_secret,
)


def test_roundtrip() -> None:
    plain = "sk-test-123456"
    assert decrypt_secret(encrypt_secret(plain)) == plain
    assert try_decrypt_secret(encrypt_secret(plain)) == plain


def test_empty_value() -> None:
    assert decrypt_secret("") == ""
    assert try_decrypt_secret("") is None
    assert try_decrypt_secret(None) is None


def test_try_decrypt_secret_returns_none_on_garbage() -> None:
    """非 Fernet 格式密文：try 版返回 None，严格版抛 ValueError。"""
    assert try_decrypt_secret("not-a-fernet-token") is None
    try:
        decrypt_secret("not-a-fernet-token")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_try_decrypt_secret_returns_none_on_old_app_secret() -> None:
    """模拟 APP_SECRET 变更：用别的 secret 加密的密文，try 版返回 None 不抛异常。"""
    old_key = base64.urlsafe_b64encode(hashlib.sha256(b"legacy-app-secret").digest())
    stale_cipher = Fernet(old_key).encrypt(b"sk-legacy-xxxx").decode()
    assert try_decrypt_secret(stale_cipher) is None


def test_mask_secret() -> None:
    assert mask_secret("") == ""
    assert mask_secret("1234567") == "****"  # len <= 8 全掩
    assert mask_secret("sk-12345678") == "sk--****5678"  # len > 8 保留首尾
    assert mask_secret("sk-test-123456") == "sk--****3456"
