#!/bin/bash
# 构建 wheelhouse（Linux / GPU cu128）—— 把 torch 全家桶只下载一次，之后全部离线安装
#
# 背景：torch+cu128 全家桶约 6GB，国内镜像也就 ~1.2MB/s，下一次要 1-2h。
# 本脚本把所有 wheel 一次性下到 ./wheelhouse/，之后 install.sh 检测到该目录即走
# `--no-index --find-links wheelhouse` 离线装（秒级），换机器把 wheelhouse/ rsync 过去即可。
#
# 双平台说明：torch 在 Linux / Windows 依赖图不同（Linux 有 triton + 独立 nvidia-*-cu12；
# Windows 把 CUDA 打进 torch wheel）。在 Linux 上跑本脚本、在 Windows 上跑 build_wheelhouse.ps1，
# 把两个 wheelhouse/ 目录合并（同名纯 Python wheel 自然去重，平台相关 wheel 各自带平台 tag 共存），
# 合并后的单一 wheelhouse/ 在两平台都能 `--no-index` 离线装，pip 自动挑匹配平台的 wheel。
#
# 用法: ./build_wheelhouse.sh

set -e

WHEELHOUSE="wheelhouse"

# torch 镜像：南京大学（已实测可用）。如换源在此改。注意必须是 cu128 的 simple index。
TORCH_INDEX="https://mirror.nju.edu.cn/pytorch/whl/cu128"
# 其余纯依赖走清华源
PYPI="https://pypi.tuna.tsinghua.edu.cn/simple"

mkdir -p "$WHEELHOUSE"

# ① torch / torchvision 的完整闭包从 cu128 源下载
#    （含 nvidia-*-cu12 / triton / sympy / jinja2 / markupsafe 等，全部由 pytorch 索引自带，自洽）。
#    单独下 torch 闭包不会触发之前那个 MarkupSafe 解析死锁——死锁源于把 requirements 里
#    不在 cu128 索引上的纯包也丢给 --index-url cu128 解析。这里只让它解析 torch 自己的闭包。
echo "[1/2] Downloading torch cu128 closure from $TORCH_INDEX ..."
pip download torch==2.8.0 torchvision==0.23.0 \
    --index-url "$TORCH_INDEX" -d "$WHEELHOUSE"

# ② 其余依赖从清华源下载；torch/torchvision 用 --find-links 命中①已下好的 cu128 wheel，
#    避免 ultralytics 的 torch 依赖把 torch 从清华源（捆绑 CUDA 的变体）再拉一份。
echo "[2/2] Downloading remaining requirements from $PYPI ..."
REQ_NO_TORCH="$(mktemp)"
grep -vE '^[[:space:]]*(torch|torchvision)[[:space:]]*==' requirements.txt > "$REQ_NO_TORCH"
pip download -r "$REQ_NO_TORCH" \
    --find-links "$WHEELHOUSE" --index-url "$PYPI" -d "$WHEELHOUSE"
rm -f "$REQ_NO_TORCH"

echo ""
echo "Done. wheelhouse/ 共 $(ls -1 "$WHEELHOUSE" | wc -l | tr -d ' ') 个 wheel，$(du -sh "$WHEELHOUSE" | cut -f1)。"
echo "下一步：在 Windows 机上跑 build_wheelhouse.ps1，把其产物合并进同一个 wheelhouse/，"
echo "之后两平台都用 ./install.sh / .\\install.ps1 离线安装。"
