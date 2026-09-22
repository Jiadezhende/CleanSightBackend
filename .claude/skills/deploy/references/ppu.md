# 阿里 PPU 机：无安装脚本，手动路径

适用：CUDA 兼容层为 `/usr/local/PPU_SDK/CUDA_SDK` 的阿里 PPU 实例（如备用机 `8.130.213.80`，SSH 端口 20085）。判别：`nvidia-smi` 头部是 `PPU-SMI`，设备节点 `/dev/alixpu_ppu*`。

**不要跑 `install.sh`**：它会重建 venv 并装官方 CUDA 版 torch，链接 NVIDIA libcuda，会毁掉厂商预装的 PPU torch。

## 机器事实（2026-09-12 核实，备用机）

| 项 | 值 |
|----|----|
| OS / Python | Ubuntu 24.04 / 系统 Python 3.12，无 3.10、无 conda |
| torch | `2.10.0+ppu2.1.0` 预装于系统 site-packages（配套 torchvision / torchaudio 同版） |
| numpy | 2.3.5，PPU torch 按 numpy 2.x 编译，**绝不可降到 1.26.4** |
| 形态 | K8s Pod，共享机器，同时跑着其他项目 |

## 安装

```bash
source /usr/local/PPU_SDK/envsetup.sh          # 每次必须，否则 PPU 内核编译 abort
/usr/local/bin/python3 -m venv --system-site-packages .venv   # 必须带 --system-site-packages，否则 PPU torch 不可见
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements/ppu.txt
```

`requirements/ppu.txt` 相对 `base.txt` 只做减法：不装 torch、不装 numpy，两者用系统的。`opencv-python-headless` 必须装进 venv 以 shadow 系统那份 QT5 GUI 构建。用阿里源，清华源对部分 wheel 返回 403。

ffmpeg / MediaMTX 手动从源机拉（复刻 `install.sh` 的 [2]、[3] 两段）：

```bash
BASE=http://49.234.120.241:8088
curl -fL -o /tmp/ff.tar.xz $BASE/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz && xz -t /tmp/ff.tar.xz
mkdir -p /tmp/ff && tar xf /tmp/ff.tar.xz -C /tmp/ff && rm -rf .ffmpeg && mv /tmp/ff/ffmpeg-* .ffmpeg
curl -fL -o /tmp/mtx.tar.gz $BASE/vendor/mediamtx/mediamtx-linux-x64.tar.gz && gzip -t /tmp/mtx.tar.gz
tar xzf /tmp/mtx.tar.gz -C mediamtx mediamtx && chmod +x mediamtx/mediamtx
```

自检：

```bash
source /usr/local/PPU_SDK/envsetup.sh
CUDA_VISIBLE_DEVICES=3 YOLO_CONFIG_DIR=$PWD/.ultralytics .venv/bin/python -c \
  "import torch,numpy,cv2,ultralytics; assert torch.cuda.is_available(); print(torch.__version__, numpy.__version__, cv2.__version__)"
.ffmpeg/bin/ffmpeg -version | head -1; mediamtx/mediamtx --version
```

## 运行

每次启动前的环境：

```bash
source /usr/local/PPU_SDK/envsetup.sh
export CUDA_VISIBLE_DEVICES=<单卡号>    # 共享机器，锁单卡；推理子进程按此变量选卡
export YOLO_CONFIG_DIR=$PWD/.ultralytics
./start_backend.sh <prod|test|dev>
```

角色与 `.env*` 见 [runtime-config.md](runtime-config.md)。

## 已知坑

1. **不 source `envsetup.sh` 直接 abort**：简单 matmul 能跑，YOLO 卷积走 RTC 编译即 `Both PPU_SDK and PPU_HOME are not exist`（exit 134）。
2. **fp16 比 fp32 慢**（7.9 ms vs 6.3 ms，YOLO11n 640）：此卡无 tensor-core 级 FP16 加速，配置里关掉半精度。
3. **首次冷启要现场编译内核**：`/usr/local/PPU_SDK/rtccache/` 未命中时每个卷积配置约 1.4 秒，刷大量 `No cache file exist`，与「预热约 2s」差一个数量级；缓存全局共享，只付一次。
4. **端口受平台映射限制**：备用机对外只开 `20016–20020` 五个口且 1:1 映射，容不下默认端口和「test +2」偏移。做法是 prod 与 test 各一个目录、各自的 `start_backend.sh` 写绝对端口、`OFFSET=0`（详见 `docs/update/20260915_BACKUP_HOST_DUAL_ENV.md`）。改过的脚本在部署机留 git 本地 diff，`git pull` 时手动处理。
5. **8000 被同 netns 的其他容器占着**，本 PID 空间看不到进程、不可回收，改端口避开。

## 部署完成检查清单

- [ ] venv 带 `--system-site-packages`，`torch.__version__` 含 `+ppu`，numpy 仍是 2.x
- [ ] `.ffmpeg/bin/ffmpeg` 为 n7.1.4，`mediamtx/mediamtx` 为 v1.15.5
- [ ] 启动前已 `source envsetup.sh` 并锁 `CUDA_VISIBLE_DEVICES`
- [ ] 端口在平台映射窗口内，外部端口号等于内部端口号
- [ ] `/health/status` 返回 `running`，对外 RTSP 口与 MediaMTX 口都在听
