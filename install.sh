#!/bin/bash
# 安装依赖脚本
# 用法: ./install.sh
#
# 说明: ultralytics 依赖 opencv-python，但服务器无 GUI 环境需要 headless 版。
# pip 不知道两者可互替，会同时装两个。本脚本安装后自动清理非 headless 版。

set -e

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Removing opencv-python (non-headless, conflicts with headless)..."
pip uninstall opencv-python -y 2>/dev/null || true

echo "Ensuring opencv-python-headless is installed..."
pip install "opencv-python-headless<4.12.0"

echo "Done. Installed opencv:"
pip show opencv-python-headless | grep -E "Name:|Version:"
