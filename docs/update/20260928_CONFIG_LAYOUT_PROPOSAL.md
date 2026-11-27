# 运行配置整理提案：环境相关的进 .env，其余合成一个 app.yaml 按逻辑分组

> **变更状态**：提案（2026-09-28）——只定配置落点，逐项调整后再分批落地
> **知识库**：无需沉淀（提案本身；结论随各批记录沉淀）

## 概述

现有配置分散在 `.env*`（经 `app/settings.py`）、6 份按服务拆分的 `config/*_config.yaml`、存储根下的
`lab_runtime_config.json` 三处，同一个逻辑主题（如一路视频的取流参数）被拆在好几个文件里。本提案按「谁变」分三处放，
yaml 合成一个 `app.yaml`，按逻辑主题分组，不按模块分。

## 判据

```text
每个环境取值不同（连接串、端口、路径、密钥、显卡、外部 URL） → .env
各环境一致                                                   → config/app.yaml（检测点定义独立为 inference.yaml）
页面上可改、跨部署保留                                         → 运行时配置
```

## 目录结构

图例：`←` 从哪里迁来；`[待定]` 尚未决定。

```text
.env / .env.dev / .env.test     环境相关（app/settings.py 只承载这一类）

config/
  app.yaml                      除检测点定义外的全部配置，按逻辑主题分组
  inference.yaml                检测点 / 模型 / 阈值 / 规则 ← config/inference_config.yaml
                                + 顶层 decimation：抽帧倍率 ← settings.inference_decimation
  logging.json                  （不变）

app/services/algorithm/colorstrip/params.yaml   算法参数档，留在算法包内（包自包含，cli 同目录直读）

{仓库根}/.lab/runtime_config.json   运行时：页面可改、实时写回 ← {存储根}/lab_runtime_config.json
                                    .lab/ 进 .gitignore（同 .ffmpeg/、.ultralytics/ 的仓库根本地目录）
```

## app.yaml 分组

```text
stream:          一路视频从拉流到帧入队
  raw_fps                         ← settings.raw_fps
  read_timeout_s                  ← settings.rtsp_read_timeout_s
  width / height / pix_fmt        ← stream_config.decoder（decoder 的 ffmpeg scale 是唯一生效的帧尺寸）
                                  client_config.frame.resize_* 删除：CQ 存了但无人读取
  chunk_read_size                 ← stream_config.decoder
  buffer_seconds                  ← settings.ca_maxlen_seconds
  backpressure_ratio              ← stream_config.decoder

recording:       录像切段与落盘
  segment_seconds                 ← settings.ca_segment_seconds
  queue_size / sweep_interval_s   ← recording_config

run:             一次 run 的看护与终止：断流重连、放弃、孤儿、超时
  check_interval / heartbeat_timeout / reconnect_interval
  cleanup_timeout / orphan_timeout ← health_monitor_config
  max_duration                    ← health_monitor_config.task_max_duration

alarm:           告警上报
  workers / queue_size            ← persistence_config.alarm

cleanup:         盘上产物保留
  enabled / days / interval_s     ← persistence_config.storage

access:          对外访问控制
  gateway 限流 / 封禁 / 反扫描阈值与路径前缀 ← settings.gateway_*（白名单 IP 仍留 .env）
  media_token_ttl                 ← settings.media_token_ttl

lab:             送标导出
  ffmpeg_preset / max_clip_ms / max_total_ms / max_clips_per_submit ← settings.lab_export_*
```
