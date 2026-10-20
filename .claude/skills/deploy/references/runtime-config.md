# 运行时配置：.env*、端口、模型权重（所有平台共用）

不影响安装，但决定后端能否启动、连到哪套库与告警端点。业务参数写在 `.env*`（每台机器一份，不进 git，模板 `.env.example`）；端口是例外，在启动脚本里声明。

## 三个角色

| | prod | test | dev |
|---|---|---|---|
| 配置文件 | `.env` | `.env.test` | `.env.dev` |
| 启动 | `start_backend.sh prod` | `start_backend.sh test` | `start_backend.sh dev` / `.ps1 dev` |
| 端口 | 基准 | 基准 **+2** | 基准（与 prod 同值，分属不同机器） |
| uvicorn `--reload` | 否 | 否 | 是 |
| 缺必填项 | 启动即抛错 | 启动即抛错 | 只告警 |
| 数据库 | 生产库 | 独立测试库（**务必与生产不同库**） | 用户自定，**勿用生产库** |
| 告警上报 | 真实端点 | 测试端点 | 不可达地址 |

`CLEANSIGHT_ENV` 由启动脚本设置，**不要写进 `.env*`**。

## 键的两类：AI 写哪些、用户填哪些

| 类别 | 键 | 谁写 |
|------|----|------|
| 敏感 | `CLEANSIGHT_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD`、`CLEANSIGHT_ALARM_REPORT_URL`、`CLEANSIGHT_GATEWAY_ALLOWED_IPS`、`CLEANSIGHT_LABEL_STUDIO_URL` / `_TOKEN` / `_DEFAULT_PROJECT_ID` | 用户。AI 不写、不读 |
| 敏感，AI 生成 | `CLEANSIGHT_MEDIA_TOKEN_SECRET` | AI 在目标机生成后直接写进文件，不经过终端输出 |
| 非敏感 | 下表其余各项 | AI 按场景写，可覆写 |

例外：dev 的 `CLEANSIGHT_ALARM_REPORT_URL` 仍是模板占位符时，AI 直接写 `http://alarm.invalid/gdmp/v1/api/nt/alarm_report`。RFC 2606 保留域名，DNS 永不解析，从物理上杜绝误发真实告警。

## 按场景启用变量

「—」= 不写，用代码默认值。场景按机器实际情况判，不只看角色名：判据写在表后。

| 变量 | 本机 / 内网开发（dev） | 测试机（test） | 生产（prod） | 代码默认 |
|---|---|---|---|---|
| `CLEANSIGHT_DEBUG` | `true` | `false` | `false` | `false`。`true` 打开 SQLAlchemy `echo`，全量 SQL 进日志 |
| `CLEANSIGHT_LOG_LEVEL` | `DEBUG` | `INFO` | `INFO` | `INFO` |
| `CLEANSIGHT_STRICT` | `0` | `1` | `1` | `0` |
| `CLEANSIGHT_GATEWAY_ENABLED` | `false` | `true` | `true` | `true`。`false` 关掉白名单、限流、封禁、反扫描整层 |
| `CLEANSIGHT_GATEWAY_ALLOWED_IPS` | — | — | 用户填（交接时提示） | 空 = 不限制来源 IP |
| `CLEANSIGHT_MEDIA_TOKEN_SECRET` | — | — | AI 生成 | 空 = 启动时随机生成，**重启后已发出的媒体 URL 全部 403** |
| `CLEANSIGHT_STORAGE_DIR` | — | — | AI 列各盘剩余空间给建议，用户确认后写 | `./database`，HLS 段与告警图都落这里，持续增长 |
| `CLEANSIGHT_CUDA_DEVICE` | 共享卡时锁卡 | 共享卡时锁卡 | 共享卡时锁卡 | `0` |
| `CLEANSIGHT_CAPACITY_PLANNED_STREAMS` | — | 同 prod | 按实际最大路数 | `15`。启动内存自检按它估最坏占用，**不限制实际路数** |
| `CLEANSIGHT_MEMORY_CHECK_ENFORCE` | — | 内存不足 32GB 时 `false` | — | `true`：最坏占用超过 `MemAvailable` 拒绝启动，仅 Linux 生效 |
| `CLEANSIGHT_VIZ_PROCESSES` | — | — | — | `2`。15 路时 processed 出帧不到 14fps 调 `3`；`0` = 线程内渲染 |
| `CLEANSIGHT_MODEL_PATH` / `CLEANSIGHT_FFMPEG_PATH` | — | — | — | `./app/data` / 项目内 `.ffmpeg/bin/ffmpeg`（**不回退 PATH**） |

判据：

- **网关开不开看机器对谁可达**，不看角色名：只有本机或内网开发机能关；对公网或业务网开放的机器，哪怕跑 dev 也保持 `true`。
- **共享卡**：同机有别的项目在用 GPU（如 PPU 备用机）就锁卡，卡号问用户。
- **Label Studio 三项**：用户要启用样本回流时才在交接里提示填，否则不提。
- 表里没列的键（限流阈值、宽松路径、拉流超时等）用 `.env.example` 注释里的默认值，不写。

> **共享内存**：推理与渲染的帧环各占约 29MB，在 `/dev/shm`。裸机部署没有影响；如果放进容器，默认 64MB 的 `/dev/shm` 余量很小，要 `--shm-size=256m`。

## 写 .env*：只按键操作

Windows 上 Claude Code 的 shell 也是 Git Bash，下列命令通用；只有 `chmod` 与 `python3` 两处在 Windows 上分别跳过、改用 `python`。远程部署时整段包进 `ssh <别名> '...'` 在目标机执行。

```bash
cd <项目目录>
F=.env.dev                                   # prod → .env，test → .env.test
[ -f "$F" ] || cp .env.example "$F"          # 已存在就不覆盖，只补键

# 覆写或追加一个非敏感键。只能用在上表的非敏感键上
setkv() { if grep -q "^$1=" "$F"; then sed -i "s|^$1=.*|$1=$2|" "$F"; else printf '%s=%s\n' "$1" "$2" >> "$F"; fi; }
setkv CLEANSIGHT_DEBUG true                  # 按场景表逐项写
setkv CLEANSIGHT_LOG_LEVEL DEBUG
setkv CLEANSIGHT_STRICT 0
setkv CLEANSIGHT_GATEWAY_ENABLED false

# dev：告警键仍是占位符才写不可达地址（判断在 shell 里做，不输出值）
grep -qE '^CLEANSIGHT_ALARM_REPORT_URL=.*your-server' "$F" \
  && setkv CLEANSIGHT_ALARM_REPORT_URL http://alarm.invalid/gdmp/v1/api/nt/alarm_report

# prod：媒体密钥缺失或为空才生成，直接写文件；之后收紧权限
grep -q '^CLEANSIGHT_MEDIA_TOKEN_SECRET=.' "$F" || {
  sed -i '/^CLEANSIGHT_MEDIA_TOKEN_SECRET=/d' "$F"
  printf 'CLEANSIGHT_MEDIA_TOKEN_SECRET=%s\n' "$(python3 -c 'import secrets;print(secrets.token_hex(32))')" >> "$F"
}
chmod 600 "$F"
```

## 交接：告诉用户填什么

写完非敏感项就停下，按下面的模板给用户，等回复再往下：

```text
请打开 <目标机>:<项目目录>/<.env.dev|.env|.env.test>，填写以下键（等号后直接写值）：
  CLEANSIGHT_DB_HOST / _PORT / _NAME / _USER / _PASSWORD   业务库连接
  CLEANSIGHT_ALARM_REPORT_URL                             告警上报地址          [dev 已填不可达地址，可不动]
  CLEANSIGHT_GATEWAY_ALLOWED_IPS                          大屏/平台侧 IP，逗号分隔；留空 = 不限制来源   [prod]
  CLEANSIGHT_LABEL_STUDIO_URL / _TOKEN / _DEFAULT_PROJECT_ID                                         [要样本回流时]
dev / test 不要填生产库。填好回复「填好了」，我只检查键是否齐全，不读取值。
```

## 键检查：只输出状态

```bash
F=.env.dev   # 按角色换
for k in CLEANSIGHT_DB_HOST CLEANSIGHT_DB_PORT CLEANSIGHT_DB_NAME CLEANSIGHT_DB_USER \
         CLEANSIGHT_DB_PASSWORD CLEANSIGHT_ALARM_REPORT_URL; do      # prod 追加 CLEANSIGHT_MEDIA_TOKEN_SECRET
  v=$(grep -E "^$k=" "$F" | tail -1 | cut -d= -f2-)
  if [ -z "$v" ]; then s=缺失; else case "$v" in your_*|*your-server*) s=占位符;; *) s=已填;; esac; fi
  echo "$k  $s"
done
```

prod 另查一下白名单：`grep -q '^CLEANSIGHT_GATEWAY_ALLOWED_IPS=.' .env && echo 已配 || echo 未配`。未配时在汇报里写明「不限制来源 IP」这一风险。

全部「已填」才往下启动。键检查只看格式，连不连得上库要到启动后看 `/health/status` 与筛过的日志。

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
- 对外两个口若经 NAT 映射，**外部端口必须等于内部端口**。非等值映射下后端认不出本机 MediaMTX（`app/services/stream/service.py` 的 `_rewrite_rtsp_url`），会绕公网回源，多数环境直接不通。内部三个口只监听 `127.0.0.1`，不要映射。

## 模型权重：魔搭手动下载，跑真推理前核对

**不随 git 分发，也不走源机**。用户从魔搭手动下载，放到 `CLEANSIGHT_MODEL_PATH` 指向的目录（缺省 `app/data/`）。需要哪些文件以 `config/inference_config.yaml` 的 `model_path` / `model_dir` / `backbone_dir` 行为准，布局：

```text
app/data/
  bubble-best.pt  bend-best.pt  clean-large-best.pt  clean-small-best.pt  gru-final.pt   在线检测 / 时序
  clean-offline-<模型>/...                                                              离线策略，一个模型一个目录
  backbones/dinov2_vits14/                                                              DINOv2 hub 代码 + 权重
```

缺权重不挡启动（warmup 失败只记日志），所以放在启动验证之后、e2e 与真实任务之前核对：

```bash
MP=$(grep -E '^CLEANSIGHT_MODEL_PATH=' "$F" | tail -1 | cut -d= -f2-); MP=${MP:-./app/data}   # 非敏感键，可取值
grep -oE 'app/data\}/[^ #]+' config/inference_config.yaml | sed 's#.*app/data}/##' | sort -u \
  | while read p; do [ -e "$MP/$p" ] && echo "在    $p" || echo "缺失  $p"; done
CLEANSIGHT_ENV=<role> .venv/bin/python -m app.services.inference.offline.cli materials   # 离线目录内的文件；Windows 用 .venv/Scripts/python
```

有缺失就把清单连同目标目录给用户，请他从魔搭补齐后回复，再核一次。`.gitignore` 已挡 `app/data/*.pt`。
