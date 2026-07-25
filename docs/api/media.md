# `/media` — token 化媒体访问

返回 HLS 段与 init 段的二进制内容。**不暴露物理路径**——URL 由 [traceback](traceback.md) 端点签发的 HMAC token 定位文件。通用约定见 [README](README.md)。

**Token**：HMAC-SHA256 签名的不透明字符串（`payload_b64.signature_b64`），consumer 无需解析，整串放进 URL 即可。默认 TTL **300s**（`media_token_ttl` 可配）；secret 取自 `settings.media_token_secret`（未配置则进程级临时密钥，重启失效）。token 由服务端 `MediaToken.sign(task_id, step_id, filename, kind)` 签发，`kind` 区分 `segment` / `init`。

**Path-traversal 防护**：token 内文件名不含 `/` `\`；解析后路径经 `Path.resolve()` 必须落在存储根内，且须为常规文件。

`/media` 在 Gateway 走放宽策略（绕过限流/反扫描，仍查 IP 白名单）。

---

## GET /media/segment/{token}

**路径**：`token`（`kind=segment`）。

**200**：`Content-Type: video/mp4`，二进制 fMP4 fragment。头 `Cache-Control: private, max-age=60`、`Content-Disposition: inline`。

**错误**：`400`（文件名非法 / token `kind` 非 segment）、`403`（token 无效或过期）、`404`（文件不存在或非常规文件）。

---

## GET /media/init/{token}

**路径**：`token`（`kind=init`，文件名固定 `init.mp4`）。

**200**：`Content-Type: video/mp4`，二进制 fMP4 init 段（step 级共享）。头 `Cache-Control: private, max-age=3600`、`Content-Disposition: inline`。

**错误**：`400`（token `kind` 非 init）、`403`（无效/过期）、`404`（`init.mp4` 不存在）。

> `/media/keypoints/{token}` 已随 keypoints 死写下线。
