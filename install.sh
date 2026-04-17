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

PIP_MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"

# torch 必须从 PyTorch 官方源安装，清华镜像只有捆绑 CUDA 的版本（额外 2-4 GB）
echo "Installing PyTorch cu128 (uses system CUDA, no bundled libraries)..."
pip install torch==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128

echo "Installing remaining dependencies..."
pip install -r requirements.txt $PIP_MIRROR

# ultralytics 会拉入 opencv-python，卸载它时会连带删除 cv2 模块文件，
# 导致 headless 版虽有元数据但实际不可用，因此必须 force-reinstall。
echo "Fixing opencv: removing non-headless and force-reinstalling headless..."
pip uninstall opencv-python -y 2>/dev/null || true
pip install --force-reinstall "opencv-python-headless<4.12.0" $PIP_MIRROR

# 验证 cv2 可正常导入
echo "Verifying cv2 import..."
if python -c "import cv2; print(f'cv2 {cv2.__version__} OK')"; then
    echo "Installation completed successfully."
else
    echo "ERROR: cv2 import failed!" >&2
    exit 1
fi
