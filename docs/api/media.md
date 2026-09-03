# `/media` — token 化媒体访问

前端播放 HLS 时用它拉**段文件（.mp4 fragment）**和**init 段（`{track}_init.mp4`）**的二进制内容。资源来自**磁盘**（`storage_base_dir` 下的 HLS 落盘），URL **不暴露物理路径**——每个文件由一个 HMAC token 定位，token 由 [traceback](traceback.md) 的 playlist 端点签发。通用约定见 [README](README.md)。

前端不直接构造这两个 URL：它们已内嵌在 traceback 签发的 playlist（`.m3u8`）里，播放器按 playlist 逐段请求即可。本文档面向"需要理解 token 失效行为、排查 403/404"的场景。

```
  traceback /playlist ──→ m3u8（内含 segment/init 的 token URL）──→ 播放器逐段 GET /media/*
```

**Token**：HMAC-SHA256 签名的不透明字符串，形如 `payload_b64.signature_b64`（URL-safe base64，无 padding）。consumer **无需解析**，整串放进 path 即可。payload 编码 `task_id / step_id / filename / kind / expiry`；`kind` 区分 `segment` / `init`，服务端校验时会核对 `kind` 必须与端点匹配（segment token 不能当 init 用，反之亦然）。

- **TTL**：默认 **300 秒**（`media_token_ttl` 可配，单位**秒**）。签发时写入绝对过期时间 `expiry`（epoch 秒），校验时 `expiry <= now` 即过期。
- **secret**：取自 `settings.media_token_secret`；**未配置**则进程启动时生成随机临时密钥并告警一次——此时**服务重启会让所有已签发 token 立即失效**（属预期行为，生产须配 `CLEANSIGHT_MEDIA_TOKEN_SECRET`）。
- 服务端**无 token 黑名单**：一旦签发，在 TTL 内且 secret 未变就一直有效，无法主动吊销。

**Path-traversal 防护**（双层）：
1. token 内 `filename` 签发/校验时都禁止含 `/` `\` 及 `.` / `..`；
2. 路径拼出后经 `Path.resolve()`，必须仍落在存储根 `base_dir` 内，否则拒绝。
两层都命中返回 **400**（不是 404，见错误表）。

`/media` 在 Gateway 走**绕过档**（完全跳过限流与反扫描，仍查 IP 白名单与封禁）——段请求自带 HMAC token，验不过即 403，无可枚举面。见 [README › Gateway](README.md)。

---

## GET /media/segment/{token}

拉单个 HLS 媒体段（fMP4 fragment）的二进制内容。播放器按 playlist 逐段请求，前端一般不手工调用。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `token` | string | 是 | segment token（`kind=segment`）。token 内 `filename` 须以 `.mp4` 结尾 |

### 响应 `200`

二进制流，`Content-Type: video/mp4`（fMP4 fragment，**非**完整 MP4，须配合同 step 的 init 段解码）。

响应头：

| 头 | 值 | 含义 |
|----|-----|------|
| `Cache-Control` | `private, max-age=60` | 仅本客户端可缓存，缓存 **60 秒** |
| `Content-Disposition` | `inline` | 浏览器内联播放，不触发下载 |

段文件按 CRF23 落盘，内容不可变；`max-age=60` 偏短是因为**段本身寿命短**（滚动清理），缓存过久没意义。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `400` | token 内 `filename` 不以 `.mp4` 结尾（`kind` 校验通过但指向非段文件） | `{"detail":"Token does not point to a segment"}` |
| `400` | `filename` 含 `/` `\` 或为 `.` / `..` | `{"detail":"Invalid filename"}` |
| `400` | 路径 resolve 后越出存储根（path traversal） | `{"detail":"Invalid path"}` |
| `403` | token 无效 / 签名不符 / 已过期 / `kind` 非 `segment` / 格式错误 | `{"detail":"Invalid or expired token"}` |
| `404` | token 合法但目标文件不存在或非常规文件 | `{"detail":"Media file not found"}` |

> **注意是 403 不是 401**：token 校验失败（含过期、kind 不符）一律 **403**，不区分"未过期但伪造"与"已过期"——body 都是同一句 `Invalid or expired token`，**别依赖 body 文案判分支，只认 status code**。
> **path traversal 是 400 不是 404**：拼路径越界走 400（`Invalid path`），只有"路径合法但文件确实不在磁盘上"才是 404。

---

## GET /media/init/{token}

拉 HLS fMP4 **init 段**（`{track}_init.mp4`，含 moov box / 编码参数）。init **按轨各一份**（`raw_init.mp4` / `processed_init.mp4`），同轨内所有段共享，播放器初始化时按轨拉一次即可。两轨是独立 playlist，不可互指。

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `token` | string | 是 | init token（`kind=init`）。token 内 `filename` 为 `raw_init.mp4` 或 `processed_init.mp4`，由签发方按轨决定 |

### 响应 `200`

二进制流，`Content-Type: video/mp4`（fMP4 init 段）。

响应头：

| 头 | 值 | 含义 |
|----|-----|------|
| `Cache-Control` | `private, max-age=3600` | 仅本客户端可缓存，缓存 **3600 秒（1 小时）** |
| `Content-Disposition` | `inline` | 内联，不触发下载 |

init 段在一个 step 生命周期内不变，故 `max-age` 远大于段（**3600s vs 段的 60s**）：前端只需拉一次并长期缓存，切段不重拉 init。

### 错误

| 状态 | 触发条件 | 响应体形态 |
|------|---------|-----------|
| `400` | token 内 `filename` 不以 `init.mp4` 结尾 | `{"detail":"Token does not point to init segment"}` |
| `403` | token 无效 / 签名不符 / 已过期 / `kind` 非 `init` / 格式错误 | `{"detail":"Invalid or expired token"}` |
| `404` | token 合法但该 init 文件不存在或非常规文件 | `{"detail":"Media file not found"}` |

> 两端点的 403 与 404 body 形态一致（都只有 `detail`）；400 的 `detail` 文案按具体校验点不同（见上表）。**判分支只认 status code。**

---

## 前端坑点

- **token 会过期，别硬编码 TTL 秒数**。默认 300s，但由 `media_token_ttl` 配置，前端**不应假设**任何具体秒数。
- **HLS 播放中途 token 过期 → 段请求返回 403**：一次录像/VOD 播放可能横跨多个 300s 窗口，playlist 里内嵌的 token 到期后，后续段 GET 会 403。**正确处理是重新向 [traceback](traceback.md) 拉一份新 playlist**（其中的 token 已重新签发），用新 URL 续播；**不要**在前端自己续签或缓存旧 token。
- **段是 fMP4 fragment，不能单独播**：必须先拿同 step 的 init 段（`GET /media/init/{token}`）初始化 MSE，再喂 segment。init 缓存 1 小时、段缓存 60s，按此设缓存即可。
- **服务重启可能让所有 token 失效**：若生产未配 `CLEANSIGHT_MEDIA_TOKEN_SECRET`，用的是进程级临时 secret，重启后旧 playlist 里的 token 全部变 403——同样靠"重拉 playlist"恢复。
- **无法主动吊销 token**：签发后在 TTL 内一直有效，前端不要把 token URL 当"一次性凭证"，它是"有时限的可重复拉取地址"。

---

## 静默失败

以下情况后端**不报错**或**行为易误判**，排查时容易当成 bug：

| 现象 | 后端实际状态 |
|------|------------|
| 段一路 200，突然全变 403 | token 到期或服务重启使 secret 变化——不是鉴权配错，重拉 playlist 换新 token |
| 200 但播放器解不出画面 | 拿到了 segment 却没先拉/缓存 init 段；fMP4 fragment 无 init 无法解码 |
| 404 而非 403 | token 合法、TTL 内，但磁盘上该文件已被滚动清理或从未落盘（段过期删除） |
| 400 `Invalid path` | 极少见，通常意味着 token 被篡改或签发侧构造了越界文件名——正常前端不会触发 |

> 消费本接口的实现：token URL 由 [traceback](traceback.md) 的 playlist 端点内嵌进 `.m3u8`，前端播放器（HLS.js / 原生 MSE）按 playlist 逐段请求，一般不直接手写这两个 URL。
