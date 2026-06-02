#!/bin/bash
# GPU 版安装依赖脚本
# 用法: ./install.sh
#
# 说明: ultralytics 依赖 opencv-python，但服务器无 GUI 环境需要 headless 版。
# pip 不知道两者可互替，会同时装两个。本脚本安装后自动清理非 headless 版。

set -e

# 安装系统依赖
echo "Checking system dependencies..."
if ! command -v ffmpeg &>/dev/null; then
    echo "Installing ffmpeg..."
    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg
fi

# 自动创建并激活虚拟环境
if [ ! -f ".venv/bin/activate" ]; then
    echo "Creating virtual environment (.venv)..."
    python3 -m venv .venv
fi
echo "Activating virtual environment..."
source .venv/bin/activate

# 安装源选择：检测到 wheelhouse/ 即全程离线（--no-index），否则联网。
# 离线 wheelhouse 由 build_wheelhouse.sh 一次性生成，详见该脚本头注释。
if [ -d wheelhouse ]; then
    echo "Detected wheelhouse/ — offline install (no network)."
    OFFLINE=1
    SRC="--no-index --find-links wheelhouse"   # torch 与其余依赖统一从本地 wheelhouse 装
else
    OFFLINE=0
    SRC="-i https://pypi.tuna.tsinghua.edu.cn/simple"
fi

# torch：离线从 wheelhouse 装；联网时必须从 PyTorch cu128 源（清华只有捆绑 CUDA 的变体，额外 2-4GB）。
# 注：旧注释「uses system CUDA, no bundled libraries」是反的——cu128 wheel 自带整套 CUDA runtime，
# 只向系统借驱动；见 docs/DEPLOYMENT_VALIDATION.md Phase 2。
echo "Installing PyTorch cu128 (bundles its own CUDA runtime, only uses system driver)..."
if [ "$OFFLINE" = 1 ]; then
    pip install $SRC torch==2.8.0 torchvision==0.23.0
else
    pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
fi

echo "Installing remaining dependencies..."
pip install $SRC -r requirements.txt

# ultralytics 会拉入 opencv-python，它与 headless 版共享 cv2/ 目录文件，
# 卸载非 headless 版会连带删除共享的 cv2 模块文件，导致 headless 版虽有元数据
# 但实际不可用，因此必须把 headless 重新铺一遍（force-reinstall）。
# 关键：force-reinstall 默认会连依赖一起重装，会把 numpy 顶到 2.x（撞 torch ABI），
# 所以用 --no-deps 只重铺 cv2、不碰 numpy；随后显式复位 numpy 以防已被顶。
# $SRC 在离线模式下为 --no-index --find-links wheelhouse，故这两步离线同样成立。
echo "Fixing opencv: removing non-headless and force-reinstalling headless (--no-deps)..."
pip uninstall -y opencv-python opencv-python-headless 2>/dev/null || true
pip install $SRC --no-deps --force-reinstall "opencv-python-headless<4.12.0"
pip install $SRC "numpy==1.26.4"

# 验证 cv2 可正常导入
echo "Verifying cv2 import..."
if python -c "import cv2; print(f'cv2 {cv2.__version__} OK')"; then
    echo "Installation completed successfully."
else
    echo "ERROR: cv2 import failed!" >&2
    exit 1
fi
