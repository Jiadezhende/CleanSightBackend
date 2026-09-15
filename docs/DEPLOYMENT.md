# CleanSight Backend 部署指南

事实来源是四个脚本：`deploy.conf`、`install.sh`、`install.ps1`、`build.sh`。与本文不符时以脚本为准。

- **生产** = Linux x86_64 + NVIDIA GPU，跑 `install.sh`。
- **开发机** = Windows，跑 `install.ps1`；仅为便利，不作为生产标准。
- 第三方二进制（ffmpeg / MediaMTX）安装后全部落在项目目录内，**不依赖系统 PATH**，也不要指向系统装的版本。

---

## 一、生产部署步骤

按顺序执行。括号内是对应的详细章节。

| # | 操作 | 命令 |
|---|---|---|
| 1 | 确认目标机满足前置条件（[§二](#二目标机前置条件)） | `python3 -V` → 必须 3.10；`nvidia-smi` |
| 2 | 拉起源机的物料分发服务（[§六](#六物料源机)） | `sudo systemctl start cleansight-dist` |
| 3 | 让管理员放通目标机 → 源机 `8088` | — |
| 4 | 目标机验证源机可达 | `curl -I http://49.234.120.241:8088/wheelhouse/SHA256SUMS` |
| 5 | 目标机写 `.env`（[§四](#四运行时配置)） | 至少填 DB 五项 + 告警 URL |
| 6 | 安装（[§三](#三安装)） | `BASE_URL=http://49.234.120.241:8088 ./install.sh` |
| 7 | 确认脚本末尾自检全过 | torch / CUDA / cv2 / ffmpeg / MediaMTX |
| 8 | **确认启动脚本声明的五个端口在本机空闲**（[§四·端口](#端口)） | 读 `start_backend.sh` 的 `BASE_*` 取值，逐个查占用 |
| 9 | 启动 | `./start_backend.sh prod` |
| 10 | 端到端验证（[§五](#五部署后验证)） | `test_single_client.py` |
| 11 | 关闭源机公网分发 | `sudo systemctl stop cleansight-dist` |

不重建物料就不需要跑 `build.sh`（[§七](#七重建物料buildsh)）。

---

## 二、目标机前置条件

| 项 | 生产（Linux） | 开发机（Windows） |
|---|---|---|
| 系统 / 架构 | Linux x86_64 | Windows |
| Python | **精确 3.10**，且 `python3 -V` 就是它 | 3.10–3.13 |
| GPU | NVIDIA 驱动就位，`torch.cuda.is_available()` 为真；不需装 CUDA toolkit | 同左 |
| 网络 | 能访问源机 `BASE_URL` 与清华 PyPI 镜像 | 同左 |
| 端口 | 启动脚本声明的五个口本机空闲（取值与查法见 [§四·端口](#端口)） | 同左 |
| 工具 | `curl`、`python3 -m venv` | PowerShell |

> Python 3.10 是生产硬约束：`wheelhouse/` 按 cp310 打标签，版本不符 `install.sh` 启动即退出。多 Python 环境下要确保 PATH 上的 `python3` 就是 3.10（脚本检的是 `python3 -V`，不是旁装的 `python3.10`）。安装后 `.venv` 已绑定它，激活后用 `python` 即可。

---

## 三、安装

### Linux 生产

```bash
BASE_URL=http://49.234.120.241:8088 ./install.sh
```

`BASE_URL` 指向物料源机。两种给法，推荐前者（不改仓库文件）：

```bash
BASE_URL=http://<源机IP>:<端口> ./install.sh   # 临时指定
BASE_URL="http://<源机IP>:<端口>"              # 或固定写进 deploy.conf
```

**留空 `BASE_URL`** 则改用目标机本地物料，此时这三个文件必须已存在（同步方式见 [§六](#六物料源机)）：

```text
wheelhouse/SHA256SUMS
vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
vendor/mediamtx/mediamtx-linux-x64.tar.gz
```

脚本依次做：建 `.venv/` → 装 `TORCH_PKGS`（源自 wheelhouse）→ 从清华镜像装 `requirements.txt` → 修 opencv 冲突并把 numpy 钉回 `1.26.4` → 校验 SHA 后部署 ffmpeg 与 MediaMTX → 末尾自检。

### Windows 开发机

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
$env:BASE_URL="http://49.234.120.241:8088"   # 可选，不给则从 deploy.conf 的 *_WIN_URL 在线下载
.\install.ps1
```

与生产的差异：torch 从 cu128 镜像在线装（不用 `wheelhouse/`）、Python 版本宽松、Windows 包不做 SHA 强校验。

### 安装产物

```text
.venv/
.ffmpeg/bin/ffmpeg        # Windows 为 ffmpeg.exe
mediamtx/mediamtx         # Windows 为 mediamtx.exe
mediamtx/mediamtx.yml     # 随仓库维护，安装脚本只更新二进制，不覆盖它
```

应用默认用项目内这两个二进制（`mediamtx_gateway/config.ini` 的 `mediamtx_bin = auto`），无需额外配置。

### deploy.conf 变量速查

| 变量 | 用途 |
|---|---|
| `BASE_URL` | 物料源机地址；空 = 用本地 `wheelhouse/` 与 `vendor/` |
| `TORCH_PKGS` | 核心重包，当前 `torch==2.8.0 torchvision==0.23.0` |
| `FFMPEG_URL` / `FFMPEG_SHA256` | Linux 生产钉版 ffmpeg |
| `MEDIAMTX_URL` / `MEDIAMTX_SHA256` | Linux 生产钉版 MediaMTX |
| `FFMPEG_WIN_URL` / `MEDIAMTX_WIN_URL` | Windows 开发机二进制（无 SHA 校验） |

---

## 四、运行时配置

不影响安装，但决定后端能否启动、连到哪套库与告警端点。业务参数写在 `.env*`（每台机器一份，不进 git，模板见 [.env.example](../.env.example)）；端口是例外，在启动脚本里声明（见本节末）。

### 三个环境的差异

| | prod | test | dev |
|---|---|---|---|
| 配置文件 | `.env` | `.env.test` | `.env.dev` |
| 启动 | `./start_backend.sh prod` | `./start_backend.sh test` | `./start_backend.sh dev`（Windows：`.\start_backend.ps1 dev`） |
| 端口 | 内置基准 | 基准 **+100** | 内置基准（与 prod 同值，分属不同机器） |
| uvicorn `--reload` | 否 | 否 | 是 |
| 数据库 | 生产库 | 独立测试库（**务必与生产不同库**） | 本地库 |
| 告警上报 | 真实端点 | 测试端点 | 不可达地址 |

启动脚本一条命令拉起网关（含 MediaMTX）+ 后端。`CLEANSIGHT_ENV` 由脚本设置，**不要写进 `.env*`**。

### 必填六项，缺一项哪个环境都起不来

`CLEANSIGHT_DB_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` + `CLEANSIGHT_ALARM_REPORT_URL`。

缺任意一项，`app.settings` 导入即抛 pydantic `ValidationError`，**与 `CLEANSIGHT_STRICT` 无关**（该开关只在「字段存在但为 0」的边角情形才走到）。

### 其余配置项与推荐值

| 变量 | prod | test | dev | 留空时 |
|---|---|---|---|---|
| `CLEANSIGHT_DEBUG` | `false` | `false` | `true` | `false`。置 `true` 会打开 SQLAlchemy `echo`，全量 SQL 进日志，生产勿开 |
| `CLEANSIGHT_LOG_LEVEL` | `INFO` | `INFO` | `DEBUG` | `INFO` |
| `CLEANSIGHT_MEDIA_TOKEN_SECRET` | **必配** | 可空 | 可空 | 启动时随机生成，**重启后已发出的媒体 URL 全部 403** |
| `CLEANSIGHT_GATEWAY_ALLOWED_IPS` | 建议配（逗号分隔） | 留空 | 留空 | 空 = 不限制来源 IP |
| `CLEANSIGHT_GATEWAY_RATE_LIMIT` | `60` | `60` | `120` | `60` |
| `CLEANSIGHT_STORAGE_DIR` | 指向大容量盘 | 默认 | 默认 | `./database`（HLS 段与告警图都落这里，会持续增长） |
| `CLEANSIGHT_MODEL_PATH` | 默认 | 默认 | 默认 | `./app/data` |
| `CLEANSIGHT_FFMPEG_PATH` | 留空 | 留空 | 留空（Mac 可指 homebrew） | 项目内 `.ffmpeg/bin/ffmpeg`，**不回退 PATH** |
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

`.env.test` 只改两处：独立测试库、测试告警端点（端口自动 +100，无需配置）。
`.env.dev` 加 `CLEANSIGHT_DEBUG=true`、`CLEANSIGHT_LOG_LEVEL=DEBUG`，并把 DB 与告警端点指向**不可达地址**——推荐用 RFC 2606 保留域名（如 `alarm.invalid`），DNS 永不解析，从物理上杜绝误连生产库、误发真实告警。

### 端口

**端口不在 `.env*` 里配，唯一声明处是启动脚本**：Linux 改 [start_backend.sh](../start_backend.sh) 的 `BASE_*` 五行，Windows 改 [start_backend.ps1](../start_backend.ps1) 的 `$Base*` 五行。脚本会把结果以环境变量注入后端、网关与 MediaMTX，压过 `.env*`、`mediamtx.yml`、`config.ini`、`settings.py` 里的同名值——那些只是「脱离脚本单独跑某个进程」时的回退。

| 用途 | dev / prod | test（+2） | 对外？ |
|------|-----------|--------------|-------|
| 后端 HTTP/WS | 8000 | 8002 | 是 |
| 网关对外 RTSP | 8004 | 8006 | 是 |
| MediaMTX RTSP（内部） | 18004 | 18006 | 否 |
| MediaMTX RTP / RTCP（UDP，内部） | 8002 / 8003 | 8004 / 8005 | 否 |

- **启动前必须确认这五个口在本机空闲**——上表是默认值，实际以启动脚本当前的 `BASE_*` / `$Base*` 取值为准，改过端口就按改后的查。被占时不会在安装自检里暴露，只在启动时失败，且 MediaMTX 的绑定失败落在网关日志里、不在后端日志里。占用方无法清除就改脚本换口（两个脚本同步改）。Windows 上还需留意 Docker/WSL 的 HNS 会**预留整段端口块**，`netstat` 看不到监听者但绑定照样失败。
- test 与 prod 错开 2，可同机并行互不抢占（TCP / UDP 两套端口各自不重号）——但这只保证这两套之间不打架，机器上其他进程是否占了这些口仍要单独确认。
- **改端口时两个脚本要同步改**（两份独立声明，不互相引用）。改过的脚本在部署机上会留 git 本地 diff，`git pull` 时手动处理。
- 对外两个口若经 NAT 映射，**外部端口必须等于内部端口**。非等值映射下后端认不出本机 MediaMTX（[`_rewrite_rtsp_url`](../app/services/stream/manager.py)），会绕公网回源，多数环境直接不通。内部三个口只监听 `127.0.0.1`，不要映射。

---

## 五、部署后验证

`install.sh` 的自检只覆盖依赖与二进制。业务链路要跑一次单客户端集成测试（推流 → `/api/start` → 推理 → `/api/terminate`）。

```bash
source .venv/bin/activate
python integration_tests/test_single_client.py --scenario 1 --task_id <任务ID> --duration 30 --no-window

# 从另一台机器验证远程后端，加 --server（端口非默认时再加 --api-port / --rtsp-port）
python integration_tests/test_single_client.py --scenario 1 --task_id <任务ID> \
    --server <目标机IP> --duration 30 --no-window
```

跑之前确认：

- `http://<目标机IP>:<后端端口>/health` 可访问（默认 8000，test 为 8002）。
- 网关 RTSP 端口可访问（默认 8004，test 为 8006）。
- 测试视频在 `test/test_video.mp4`，否则用 `--video_path` 指定。
- `<任务ID>` 在数据库中可用；脚本找不到会尝试建测试任务，因此 DB 必须可写。

通过标准：推流成功 → `/api/start` 返回成功 → 跑满 `duration` 无异常退出 → `/api/terminate` 清理干净。

---

## 六、物料源机

### 当前源机

```text
SSH 别名：label-studio        公网 IP：49.234.120.241
内部主机：VM-32-133-ubuntu    用户：ubuntu
分发端口：8088               （8080 被 Label Studio 占用）
```

分发服务由 systemd 管理，**`static` 无 `[Install]`、不开机自启**，只在部署窗口手动开关：

```bash
sudo systemctl start cleansight-dist    # 部署窗口开始
sudo systemctl status cleansight-dist
sudo systemctl stop  cleansight-dist    # 部署窗口结束，务必关闭
```

```ini
# /etc/systemd/system/cleansight-dist.service
ExecStart=/usr/bin/python3 -m http.server 8088 --bind 0.0.0.0 --directory /srv/cleansight-dist
User=ubuntu
Restart=on-failure
```

服务根目录只放物料软链，不暴露代码仓库和 `.env`：

```text
/srv/cleansight-dist/
  wheelhouse -> /data/cleansight-offline/wheelhouse
  vendor     -> /data/cleansight-offline/vendor
```

自检：

```bash
curl -sI localhost:8088/wheelhouse/SHA256SUMS                          # 源机本机
curl -I http://49.234.120.241:8088/wheelhouse/SHA256SUMS               # 目标机/外网
curl -I http://49.234.120.241:8088/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
curl -I http://49.234.120.241:8088/vendor/mediamtx/mediamtx-linux-x64.tar.gz
```

已就位的固定名物料（Linux 与 Windows 各两份，`install.sh` / `install.ps1` 从 `BASE_URL` 派生这些路径）：

```text
vendor/ffmpeg/ffmpeg-linux-x64.tar.xz     vendor/ffmpeg/ffmpeg-win-x64.zip
vendor/mediamtx/mediamtx-linux-x64.tar.gz vendor/mediamtx/mediamtx-win-x64.zip
# Windows MediaMTX SHA256: 19cd9d1fbb76225380859109175b7547d2e68b4b70858be4fa565604743acf8d
```

> 该服务绑 `0.0.0.0:8088`、明文无鉴权，`ufw` 未启用时只靠云安全组挡。**不要长期裸跑在公网**；长期分发应交管理员用 nginx / 对象存储托管并限制来源。

### 不走 HTTP：直接同步物料

```bash
rsync -av wheelhouse/ <target>:/path/to/CleanSightBackend/wheelhouse/
rsync -av vendor/     <target>:/path/to/CleanSightBackend/vendor/
# 然后目标机不带 BASE_URL 直接跑
./install.sh
```

此模式下 `install.sh` 先校验 `wheelhouse/SHA256SUMS` 再安装。

---

## 七、重建物料（build.sh）

**不是每次部署都要跑。** 只在这四种情况下重建，其余时候复用同一批 `wheelhouse/` 与 `vendor/`：

- 调整 `torch` / `torchvision` 版本或 CUDA wheel 来源
- 升级 ffmpeg 或 MediaMTX
- 首次准备源机物料

```bash
./build.sh   # 需 Linux x86_64 + Python 3.10（与生产一致）+ python3/pip/curl/sha256sum
```

升级 Linux 二进制的流程（`BASE_URL` 不参与构建）：

1. 改 `FFMPEG_URL` 或 `MEDIAMTX_URL`
2. 临时清空对应的 `*_SHA256`
3. 在构建机跑 `./build.sh`
4. 把打印出的 SHA 回填 `deploy.conf`
5. 再跑一次 `./build.sh`，确认校验通过

产物（已在 `.gitignore`，不要提交）：

```text
wheelhouse/SHA256SUMS + *.whl
vendor/ffmpeg/{ffmpeg-linux-x64.tar.xz, ffmpeg-win-x64.zip}
vendor/mediamtx/{mediamtx-linux-x64.tar.gz, mediamtx-win-x64.zip}
```

> `build.sh` 依赖外部 URL 下载，这些地址可能失效，所以它是「物料重建工具」，不是日常发布步骤。生产也不要临时混装 PyTorch / CUDA / ffmpeg / MediaMTX 版本——冲突重灾区，统一由 `deploy.conf` 钉版。
