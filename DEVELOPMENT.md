# CleanSight Backend 开发环境部署

本文覆盖开发环境（主要是 Windows GPU 开发机）的安装与物料拉取。生产部署、物料构建（`build.sh`）与源机分发服务见 [DEPLOYMENT.md](DEPLOYMENT.md)。

开发环境与生产的取舍：

- 开发机定位是便利，不作为生产标准；Windows 物料不做 SHA 强校验。
- `torch` / `torchvision` 在线安装，按本机 Python 版本自动选 wheel，不依赖 `wheelhouse/`。
- Python 支持 3.10–3.13（开发宽松）。

## 安装前必须确认的变量

Windows 开发机二进制来源集中在 `deploy.conf`：

```bash
# Windows 开发机二进制来源。
FFMPEG_WIN_URL="..."
MEDIAMTX_WIN_URL="..."
```

如果配置了 `BASE_URL`，ffmpeg / MediaMTX 从源机拉取；否则从上面的 Windows URL 在线下载。源机地址与分发服务见 [DEPLOYMENT.md](DEPLOYMENT.md) 的「物料供给侧」。

## Windows 开发安装

Windows 安装入口是原生 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\install.ps1
```

Windows 安装策略与生产不同：

- `torch` / `torchvision` 从 cu128 PyTorch 镜像在线安装。
- 轻量依赖从清华 PyPI 镜像在线安装。
- ffmpeg / MediaMTX 使用 Windows 钉版包。
- 如果配置了 `BASE_URL`，ffmpeg / MediaMTX 从源机拉取；否则从 `deploy.conf` 中的 Windows URL 在线下载。
- Windows 包不做 SHA 强校验，定位是开发便利，不作为生产标准。
- Python 支持 3.10–3.13（开发宽松；`torch` 在线安装，按本机版本自动选 wheel，不依赖 `wheelhouse/`）。

离线拉取源机物料时：

```powershell
$env:BASE_URL="http://<源机IP>:<分发端口>"
.\install.ps1
```

当前 `label-studio` 源机已补齐 `vendor/mediamtx/mediamtx-win-x64.zip`，Windows 开发机可以使用该源机的 `BASE_URL` 拉取 ffmpeg / MediaMTX 物料。

## 安装后的自包含路径

安装完成后，关键第三方依赖都在项目目录内（Windows 下带 `.exe` 后缀）：

```text
.venv/
.ffmpeg/bin/ffmpeg.exe
mediamtx/mediamtx.exe
mediamtx/mediamtx.yml
```

应用默认使用项目内的 `.ffmpeg/bin/ffmpeg.exe`；独立 RTSP Gateway 的 `mediamtx_gateway/config.ini` 中 `mediamtx_bin = auto` 默认选用项目内的 `mediamtx/mediamtx.exe`，均无需额外配置。不要指向系统安装的 ffmpeg 或 MediaMTX。

## 启动开发后端

```powershell
.\start_backend.ps1 dev    # 加载 .env.dev
```

运行时变量（数据库、外部接口、网关等）与生产共用同一套键名，开发环境写在 `.env.dev`；完整清单见 [DEPLOYMENT.md](DEPLOYMENT.md) 的「应用启动前必须配置的运行时变量」。
