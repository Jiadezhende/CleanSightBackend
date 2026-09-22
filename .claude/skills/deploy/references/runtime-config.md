# 运行时配置：.env*、端口、模型权重（所有平台共用）

不影响安装，但决定后端能否启动、连到哪套库与告警端点。业务参数写在 `.env*`（每台机器一份，不进 git，模板 `.env.example`）；端口是例外，在启动脚本里声明。

## 三个角色

| | prod | test | dev |
|---|---|---|---|
| 配置文件 | `.env` | `.env.test` | `.env.dev` |
| 启动 | `start_backend.sh prod` | `start_backend.sh test` | `start_backend.sh dev` / `.ps1 dev` |
| 端口 | 基准 | 基准 **+2** | 基准（与 prod 同值，分属不同机器） |
| uvicorn `--reload` | 否 | 否 | 是 |
| 数据库 | 生产库 | 独立测试库（**务必与生产不同库**） | 本地库或不可达地址 |
| 告警上报 | 真实端点 | 测试端点 | 不可达地址 |

`CLEANSIGHT_ENV` 由启动脚本设置，**不要写进 `.env*`**。

## 必填六项，缺一项哪个角色都起不来

`CLEANSIGHT_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` + `CLEANSIGHT_ALARM_REPORT_URL`。缺任意一项 `app.settings` 导入即抛 pydantic `ValidationError`，与 `CLEANSIGHT_STRICT` 无关。

## 其余配置项与推荐值

| 变量 | prod | test | dev | 留空时 |
|---|---|---|---|---|
| `CLEANSIGHT_DEBUG` | `false` | `false` | `true` | `false`。`true` 会打开 SQLAlchemy `echo`，全量 SQL 进日志，生产勿开 |
| `CLEANSIGHT_LOG_LEVEL` | `INFO` | `INFO` | `DEBUG` | `INFO` |
| `CLEANSIGHT_MEDIA_TOKEN_SECRET` | **必配** | 可空 | 可空 | 启动时随机生成，**重启后已发出的媒体 URL 全部 403** |
| `CLEANSIGHT_GATEWAY_ALLOWED_IPS` | 建议配（逗号分隔） | 留空 | 留空 | 空 = 不限制来源 IP |
| `CLEANSIGHT_GATEWAY_RATE_LIMIT` | `60` | `60` | `120` | `60` |
| `CLEANSIGHT_STORAGE_DIR` | 指向大容量盘 | 默认 | 默认 | `./database`，HLS 段与告警图都落这里，持续增长 |
| `CLEANSIGHT_MODEL_PATH` | 默认 | 默认 | 默认 | `./app/data` |
| `CLEANSIGHT_FFMPEG_PATH` | 留空 | 留空 | 留空 | 项目内 `.ffmpeg/bin/ffmpeg`，**不回退 PATH** |
| `CLEANSIGHT_STRICT` | `1` | `1` | `0` | `0` |
| `CLEANSIGHT_LABEL_STUDIO_URL` / `_TOKEN` / `_DEFAULT_PROJECT_ID` | 按需 | 按需 | 按需 | 空 = 不启用样本回流 |

### 生产 `.env` 模板

```dotenv
CLEANSIGHT_DB_HOST=...
CLEANSIGHT_DB_PORT=5432
CLEANSIGHT_DB_NAME=...
CLEANSIGHT_DB_USER=...
CLEANSIGHT_DB_PASSWORD=...
CLEANSIGHT_ALARM_REPORT_URL=http://<平台>/gdmp/v1/api/nt/alarm_report

CLEANSIGHT_DEBUG=false
CLEANSIGHT_STRICT=1
CLEANSIGHT_MEDIA_TOKEN_SECRET=<固定不变的随机串>
CLEANSIGHT_GATEWAY_ALLOWED_IPS=<大屏/平台侧 IP>
```

密钥生成：`python -c "import secrets; print(secrets.token_hex(32))"`。

`.env.test` 只改两处：独立测试库、测试告警端点。`.env.dev` 加 `CLEANSIGHT_DEBUG=true`、`CLEANSIGHT_LOG_LEVEL=DEBUG`，DB 与告警端点指向**不可达地址**，推荐 RFC 2606 保留域名（如 `alarm.invalid`），DNS 永不解析，从物理上杜绝误连生产库、误发真实告警。

## 端口

**唯一声明处是启动脚本**：Linux 改 `start_backend.sh` 的 `BASE_*` 五行，Windows 改 `start_backend.ps1` 的 `$Base*` 五行。脚本把结果以环境变量注入后端、网关与 MediaMTX，压过 `.env*`、`mediamtx.yml`、`config.ini`、`settings.py` 里的同名值，那些只是「脱离脚本单独跑某个进程」时的回退。

| 用途 | dev / prod | test（+2） | 对外？ |
|------|-----------|-----------|-------|
| 后端 HTTP/WS | 8000 | 8002 | 是 |
| 网关对外 RTSP | 8004 | 8006 | 是 |
| MediaMTX RTSP（内部） | 18004 | 18006 | 否 |
| MediaMTX RTP / RTCP（UDP，内部） | 8002 / 8003 | 8004 / 8005 | 否 |

- **启动前确认这五个口空闲**，实际以脚本当前取值为准。被占不会在安装自检里暴露，只在启动时失败，且 MediaMTX 的绑定失败落在网关日志、不在后端日志。Linux 查 `ss -ltn`，Windows 见 windows.md 坑 1（HNS 预留对 netstat 不可见）。
- test 与 prod 错开 2，可同机并行；但只保证这两套之间不打架，其他进程是否占口仍要单独确认。
- **改端口两个脚本同步改**。改过的脚本在部署机留 git 本地 diff，`git pull` 时手动处理。
- 对外两个口若经 NAT 映射，**外部端口必须等于内部端口**。非等值映射下后端认不出本机 MediaMTX（`app/services/stream/manager.py` 的 `_rewrite_rtsp_url`），会绕公网回源，多数环境直接不通。内部三个口只监听 `127.0.0.1`，不要映射。

## 模型权重

**不随 git 分发**，从内部模型库按需取用，放到 `CLEANSIGHT_MODEL_PATH` 指向的目录（默认 `app/data/`）。六份 `.pt` 与用到它们的检测点见 `config/inference_config.yaml` 的 `model_path` 行。`.gitignore` 已挡 `app/data/*.pt`；早期漏进版本库的 `bend-best.pt` / `bubble-best.pt` 已于 2026-09-22 停止跟踪，旧提交里仍有。
