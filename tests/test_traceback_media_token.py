"""
MediaToken 单元测试

覆盖：
- 签发 / 校验 happy path（payload 含 task_id + step_id）
- 错误 secret / 错误签名 / kind 不匹配 / 过期 / 格式错误
- 路径穿越类 filename 拒签
- 默认单例：环境变量未设置时使用 ephemeral secret，测试间正确隔离
"""

import time

import pytest

from app.services.traceback.media_token import (
    MediaToken,
    MediaTokenError,
)


class TestMediaTokenSignVerify:
    def setup_method(self):
        self.mt = MediaToken(secret=b"test-secret-key-12345", default_ttl=300)

    def test_sign_and_verify_segment(self):
        token = self.mt.sign(
            task_id=42,
            step_id=1,
            filename="processed_segment_1700000000000000.mp4",
            kind="segment",
        )
        payload = self.mt.verify(token, kind="segment")
        assert payload.task_id == 42
        assert payload.step_id == 1
        assert payload.filename == "processed_segment_1700000000000000.mp4"
        assert payload.kind == "segment"
        assert payload.expiry > int(time.time())

    def test_sign_and_verify_keypoints(self):
        token = self.mt.sign(
            task_id=1, step_id=2, filename="keypoints_123.json", kind="keypoints"
        )
        payload = self.mt.verify(token, kind="keypoints")
        assert payload.kind == "keypoints"
        assert payload.step_id == 2

    def test_kind_mismatch_rejected(self):
        token = self.mt.sign(1, 1, "f.mp4", kind="segment")
        with pytest.raises(MediaTokenError, match="Kind mismatch"):
            self.mt.verify(token, kind="keypoints")

    def test_kind_none_skips_check(self):
        token = self.mt.sign(1, 1, "f.mp4", kind="segment")
        payload = self.mt.verify(token)  # 不限定 kind
        assert payload.kind == "segment"

    def test_wrong_secret_rejected(self):
        token = self.mt.sign(1, 1, "f.mp4", kind="segment")
        attacker = MediaToken(secret=b"wrong-secret", default_ttl=300)
        with pytest.raises(MediaTokenError, match="Signature mismatch"):
            attacker.verify(token)

    def test_tampered_payload_rejected(self):
        token = self.mt.sign(1, 1, "f.mp4", kind="segment")
        # 替换 payload 部分（保留原签名）
        _, sig = token.split(".", 1)
        # 伪造一个不同 task_id 的 payload
        forged_token = self.mt.sign(999, 1, "f.mp4", kind="segment")
        forged_payload = forged_token.split(".", 1)[0]
        bad = f"{forged_payload}.{sig}"
        with pytest.raises(MediaTokenError, match="Signature mismatch"):
            self.mt.verify(bad)

    def test_expired_token_rejected(self):
        # 用很短的 TTL 签发，注入未来时间校验
        token = self.mt.sign(1, 1, "f.mp4", kind="segment", ttl=1, now=1000)
        with pytest.raises(MediaTokenError, match="expired"):
            self.mt.verify(token, now=1002)  # 已过期

    def test_token_valid_at_boundary(self):
        token = self.mt.sign(1, 1, "f.mp4", kind="segment", ttl=10, now=1000)
        # 1009 < expiry(1010) 可用；1010 == expiry 已过期
        assert self.mt.verify(token, now=1009).expiry == 1010
        with pytest.raises(MediaTokenError, match="expired"):
            self.mt.verify(token, now=1010)

    def test_malformed_token_rejected(self):
        for bad in ["", "no-dot", "...", "x" * 10]:
            with pytest.raises(MediaTokenError):
                self.mt.verify(bad)

    def test_garbage_b64_rejected(self):
        with pytest.raises(MediaTokenError):
            self.mt.verify("!!!.!!!")

    def test_filename_with_slash_rejected(self):
        with pytest.raises(ValueError, match="path traversal"):
            self.mt.sign(1, 1, "../etc/passwd", kind="segment")
        with pytest.raises(ValueError, match="path traversal"):
            self.mt.sign(1, 1, "subdir/file.mp4", kind="segment")
        with pytest.raises(ValueError, match="path traversal"):
            self.mt.sign(1, 1, "..", kind="segment")

    def test_invalid_kind_rejected(self):
        with pytest.raises(ValueError, match="Invalid kind"):
            self.mt.sign(1, 1, "f.mp4", kind="badkind")  # type: ignore[arg-type]

    def test_empty_filename_rejected(self):
        with pytest.raises(ValueError, match="filename must be non-empty"):
            self.mt.sign(1, 1, "", kind="segment")

    def test_empty_secret_rejected(self):
        with pytest.raises(ValueError, match="must not be empty"):
            MediaToken(secret=b"", default_ttl=60)

    def test_zero_ttl_rejected(self):
        with pytest.raises(ValueError, match="default_ttl"):
            MediaToken(secret=b"x", default_ttl=0)
        mt = MediaToken(secret=b"x", default_ttl=60)
        with pytest.raises(ValueError, match="ttl"):
            mt.sign(1, 1, "f.mp4", kind="segment", ttl=0)


class TestMediaTokenDefault:
    def setup_method(self):
        MediaToken.reset_default()

    def teardown_method(self):
        MediaToken.reset_default()

    def test_default_singleton(self, monkeypatch):
        # 注入固定 secret
        monkeypatch.setattr(
            "app.settings.settings.media_token_secret", "fixed-secret-from-settings"
        )
        a = MediaToken.default()
        b = MediaToken.default()
        assert a is b
        token = a.sign(1, 1, "f.mp4", kind="segment")
        assert b.verify(token).task_id == 1

    def test_default_falls_back_to_ephemeral_secret(self, monkeypatch):
        monkeypatch.setattr("app.settings.settings.media_token_secret", "")
        mt = MediaToken.default()
        # 随机生成；至少能签发与校验
        token = mt.sign(1, 2, "f.mp4", kind="segment")
        payload = mt.verify(token)
        assert payload.task_id == 1
        assert payload.step_id == 2
