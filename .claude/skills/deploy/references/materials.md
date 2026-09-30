# 物料：源机分发与 build.sh 重建

三类物料、三条来源，每条只有一个出口：

```text
构建机 ./build.sh（开头配置块：上游 URL + TORCH_PKGS）
   ├─ wheelhouse/   torch 闭包 + SHA256SUMS（自动生成，~6GB，cp310）
   └─ vendor/       ffmpeg / mediamtx 的 linux + win 四个包
         │ rsync
         ▼
源机 http://49.234.120.241:8088（systemd cleansight-dist，部署时起、部署完关）
         ├─ install.sh   torch ← wheelhouse（--require-hashes 逐 wheel 校）；ffmpeg / mediamtx ← vendor
         ├─ install.ps1  ffmpeg / mediamtx ← vendor（torch 走 cu128 索引在线）
         └─ PPU 手动     ffmpeg / mediamtx ← vendor
```

## 源机

```text
SSH 别名：label-studio        公网 IP：49.234.120.241
内部主机：VM-32-133-ubuntu    用户：ubuntu
分发端口：8088               （8080 被 Label Studio 占用）
```

分发服务由 systemd 管理，`static`、**不开机自启**，只在部署窗口手动开关：

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
curl -I http://49.234.120.241:8088/wheelhouse/SHA256SUMS
curl -I http://49.234.120.241:8088/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz
curl -I http://49.234.120.241:8088/vendor/mediamtx/mediamtx-linux-x64.tar.gz
```

已就位的固定名物料（`install.sh` / `install.ps1` 从 `BASE_URL` 派生这些路径）：

```text
vendor/ffmpeg/ffmpeg-linux-x64.tar.xz     vendor/ffmpeg/ffmpeg-win-x64.zip
vendor/mediamtx/mediamtx-linux-x64.tar.gz vendor/mediamtx/mediamtx-win-x64.zip
```

该服务绑 `0.0.0.0:8088`、明文无鉴权，`ufw` 未启用时只靠云安全组挡。**不要长期裸跑在公网**；长期分发应交管理员用 nginx / 对象存储托管并限制来源。

## build.sh：只在这几种情况跑

- 调整 torch / torchvision 版本或 CUDA wheel 来源
- 升级 ffmpeg 或 MediaMTX
- 首次准备源机物料

其余时候复用同一批 `wheelhouse/` 与 `vendor/`。它依赖外部 URL 下载（GitHub、南大镜像），地址可能失效，所以是「物料重建工具」不是日常发布步骤。

```bash
./build.sh   # 需 Linux x86_64 + Python 3.10（与生产一致）+ python3/pip/curl/sha256sum
```

逐物料幂等：文件在就跳过下载，中断后可安全重跑（`.part` 断点续传）。torch 闭包下完会用 `pip install --dry-run --no-index` 模拟目标机全新 venv 离线解析，不完整就报错，避免把半包封进 SHA256SUMS。

产物已在 `.gitignore`，不要提交：

```text
wheelhouse/SHA256SUMS + *.whl
vendor/ffmpeg/{ffmpeg-linux-x64.tar.xz, ffmpeg-win-x64.zip}
vendor/mediamtx/{mediamtx-linux-x64.tar.gz, mediamtx-win-x64.zip}
```

## 升级 ffmpeg / MediaMTX

1. 改 `build.sh` 开头的 `FFMPEG_URL` / `FFMPEG_WIN_URL`（或 `MEDIAMTX_*`）
2. 删掉 `vendor/` 下的旧包（幂等逻辑文件在就跳过）
3. 在构建机跑 `./build.sh`
4. `rsync -av vendor/ label-studio:/data/cleansight-offline/vendor/`

ffmpeg 必须钉版：4.x / 8.x 对 `-hls_fmp4_init_filename` 解析差异巨大，见 `docs/kb/DESIGN_HLS_TIMELINE.md`。vendor 不做 SHA 钉版比对，包只在构建机下载一次、经源机分发，下坏了由 install 侧的 `xz -t` / `gzip -t` 当场抓出来。

## 升级 torch

版本出现在两处，**一起改**：`build.sh` 开头的 `TORCH_PKGS`（产出 prod 的 wheelhouse）与 `requirements/gpu.txt`（Windows 在线装）。改完重跑 `build.sh`，`rsync -av wheelhouse/ label-studio:/data/cleansight-offline/wheelhouse/`。`wheelhouse/SHA256SUMS` 由 build.sh 自动生成，是 6GB wheel 走 HTTP 流式安装时唯一的完整性保障。

## 配置在哪：每个脚本开头一块

没有共享的配置文件（原 `deploy.conf` 已删）：

| 脚本 | 配置块 |
|---|---|
| `build.sh` | 上游 `FFMPEG_URL` / `MEDIAMTX_URL` 与两个 `*_WIN_URL`、`TORCH_PKGS`、torch 索引 |
| `install.sh` | 源机 `BASE_URL`、清华源地址。不含 torch 版本，wheelhouse 目录本身即钉版 |
| `install.ps1` | 源机 `BASE_URL`、清华源地址。torch 版本与 cu128 索引在 `requirements/gpu.txt` |

依赖清单按部署路径分：`requirements/base.txt` 是平台无关底座，`prod.txt` / `gpu.txt` / `ppu.txt` 各 `-r base.txt` 后只补自己那份 numpy 与 torch。改依赖只改 `base.txt`，除非改的就是 torch / numpy 本身。
