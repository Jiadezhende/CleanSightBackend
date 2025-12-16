**RTSP 全流程调用说明（CleanSightBackend）**

概述
- 目标：演示从推流端（FFmpeg）推流到 MediaMTX；后端通过 `/inspection` 拉取 RTSP 流并送入 AI 服务；通过 `/ai/load_task/{task_id}` 把数据库中的 task 与内存 `client_id` 关联；可通过 WebSocket `/ai/video` 获取可视化帧。
- 关键概念：
  - `client_id`：系统中标识流/客户端的字符串，代码中由 `DBTask.source_ip` 提供并用作 `client_id`。
  - `task_id`：任务表主键，用于从数据库读取任务配置并在 AI 服务中装载任务。
  - `rtsp_url`：推流地址（MediaMTX 或发布者对外的 RTSP 地址），后端从该地址拉流进行处理。

主要接口

- GET `/ai/load_task/{task_id}`
  - 功能：从数据库读取 `task_id` 的记录，取 `source_ip` 作为 `client_id`，在内存中为该 `client_id` 设置任务（`ai.set_task`）。
  - 请求示例：GET `/ai/load_task/1`
  - 成功返回字段（示例）：
    - `task_id`: int
    - `status`: string
    - `cleaning_stage`: string
    - `bending`: bool
    - `bubble_detected`: bool
    - `fully_submerged`: bool
    - `updated_at`: ISO 时间字符串
  - 常见错误：404(未找到任务)、400(source_ip 为空)、500(内部错误)

- POST `/inspection/start_rtsp_stream`
  - 功能：为 `client_id` 启动后台拉流线程，从指定 `rtsp_url` 拉流并把帧送到 AI 服务。
  - 请求 JSON：
    - `client_id`: string（必填）
    - `rtsp_url`: string（必填）
    - `fps`: int（可选，默认 30）
  - 请求示例：
    {
      "client_id": "172.16.77.221",
      "rtsp_url": "rtsp://36.103.203.206:8004/live/172.16.77.221",
      "fps": 30
    }
  - 成功响应示例：
    {"status":"success","message":"RTSP 流捕获已启动 for 172.16.77.221"}
  - 注意：若相同 `client_id` 已在运行，会返回 400 错误。

- POST `/inspection/stop_rtsp_stream?client_id=...`
  - 功能：停止该 `client_id` 的拉流线程并清理 AI 客户端资源。
  - 请求示例：POST `/inspection/stop_rtsp_stream?client_id=172.16.77.221`
  - 成功响应：{"status":"success","message":"RTSP 流捕获已停止 for 172.16.77.221"}

- GET `/ai/status`
  - 功能：查询 AI 服务整体状态（用于健康检查）。

- POST `/ai/terminate_task/{client_id}`
  - 功能：终止并清理指定 `client_id` 的任务（释放内存资源）。
  - 请求示例：POST `/ai/terminate_task/172.16.77.221`

- WebSocket `/ai/video?client_id=xxx`
  - 功能：向连接者推送实时可视化帧，消息为文本格式的 data URL：`data:image/jpeg;base64,<b64>`。
  - 用途：在前端或测试脚本中显示 AI 的可视化结果。

端到端调用顺序（推荐）
1. 确认后端服务、MediaMTX 与数据库可达；确认或创建 `task`，并保证 `DBTask.source_ip` 填写为期望的 `client_id`（例如 `172.16.77.221`）。
2. 在推流端启动 FFmpeg 将视频推到 MediaMTX：
   - 推流地址通常：`rtsp://<mediamtx-host>:<port>/live/<client_id>`，与 `DBTask.source_ip` 保持一致。
   - 示例（PowerShell）：
```powershell
ffmpeg -an -re -stream_loop -1 -i test_video.mp4 -c:v libx264 -preset ultrafast -tune zerolatency -rtsp_transport tcp -f rtsp rtsp://36.103.203.206:8004/live/172.16.77.221
```
3. 在后端装载任务（将 DB 的 `task_id` 与 `client_id` 关联）：
   - 调用：GET `/ai/load_task/{task_id}`
   - 示例：
```powershell
Invoke-RestMethod -Method Get -Uri "http://36.103.203.206:8000/ai/load_task/1"
```
4. 告知后端开始拉流并处理：
   - 调用：POST `/inspection/start_rtsp_stream`，body 带 `client_id` 与 `rtsp_url`。
   - 示例：
```powershell
$body = @{ client_id="172.16.77.221"; rtsp_url="rtsp://36.103.203.206:8004/live/172.16.77.221"; fps=30 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://36.103.203.206:8000/inspection/start_rtsp_stream" -Body $body -ContentType "application/json"
```
5. （可选）连接 WebSocket `/ai/video?client_id=...` 接收实时可视化帧。
6. 测试或运行结束后，停止拉流并终止任务：
```powershell
Invoke-RestMethod -Method Post -Uri "http://36.103.203.206:8000/inspection/stop_rtsp_stream?client_id=172.16.77.221"
Invoke-RestMethod -Method Post -Uri "http://36.103.203.206:8000/ai/terminate_task/172.16.77.221"
```

注意事项与故障排查
- 确保 `client_id` 在 DB (`task.source_ip`) 与推流路径一致；后端代码以 `source_ip` 作为 `client_id`。
- 推流地址应使用后端可访问的 IP（尽量使用 IPv4 地址而非 `localhost`），避免 IPv6/解析差异导致的连接失败。
- 如果后端拉流失败：查看后端日志和 ffmpeg stderr（拉流线程会在 ffmpeg 异常退出时打印 stderr）以定位 DESCRIBE/SETUP/READ 的错误代码或超时信息。
- 若出现重复启动错误（400），请先调用 stop 接口或在后端确认没有残留线程。

附录：常见示例（一套流程）
1) 在 DB 创建或确认 task（确保 `source_ip` 设置为 `172.16.77.221`）
2) 启动推流（机器 A）
3) 装载任务：GET `/ai/load_task/1`
4) 启动拉流：POST `/inspection/start_rtsp_stream`（带 `client_id` 与 `rtsp_url`）
5) 可选：用 WebSocket 订阅 `/ai/video?client_id=172.16.77.221` 显示可视化

如需我把此文档再扩展为带图的 README 或直接生成 PowerShell 测试脚本，请回复要使用的 `task_id`、MediaMTX host:port，以及希望的 `client_id` 示例。
