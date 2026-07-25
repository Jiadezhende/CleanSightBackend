# CleanSight Backend 部署指南

本文以当前部署脚本为准：`deploy.conf`、`build.sh`、`install.sh`、`install.ps1`。旧部署说明可能已经过时，不作为本指南的事实来源。

## 总体原则

- 生产环境使用 Linux x86_64，执行 `install.sh`。
- Windows 仅作为 GPU 开发机环境，执行 `install.ps1`。
- 有条件时建议在 macOS 或 Linux 上开发；涉及生产依赖、CUDA、离线物料时，以 Linux 环境验证为准。
- 第三方二进制安装后完全自包含在项目目录内，不依赖系统 PATH：
  - ffmpeg 部署到 `.ffmpeg/`
  - MediaMTX 部署到 `mediamtx/`
- 依赖被分成两类：
  - 核心重包：`torch` / `torchvision` / CUDA 相关 wheel，由 `build.sh` 预先打包到 `wheelhouse/`。
  - 轻量 Python 依赖：安装时从线上镜像下载，来源写在安装脚本中。
- 生产环境不要临时混装 PyTorch、CUDA、ffmpeg、MediaMTX 版本；这些是冲突重灾区，统一由 `deploy.conf` 钉版。
- 同一批 `wheelhouse/` 和 `vendor/` 可以放在共享源机或共享容器中复用，后续 Linux 生产机或 Linux 开发容器通过 `BASE_URL` 快速安装。

## 安装侧

### 安装前必须确认的变量

安装脚本真正依赖的部署变量集中在 `deploy.conf`。生产安装前先确认这些变量，再执行 `install.sh`。

> 先决条件：目标机 `python3` 命令必须解析到 Python 3.10。`install.sh` 检的是 `python3 -V`（不是旁装的 `python3.10`），多 Python 环境下需确保 PATH 上的 `python3` 就是 3.10。这是生产硬约束——`wheelhouse/` 按 cp310 打标签，版本不符 `install.sh` 启动即退出。安装后 `.venv` 已绑定该 3.10，激活后用 `python` 即可。

生产安装前重点确认：

```bash
# 物料源机地址。空值表示使用项目本地 wheelhouse/ 与 vendor/。
BASE_URL="${BASE_URL:-}"

# 核心 Python 重包。
TORCH_PKGS="torch==2.8.0 torchvision==0.23.0"

# Linux 生产钉版二进制及 SHA。
FFMPEG_URL="..."
FFMPEG_SHA256="..."
MEDIAMTX_URL="..."
MEDIAMTX_SHA256="..."
```

`BASE_URL` 有两种用法：

```bash
# 推荐：不改 deploy.conf，安装时临时指定。
BASE_URL=http://<源机IP>:<分发端口> ./install.sh

# 或者：把 deploy.conf 中的 BASE_URL 改成固定源机地址。
BASE_URL="http://<源机IP>:<分发端口>"
```

当前源机的实际地址是：

```bash
BASE_URL=http://49.234.120.241:8088 ./install.sh
```

如果 `BASE_URL` 为空，目标机本地必须已经存在：

```text
wheelhouse/SHA256SUMS
vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
vendor/mediamtx/mediamtx-linux-x64.tar.gz
```

也就是说，要么配置 `BASE_URL` 从源机拉取，要么先把 `wheelhouse/` 和 `vendor/` 同步到项目目录。

Windows 开发机的二进制来源是另一组变量，同样集中在 `deploy.conf`：

```bash
# Windows 开发机二进制来源。
FFMPEG_WIN_URL="..."
MEDIAMTX_WIN_URL="..."
```

`BASE_URL` 对 Windows 同样生效：配置后 ffmpeg / MediaMTX 从源机拉取，否则从上面的 Windows URL 在线下载。Windows 安装的完整流程见下文「Windows 开发安装」。

### Linux 生产安装

Linux 安装入口是：

```bash
./install.sh
```

脚本要求：

- 操作系统：Linux
- 架构：x86_64
- Python：必须为 3.10（生产统一版本；`wheelhouse/` 按 3.10 打 cp 标签，须精确匹配，`install.sh` 会强校验）
- 需要可创建虚拟环境：`python3 -m venv`
- 本机需要有 NVIDIA 驱动，`torch.cuda.is_available()` 必须为真；不要求系统额外安装 CUDA toolkit
- 如果从源机拉物料，需要有 `curl`
- 安装时需要能访问清华 PyPI 镜像，用于拉取 `requirements.txt` 中的轻量依赖

`install.sh` 会完成：

1. 创建或复用 `.venv/`。
2. 安装 `deploy.conf` 中的 `TORCH_PKGS`，来源为本地 `wheelhouse/` 或 `${BASE_URL}/wheelhouse/`。
3. 从清华 PyPI 镜像安装 `requirements.txt` 中的轻量依赖。
4. 修复 `opencv-python` / `opencv-python-headless` 冲突，并将 `numpy` 固定回 `1.26.4`。
5. 校验并部署 ffmpeg 到 `.ffmpeg/`。
6. 校验并部署 MediaMTX 到 `mediamtx/`。
7. 执行安装后自检：`torch`、`numpy`、`cv2`、`ultralytics`、CUDA、ffmpeg、MediaMTX。

### Windows 开发安装

Windows 仅作为 GPU 开发机环境，定位是便利，不作为生产标准。安装入口是原生 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install.ps1
```

Windows 安装策略与 Linux 生产不同：

- `torch` / `torchvision` 从 cu128 PyTorch 镜像在线安装，按本机 Python 版本自动选 wheel，不依赖 `wheelhouse/`。
- 轻量依赖从清华 PyPI 镜像在线安装。
- ffmpeg / MediaMTX 使用 Windows 钉版包；如果配置了 `BASE_URL`，从源机拉取，否则从 `deploy.conf` 中的 `FFMPEG_WIN_URL` / `MEDIAMTX_WIN_URL` 在线下载。
- Windows 包不做 SHA 强校验。
- Python 支持 3.10–3.13（开发宽松，不像生产要求精确 3.10）。

离线拉取源机物料时：

```powershell
$env:BASE_URL="http://<源机IP>:<分发端口>"
.\install.ps1
```

当前 `label-studio` 源机已补齐 `vendor/mediamtx/mediamtx-win-x64.zip`，Windows 开发机可用该源机的 `BASE_URL` 拉取 ffmpeg / MediaMTX 物料。

安装完成后，关键第三方依赖都在项目目录内（Windows 下带 `.exe` 后缀）：

```text
.venv/
.ffmpeg/bin/ffmpeg.exe
mediamtx/mediamtx.exe
mediamtx/mediamtx.yml
```

应用默认使用项目内的 `.ffmpeg/bin/ffmpeg.exe`；独立 RTSP Gateway 的 `mediamtx_gateway/config.ini` 中 `mediamtx_bin = auto` 默认选用项目内的 `mediamtx/mediamtx.exe`，均无需额外配置。不要指向系统安装的 ffmpeg 或 MediaMTX。

### 应用启动前必须配置的运行时变量

数据库、外部接口、网关等变量不是安装变量；它们不影响 `install.sh` 的依赖安装，但会影响后端启动和业务功能。

生产环境使用 `.env`，开发环境使用 `.env.dev`，测试环境使用 `.env.test`。启动脚本通过 `CLEANSIGHT_ENV` 选择配置文件，并一条命令同时拉起 RTSP 网关（含 MediaMTX）与后端：

```bash
./start_backend.sh prod   # 加载 .env，网关+MediaMTX+后端一起起
./start_backend.sh test   # 加载 .env.test
./start_backend.sh dev    # 加载 .env.dev
```

Windows 开发机用 PowerShell 启动脚本，运行时变量键名与生产共用，写在 `.env.dev`：

```powershell
.\start_backend.ps1 dev   # 加载 .env.dev
```

**端口由启动脚本按环境自动分配，不写进 `.env*`。** `.env*` 只放业务参数（DB / 外部接口 URL / 密钥 / 网关 IP 白名单）。基准端口（dev/prod）与 test 偏移如下：

| 用途 | dev / prod | test（+100） |
|------|-----------|--------------|
| 后端 HTTP/WS | 8000 | 8100 |
| 网关对外 RTSP | 8004 | 8104 |
| MediaMTX RTSP（内部） | 18004 | 18104 |
| MediaMTX RTP / RTCP（UDP，内部） | 8002 / 8003 | 8102 / 8103 |

**test 与 prod 同机共存**：二者端口整体错开 100，可同时 `./start_backend.sh prod` 与 `./start_backend.sh test`，互不抢占。dev 与 prod 同端口（分属不同机器，不冲突）。

生产启动前至少应填写：

```dotenv
CLEANSIGHT_DB_HOST=...
CLEANSIGHT_DB_PORT=5432
CLEANSIGHT_DB_NAME=...
CLEANSIGHT_DB_USER=...
CLEANSIGHT_DB_PASSWORD=...

CLEANSIGHT_FILE_PATH_INSERT_URL=...
CLEANSIGHT_ALARM_REPORT_URL=...

CLEANSIGHT_STRICT=1
```

生产环境还建议显式配置：

```dotenv
# 媒体 URL HMAC 签名密钥。未配置时会生成临时密钥，重启后失效。
CLEANSIGHT_MEDIA_TOKEN_SECRET=...

# 如模型目录不使用默认 ./app/data，则配置此项。
CLEANSIGHT_MODEL_PATH=...

# 如需要限制 HTTP API 来源 IP，则配置白名单。
CLEANSIGHT_GATEWAY_ALLOWED_IPS=...
```

### 安装后的自包含路径

安装完成后，关键第三方依赖都在项目目录内：

```text
.venv/
.ffmpeg/bin/ffmpeg
mediamtx/mediamtx
mediamtx/mediamtx.yml
```

MediaMTX 的配置文件 `mediamtx/mediamtx.yml` 随仓库维护，安装脚本只更新二进制，不覆盖配置和 LICENSE。

应用默认使用项目内的 `.ffmpeg/bin/ffmpeg`，独立 RTSP Gateway 也通过 `mediamtx_gateway/config.ini` 的 `mediamtx_bin = auto` 默认选用项目内的 `mediamtx/mediamtx`，均无需额外配置。不要指向系统安装的 ffmpeg 或 MediaMTX。

## 物料供给侧

### build.sh 的定位

`build.sh` 用于构建安装物料：

```bash
./build.sh
```

脚本要求运行在 Linux x86_64，Python 必须为 3.10（与生产一致——`wheelhouse/` 据此打 cp 标签，`build.sh` 会强校验），并且本机有 `python3`、`pip`、`curl`、`sha256sum`。

它不是每次部署都要运行的脚本。一般只在以下情况运行：

- 调整 `torch` / `torchvision` 版本。
- 调整 CUDA 对应的 PyTorch wheel 来源。
- 升级 ffmpeg 或 MediaMTX。
- 首次准备源机物料。

`build.sh` 依赖外部资源下载，资源 URL 可能随时间失效。因此它应被视为“物料重建工具”，不是日常生产发布步骤。稳定版本构建好以后，应复用同一批 `wheelhouse/` 和 `vendor/` 物料。

### build.sh 需要配置的 deploy 变量

构建前确认 `deploy.conf`：

```bash
TORCH_PKGS="torch==2.8.0 torchvision==0.23.0"

FFMPEG_URL="..."
FFMPEG_SHA256="..."
FFMPEG_WIN_URL="..."

MEDIAMTX_URL="..."
MEDIAMTX_SHA256="..."
MEDIAMTX_WIN_URL="..."
```

`BASE_URL` 不参与物料构建，只参与安装拉取。

Linux 生产包必须有 SHA：

- `FFMPEG_SHA256`
- `MEDIAMTX_SHA256`

升级 Linux 二进制时的建议流程：

1. 修改 `FFMPEG_URL` 或 `MEDIAMTX_URL`。
2. 先临时清空对应 SHA。
3. 在 Linux 构建机运行 `./build.sh`。
4. 将脚本打印出的 SHA 回填到 `deploy.conf`。
5. 再运行一次 `./build.sh`，确认 SHA 校验通过。

Windows 包用于开发便利，当前不做 SHA 强校验。

### build.sh 的产物

构建产物如下：

```text
wheelhouse/
  SHA256SUMS
  *.whl

vendor/ffmpeg/
  ffmpeg-linux-x64.tar.xz
  ffmpeg-win-x64.zip

vendor/mediamtx/
  mediamtx-linux-x64.tar.gz
  mediamtx-win-x64.zip
```

这些目录已被 `.gitignore` 忽略，不应提交到 git。

### 启动物料分发服务

源机需要对目标机开放 HTTP 分发服务。HTTP 根目录必须是包含 `wheelhouse/` 和 `vendor/` 的项目目录或物料目录。

临时分发可使用：

```bash
cd /path/to/CleanSightBackend
python3 -m http.server <分发端口> --bind 0.0.0.0
```

然后联系管理员放通源机到目标机的访问，例如：

```text
http://<源机IP>:<分发端口>/
```

目标机安装时使用：

```bash
BASE_URL=http://<源机IP>:<分发端口> ./install.sh
```

脚本会派生以下路径：

```text
${BASE_URL}/wheelhouse/
${BASE_URL}/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
${BASE_URL}/vendor/mediamtx/mediamtx-linux-x64.tar.gz
```

Windows 开发机使用同一源机时，会拉取：

```text
${BASE_URL}/vendor/ffmpeg/ffmpeg-win-x64.zip
${BASE_URL}/vendor/mediamtx/mediamtx-win-x64.zip
```

建议在目标机安装前先验证：

```bash
curl -I http://<源机IP>:<分发端口>/wheelhouse/
curl -I http://<源机IP>:<分发端口>/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
curl -I http://<源机IP>:<分发端口>/vendor/mediamtx/mediamtx-linux-x64.tar.gz
```

长期分发建议交给管理员用 nginx、对象存储或受控内网 HTTP 服务托管，并限制访问来源。物料体积较大，不建议无鉴权长期暴露到公网。

### 当前 label-studio 源机

当前物料源机：

```text
SSH 别名：label-studio
公网 IP：49.234.120.241
内部主机：VM-32-133-ubuntu
用户：ubuntu
```

这台机器同时运行 Label Studio 和物料分发服务。Label Studio 占用 `8080`，所以 CleanSight 物料分发服务使用 `8088`：

```bash
BASE_URL=http://49.234.120.241:8088 ./install.sh
```

分发服务由 systemd 管理：

```ini
# /etc/systemd/system/cleansight-dist.service
ExecStart=/usr/bin/python3 -m http.server 8088 --bind 0.0.0.0 --directory /srv/cleansight-dist
User=ubuntu
Restart=on-failure
```

该 unit 当前是 `static`，没有 `[Install]`，不开机自启。这符合“只在部署窗口手动开启”的设计。

服务根目录只放物料软链，不暴露代码仓库和 `.env`：

```text
/srv/cleansight-dist/
  wheelhouse -> /data/cleansight-offline/wheelhouse
  vendor     -> /data/cleansight-offline/vendor
```

物料实际目录：

```text
/data/cleansight-offline/
```

源机本机自检：

```bash
curl -sI localhost:8088/wheelhouse/SHA256SUMS
```

目标机或外部网络自检：

```bash
curl -I http://49.234.120.241:8088/wheelhouse/SHA256SUMS
curl -I http://49.234.120.241:8088/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
curl -I http://49.234.120.241:8088/vendor/mediamtx/mediamtx-linux-x64.tar.gz
```

当前已就位的固定名物料：

```text
vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
vendor/ffmpeg/ffmpeg-win-x64.zip
vendor/mediamtx/mediamtx-linux-x64.tar.gz
vendor/mediamtx/mediamtx-win-x64.zip
```

Windows MediaMTX 物料 SHA256：

```text
19cd9d1fbb76225380859109175b7547d2e68b4b70858be4fa565604743acf8d
```

部署窗口结束后，如果没有目标机正在安装，应关闭公网暴露：

```bash
sudo systemctl stop cleansight-dist
```

下一次部署前再手动启动：

```bash
sudo systemctl start cleansight-dist
sudo systemctl status cleansight-dist
```

这项服务当前绑定 `0.0.0.0:8088`，明文无鉴权；`ufw` 未启用时主要依赖云安全组限制访问。不要长期裸跑在公网。

### 不走 HTTP 的本地物料模式

如果目标机不通过 `BASE_URL` 拉取，可以把物料直接同步到目标机项目目录：

```bash
rsync -av wheelhouse/ <target>:/path/to/CleanSightBackend/wheelhouse/
rsync -av vendor/ <target>:/path/to/CleanSightBackend/vendor/
```

然后在目标机运行：

```bash
./install.sh
```

这种模式下 `install.sh` 会先校验 `wheelhouse/SHA256SUMS`，再安装。

## 推荐生产部署顺序

1. 在 Linux 构建机确认 `deploy.conf` 中的 PyTorch、ffmpeg、MediaMTX 版本。
2. 仅在需要重建物料时运行 `./build.sh`。
3. 将 `wheelhouse/` 和 `vendor/` 保留在源机，启动 HTTP 分发服务。
4. 让管理员放通目标机访问源机的实际分发端口；当前 `label-studio` 源机是 `8088`。
5. 在目标机配置 `.env` 中的生产运行时变量。
6. 在目标机运行 `BASE_URL=http://49.234.120.241:8088 ./install.sh`，或替换为实际源机地址。
7. 确认安装脚本末尾的 CUDA、ffmpeg、MediaMTX 自检通过。
8. 运行 `./start_backend.sh prod` 一并启动网关、MediaMTX 与后端（端口按环境自动分配）。
9. 在虚拟环境中跑一遍 `test_single_client`，完成端到端验证。
10. 部署窗口结束后停止公网分发服务。

## 部署后验证

`install.sh` 末尾的自检只能证明依赖、CUDA、ffmpeg、MediaMTX 二进制可用。业务链路还需要跑一次单客户端集成测试，验证推流、`/api/start`、推理链路和 `/api/terminate`。

在目标机项目目录执行：

```bash
source .venv/bin/activate
python integration_tests/test_single_client.py --scenario 1 --task_id <任务ID> --duration 30 --no-window
```

如果从另一台机器验证远程后端，增加 `--server`：

```bash
source .venv/bin/activate
python integration_tests/test_single_client.py --scenario 1 --task_id <任务ID> --server <目标机IP> --duration 30 --no-window
```

验证前确认：

- 后端服务已启动，`http://<目标机IP>:8000/health` 可访问（test 环境为 `8100`）。
- MediaMTX Gateway 已启动，RTSP 对外端口 `8004` 可访问（test 环境为 `8104`）。
- 测试视频存在，默认路径是 `test/test_video.mp4`；如不在默认路径，用 `--video_path <路径>` 指定。
- `<任务ID>` 在数据库中可用；脚本在找不到任务时会尝试创建测试任务，因此数据库配置也必须可写。

通过标准：

- 脚本能成功推流到 `rtsp://<目标机IP>:8004/live/<client_id>`。
- `/api/start` 返回成功。
- 持续运行到 `duration` 结束，无异常退出。
- `/api/terminate` 成功清理资源。
