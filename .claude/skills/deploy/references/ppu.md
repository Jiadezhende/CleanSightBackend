# 阿里 PPU 机：无安装脚本，手动路径

适用：CUDA 兼容层为 `/usr/local/PPU_SDK/CUDA_SDK` 的阿里 PPU 实例（如备用机 `8.130.213.80`，SSH 别名 `cleansight-copy-root`，端口 20085）。判别：设备节点 `/dev/alixpu_ppu*`；`source /usr/local/PPU_SDK/envsetup.sh` 后 `ppu-smi` 头部是 `PPU-SMI`（该机**没有** `nvidia-smi`）。

**不要跑 `install.sh`**：它会重建 venv 并装官方 CUDA 版 torch，链接 NVIDIA libcuda，会毁掉厂商预装的 PPU torch。

## 机器事实（2026-11-10 核实，备用机）

**镜像会被平台换掉**：09-12 → 11-10 之间 torch 版本标签、系统 numpy、系统 opencv 都变了。每次部署在预检里重取，以实测为准，下表只作对照。

| 项 | 值 |
|----|----|
| 形态 | PAI DSW 实例（K8s Pod），多租户共享；4 卡 PPU-ZW810E 都有别人的进程 |
| OS / Python | Ubuntu 24.04 / 系统 `/usr/local/bin/python3` 3.12，无 3.10、无 conda |
| torch | `2.10.0`，CUDA 13.0 兼容层（09-12 时版本号带 `+ppu2.1.0`，现在不带） |
| numpy | 系统 1.26.0（09-12 时是 2.3.5）。**venv 内不装 numpy，跟系统走** |
| 系统 opencv / matplotlib | 只有 `opencv-python-headless` 4.13；没有 GUI 版 opencv，没有 matplotlib |
| 工具 | git / curl / xz 有；无 tmux、无 systemd |

### 存储：代码、venv、数据都放 `/mnt/workspace`

| 挂载 | 类型 | 容器重启后 | 结论 |
|------|------|-----------|------|
| `/`（含 `/root`、`/tmp`） | overlay 根盘 | **丢失** | 不放任何部署物（2026-11 一次重启丢了整套部署） |
| `/mnt/workspace`（同 `/mnt/nas`） | CPFS 共享 NAS，POSIX 完整 | 保留 | **全放这里**。多租户共用、常年 97%+，部署前 `df -h` 看余量 |
| `/mnt/data` | ossfs2（OSS FUSE） | 保留 | **不能放数据**：不支持随机写，`np.savez`（推理缓存、时序概率）和 ffmpeg 写 mp4（送标 / 步骤导出）必坏；写入中的文件对读者不可见 |

## 部署布局

prod / test 各一个目录、各自的 venv 与本地端口改动；数据用默认 `./database`，`.env*` 不写 `CLEANSIGHT_STORAGE_DIR`。

```text
/mnt/workspace/CleanSightBackend-prod   ./run_ppu.sh → start_backend.sh prod → .env
  后端 20018 / RTSP 20019 / MediaMTX 18004 / RTP,RTCP 8002,8003
/mnt/workspace/CleanSightBackend-test   ./run_ppu.sh → start_backend.sh test → .env.test
  后端 20016 / RTSP 20017 / MediaMTX 18104 / RTP,RTCP 8102,8103
```

- 卡号：按部署时 `ppu-smi` 选占用最少的卡，prod / test 分卡，写进各自 `.env*` 的 `CLEANSIGHT_CUDA_DEVICE`（外层 `CUDA_VISIBLE_DEVICES` 管不到推理子进程）。2026-11-10 为 prod 3、test 1。
- 端口：平台只 1:1 映射 `20010–20020` 段，容不下默认端口与 test +2 偏移，所以每个目录的 `start_backend.sh` 写绝对端口、`OFFSET=0`，留 git 本地 diff。

## 安装

远程长任务（clone / pip / pytest）一律 `setsid nohup bash -c "..." > log 2>&1 </dev/null &` 后台跑，30–60s 轮询一次。少了 `setsid` 或 `</dev/null`，SSH 会挂到子进程结束。

### 1. 取代码：浅克隆

```bash
cd /mnt/workspace
git clone --depth 1 --single-branch -b <分支> https://github.com/Jiadezhende/CleanSightBackend.git CleanSightBackend-prod
cp -a CleanSightBackend-prod CleanSightBackend-test
```

全量 clone（~95MB）在该机约 70KB/s，15 分钟超时失败；浅克隆约 14MB、3 分钟。浅克隆可以正常 `git pull`。

### 2. 端口改绝对值（每个目录一次）

```bash
set -- 20018 20019 18004 8002 8003     # test: 20016 20017 18104 8102 8103
sed -i -E "s/^BASE_BACKEND=[0-9]+/BASE_BACKEND=$1/; s/^BASE_PROXY=[0-9]+/BASE_PROXY=$2/; \
  s/^BASE_INTERNAL=[0-9]+/BASE_INTERNAL=$3/; s/^BASE_RTP=[0-9]+/BASE_RTP=$4/; s/^BASE_RTCP=[0-9]+/BASE_RTCP=$5/; \
  s/^    test\) OFFSET=2 ;;/    test) OFFSET=0 ;;/" start_backend.sh
```

### 3. venv

```bash
source /usr/local/PPU_SDK/envsetup.sh                     # 每次必须，否则 PPU 内核编译 abort
/usr/local/bin/python3 -m venv --system-site-packages .venv   # 必须带，否则 PPU torch 不可见
I=https://mirrors.aliyun.com/pypi/simple/                 # 清华源对部分 wheel 返回 403
echo "numpy==$(/usr/local/bin/python3 -c 'import numpy;print(numpy.__version__)')" > /tmp/np-pin.txt
.venv/bin/pip install -i $I -c /tmp/np-pin.txt -r requirements/ppu.txt
.venv/bin/pip uninstall -y opencv-python opencv-python-headless
.venv/bin/pip install -i $I --no-deps --force-reinstall "opencv-python-headless<4.12.0"
```

- **`-c` 钉系统 numpy 不能省**：系统缺 matplotlib 和 GUI 版 opencv，ultralytics 补装时会拉最新版（`opencv-python` 5.x、`contourpy` 1.4 都要求 numpy≥2），不钉就往 venv 装 numpy 2.x，压掉系统那份。
- **卸 GUI 版、`--no-deps` 重装 headless**：和 `install.sh` 同理，两者共用 `cv2/`。pip 只卸 venv 内的副本，不会动系统包。
- 末尾一串 `vllm ... requires ..., but you have ...` 是系统里别人的 vllm，不用管。
- **不要把依赖装进系统 Python**：`/usr/local/bin/python3` 是多租户共用的。

### 4. ffmpeg / MediaMTX

从源机拉一次，复刻 `install.sh` 的 [2]、[3] 两段，再复制到两个目录：

```bash
BASE=<用户给的源机地址>        # 没给就用 install.sh 开头的默认值
curl -fL -o /tmp/ff.tar.xz $BASE/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz && xz -t /tmp/ff.tar.xz
mkdir -p /tmp/ff && tar xf /tmp/ff.tar.xz -C /tmp/ff && rm -rf .ffmpeg && cp -a /tmp/ff/ffmpeg-* .ffmpeg
curl -fL -o /tmp/mtx.tar.gz $BASE/vendor/mediamtx/mediamtx-linux-x64.tar.gz && gzip -t /tmp/mtx.tar.gz
tar xzf /tmp/mtx.tar.gz -C mediamtx mediamtx && chmod +x mediamtx/mediamtx
```

### 5. 启停脚本（每个目录一份，不进 git）

`run_ppu.sh`（`ROLE` 按目录改成 prod / test）：

```bash
#!/bin/bash
cd "$(dirname "$0")" || exit 1
ROLE=prod
if [ -s .run.pid ] && kill -0 "$(cat .run.pid)" 2>/dev/null; then
    echo "已在运行（pgid $(cat .run.pid)），先 ./stop_ppu.sh"; exit 1
fi
source /usr/local/PPU_SDK/envsetup.sh >/dev/null 2>&1
export YOLO_CONFIG_DIR="$PWD/.ultralytics"
setsid nohup ./start_backend.sh "$ROLE" > backend.log 2>&1 < /dev/null &
echo $! > .run.pid
echo "started $ROLE, pgid $!"
```

`stop_ppu.sh`：按进程组停整套（后端 + 网关 + MediaMTX + 推理子进程）：

```bash
#!/bin/bash
cd "$(dirname "$0")" || exit 1
[ -s .run.pid ] || { echo "无 .run.pid，未由 run_ppu.sh 启动"; exit 0; }
PG=$(cat .run.pid)
kill -TERM -- "-$PG" 2>/dev/null || { echo "进程组 $PG 已不存在"; rm -f .run.pid; exit 0; }
for _ in $(seq 20); do pgrep -g "$PG" >/dev/null || break; sleep 1; done
pgrep -g "$PG" >/dev/null && { echo "20s 未退出，SIGKILL"; kill -KILL -- "-$PG" 2>/dev/null; }
rm -f .run.pid
echo "stopped pgid $PG"
```

```bash
chmod +x run_ppu.sh stop_ppu.sh
printf "run_ppu.sh\nstop_ppu.sh\n.run.pid\n" >> .git/info/exclude
```

### 6. 自检

```bash
source /usr/local/PPU_SDK/envsetup.sh; mkdir -p .ultralytics
CUDA_VISIBLE_DEVICES=<卡号> YOLO_CONFIG_DIR=$PWD/.ultralytics .venv/bin/python -c "
import torch, numpy, cv2, ultralytics; from ultralytics import YOLO
assert torch.cuda.is_available()
torch.nn.Conv2d(3, 8, 3).cuda()(torch.randn(1, 3, 64, 64, device='cuda')); torch.cuda.synchronize()
assert numpy.__file__.startswith('/usr/local/lib/'), 'numpy 被装进了 venv: ' + numpy.__file__
assert 'NONE' in [l for l in cv2.getBuildInformation().splitlines() if 'GUI:' in l][0], 'cv2 不是 headless'
print(torch.__version__, numpy.__version__, cv2.__version__, ultralytics.__version__)"
.ffmpeg/bin/ffmpeg -version | head -1; mediamtx/mediamtx --version
```

`mkdir -p .ultralytics` 不能省：目录不存在时 ultralytics 判为不可写，回退写 `/tmp/Ultralytics`。

pytest 不需要 `.env.dev`，用环境变量给不可达占位即可（缺必填项时 conftest 导入阶段就抛 `ValidationError`）：

```bash
CLEANSIGHT_DB_HOST=db.invalid CLEANSIGHT_DB_PORT=5432 CLEANSIGHT_DB_NAME=x CLEANSIGHT_DB_USER=x CLEANSIGHT_DB_PASSWORD=x \
CLEANSIGHT_ALARM_REPORT_URL=http://alarm.invalid/gdmp/v1/api/nt/alarm_report \
YOLO_CONFIG_DIR=$PWD/.ultralytics .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
```

## 运行

先按 [runtime-config.md](runtime-config.md) 配好对应角色的 `.env*`（网关保持开启，`CLEANSIGHT_CUDA_DEVICE` 锁卡），再在目录里 `./run_ppu.sh`，停服务用 `./stop_ppu.sh`。健康检查在本机做：

```bash
curl -s -m 5 http://localhost:<后端口>/health/status | head -c 300; echo
ss -ltn | grep -E ":(<RTSP口>|<MediaMTX口>)\b"
```

启动通过后按 runtime-config.md「模型权重」一节核对。`app/data/` 整个不在 git 里（含 `backbones/dinov2_vits14/`），新机上全部要用户补。

## 已知坑

1. **不 source `envsetup.sh` 直接 abort**：简单 matmul 能跑，YOLO 卷积走 RTC 编译即报 `Both PPU_SDK and PPU_HOME are not exist`（exit 134）。
2. **fp16 比 fp32 慢**（7.9 ms vs 6.3 ms，YOLO11n 640）：此卡没有 tensor-core 级 FP16 加速。
3. **首次冷启要现场编译内核**：`/usr/local/PPU_SDK/rtccache/` 未命中时，每个卷积配置约 1.4 秒，日志刷大量 `No cache file exist`；缓存全局共享，只付一次。
4. **端口映射要每次实测**：`20013`、`20020` 已被别的租户占用。2026-11-10 容器重建后，20016–20019 从外部连不进本容器（无论容器里有没有监听都不通），待平台确认映射。验法：容器里临时起 `python3 -m http.server <口> --bind 0.0.0.0`，从外部用 `curl --noproxy '*'` 直连。本地开着 Clash 等代理（`127.0.0.1:7890`）时不加 `--noproxy`，会得到 502 或空响应，造成误判。
5. **多租户**：同机别人在跑 uvicorn / gunicorn，停服务只用 `stop_ppu.sh`，**禁用** `pkill -f "app.main"` / `pkill -f uvicorn`（linux.md 坑 6 的通用做法在这台机器上会误杀别人）。端口被占也可能来自同 netns 的其他容器，本 PID 空间看不到。
6. **容器重启丢根盘**：`/root`、`/tmp`、所有进程都没了；`/mnt/workspace` 里的代码、venv、数据不丢，重启后在各目录重新执行 `./run_ppu.sh`。

## 部署完成检查清单

- [ ] 代码、venv、数据都在 `/mnt/workspace`，没有任何部署物在根盘或 `/mnt/data`
- [ ] 浅克隆目标分支，`start_backend.sh` 端口为绝对值、`OFFSET=0`
- [ ] venv 带 `--system-site-packages`；numpy 来自系统（`/usr/local/lib/...`），cv2 为 headless；PPU 卷积冒烟通过
- [ ] `.ffmpeg/bin/ffmpeg` 为 n7.1.4，`mediamtx/mediamtx` 为 v1.15.5
- [ ] `run_ppu.sh` / `stop_ppu.sh` 就位且已加入 `.git/info/exclude`
- [ ] test 目录 `pytest tests/` 全绿
- [ ] 端口在平台映射窗口内且外部实测可达，外部端口号等于内部端口号
- [ ] `.env*` 键检查全「已填」，`CLEANSIGHT_CUDA_DEVICE` 已锁卡，全程未读值
- [ ] `/health/status` 返回 `running`，对外 RTSP 口与 MediaMTX 口都在听
- [ ] 权重核对无缺失（或缺失清单已交给用户）
