"""routers 层通用能力：≥2 个 router 共用、不属于任何一个 router 的 HTTP 侧工具。

- `runs`：读侧入口处的 run 解析（`resolve_run` / `no_run` / `resolve_timeline` / `resolve_media_run`）
- `media_token`：`/media/*` 的 HMAC 短 TTL token 签发与校验（`MediaToken`）

零 re-export，调用方一律走深路径（`from .utils.runs import resolve_run`）。
"""
