#!/bin/bash
# 离线安装：在目标机上把 Python 依赖 + ffmpeg + mediamtx 一次装齐。
# 优先用 ./build.sh 备好的 wheelhouse/ 和 vendor/（全离线、0 下载）；缺料则联网兜底。
#
# 用法: ./install.sh   （GPU / torch cu128）
#
# 三步：
#   ① venv + pip 装 Python 依赖（含 ultralytics 的 opencv-headless 修复）
#   ② scripts/install_ffmpeg.sh 装钉版 ffmpeg（自动用 vendor/ffmpeg/ 里的 tarball）
#   ③ scripts/install_mediamtx.sh 装 mediamtx（自动用 vendor/mediamtx/ 里的包）
#
# 注意：以普通用户运行即可；第 ② 步写 /opt 需要 sudo，脚本内部会按需提权。

set -e

# ── 自动创建并激活虚拟环境 ──
if [ ! -f ".venv/bin/activate" ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi
echo "Activating virtual environment..."
source .venv/bin/activate

# ── 安装源：检测到 wheelhouse/ 即全程离线（--no-index），否则联网 ──
if [ -d wheelhouse ]; then
    echo "Detected wheelhouse/ — offline install (no network)."
    OFFLINE=1
    SRC="--no-index --find-links wheelhouse"
else
    OFFLINE=0
    SRC="-i https://pypi.tuna.tsinghua.edu.cn/simple"
fi

# ─────────────────────────────────────────────────────────────────────
# ① Python 依赖
# ─────────────────────────────────────────────────────────────────────
echo "[1/3] Python 依赖"
if [ "$OFFLINE" = 1 ]; then
    # 离线：torch 与其余依赖统一从本地 wheelhouse 装
    pip install $SRC torch==2.8.0 torchvision==0.23.0
else
    # 联网：torch 必须从 PyTorch cu128 源（清华只有捆绑 CUDA 的变体，额外 2-4GB）；
    #       cu128 wheel 自带整套 CUDA runtime，只向系统借驱动。
    pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
fi
pip install $SRC -r requirements.txt

# ultralytics 会拉入 opencv-python，它与 headless 版共享 cv2/ 目录文件，卸载非 headless
# 会连带删除共享的 cv2 模块文件。force-reinstall 默认会连依赖一起重装、把 numpy 顶到 2.x
# （撞 torch ABI），故用 --no-deps 只重铺 cv2、不碰 numpy；随后显式复位 numpy 以防已被顶。
echo "      修复 opencv headless..."
pip uninstall -y opencv-python opencv-python-headless 2>/dev/null || true
pip install $SRC --no-deps --force-reinstall "opencv-python-headless<4.12.0"
pip install $SRC "numpy==1.26.4"
python -c "import cv2; print('cv2', cv2.__version__, 'OK')" \
    || { echo "ERROR: cv2 import failed!" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────
# ② ffmpeg 钉版（写 /opt 需 sudo；脚本自动发现 vendor/ffmpeg/ 里的 tarball）
# ─────────────────────────────────────────────────────────────────────
echo "[2/3] ffmpeg 钉版（scripts/install_ffmpeg.sh）"
sudo bash scripts/install_ffmpeg.sh

# ─────────────────────────────────────────────────────────────────────
# ③ mediamtx（自动用 vendor/mediamtx/ 里的包）
# ─────────────────────────────────────────────────────────────────────
echo "[3/3] mediamtx（scripts/install_mediamtx.sh）"
bash scripts/install_mediamtx.sh

echo ""
echo "─────────────────────────────────────────────────────────────"
echo "安装完成。最后一步：在 .env 写入 ffmpeg 钉版路径"
echo "  CLEANSIGHT_FFMPEG_PATH=/opt/ffmpeg-static/bin/ffmpeg"
echo "─────────────────────────────────────────────────────────────"
