#!/bin/bash
# 离线安装：消费 build.sh 产出的静态物料，在目标机一次装齐。
#   核心 torch 闭包 → 从 wheelhouse/ 离线装（带 SHA256SUMS 完整性校验）
#   其余 Python 依赖 → 从清华源在线拉
#   ffmpeg / mediamtx → 校验 vendor/ 里的钉版包后部署到位
#
# 前置：已把 wheelhouse/ 与 vendor/ rsync 到本目录（缺则报错）。版本/URL 全在 deploy.conf。
# 用法: ./install.sh   （全程在项目目录内安装，免 sudo）

set -euo pipefail

cd "$(dirname "$0")"
source deploy.conf

# ── 执行前环境检查 ──
[ "$(uname -s)" = "Linux" ]  || { echo "ERROR: 仅支持 Linux（当前 $(uname -s)）" >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "ERROR: 仅支持 x86_64（当前 $(uname -m)）" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: 缺少 python3" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if (3,10) <= sys.version_info < (3,14) else 1)' \
    || { echo "ERROR: 需要 Python 3.10–3.13（当前 $(python3 -V)）" >&2; exit 1; }
[ -f wheelhouse/SHA256SUMS ] || { echo "ERROR: 缺 wheelhouse/（先在构建机 ./build.sh 再 rsync 过来）" >&2; exit 1; }
[ -d vendor ]                || { echo "ERROR: 缺 vendor/（先在构建机 ./build.sh 再 rsync 过来）" >&2; exit 1; }

# 校验 vendor 物料：deploy.conf 钉了就强制比对，没钉则告警跳过。
verify_sha() {  # $1=文件 $2=期望值
    [ -f "$1" ] || { echo "ERROR: 物料缺失：$1" >&2; exit 1; }
    if [ -n "$2" ]; then
        echo "$2  $1" | sha256sum -c - || { echo "ERROR: SHA256 不匹配：$1" >&2; exit 1; }
    else
        echo "WARN: $1 未钉 SHA256（deploy.conf 为空），跳过校验" >&2
    fi
}

# ── 虚拟环境 ──
if [ ! -f .venv/bin/activate ]; then
    echo "创建虚拟环境 .venv ..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# ── [1] Python 依赖：核心离线 + 其余在线 ──
echo "[1/3] Python 依赖"
echo "      校验 wheelhouse 完整性..."
( cd wheelhouse && sha256sum -c --quiet SHA256SUMS ) \
    || { echo "ERROR: wheelhouse SHA256 校验失败，物料损坏" >&2; exit 1; }
echo "      核心 torch 闭包（离线）..."
pip install --no-index --find-links wheelhouse $TORCH_PKGS
echo "      其余依赖（在线，${PYPI_INDEX_URL}）..."
pip install -r requirements.txt -i "$PYPI_INDEX_URL" --find-links wheelhouse

# ultralytics 会拉入 opencv-python，与 headless 版共享 cv2/ 文件，卸载非 headless 会连带
# 删共享模块。force-reinstall 默认连依赖一起重装会把 numpy 顶到 2.x（撞 torch ABI），
# 故 --no-deps 只重铺 cv2、不碰 numpy；随后显式复位 numpy 以防已被顶。
echo "      修复 opencv headless..."
pip uninstall -y opencv-python opencv-python-headless 2>/dev/null || true
pip install -i "$PYPI_INDEX_URL" --find-links wheelhouse --no-deps --force-reinstall "opencv-python-headless<4.12.0"
pip install -i "$PYPI_INDEX_URL" --find-links wheelhouse "numpy==1.26.4"

# ── [2] ffmpeg → 项目内 .ffmpeg/（与 mediamtx 同为项目内二进制，免 sudo）──
# 必须钉版：ffmpeg 4.x/8.x 对 -hls_fmp4_init_filename 解析差异巨大，见 docs/HLS_TIMELINE_PITFALL.md。
ff_asset="vendor/ffmpeg/$(basename "$FFMPEG_URL")"
echo "[2/3] ffmpeg → .ffmpeg/"
verify_sha "$ff_asset" "$FFMPEG_SHA256"
xz -t "$ff_asset" || { echo "ERROR: ffmpeg 压缩包损坏" >&2; exit 1; }
ff_tmp="$(mktemp -d)"; trap 'rm -rf "$ff_tmp"' EXIT
tar xf "$ff_asset" -C "$ff_tmp"
ff_inner="$(find "$ff_tmp" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"
[ -x "$ff_inner/bin/ffmpeg" ] || { echo "ERROR: 解压结构异常，找不到 bin/ffmpeg" >&2; exit 1; }
rm -rf .ffmpeg
mv "$ff_inner" .ffmpeg

# ── [3] mediamtx → MEDIAMTX_DIR（只取二进制，保留 mediamtx.yml / LICENSE）──
mtx_asset="vendor/mediamtx/$(basename "$MEDIAMTX_URL")"
echo "[3/3] mediamtx → ${MEDIAMTX_DIR}/"
verify_sha "$mtx_asset" "$MEDIAMTX_SHA256"
tar xzf "$mtx_asset" -C "$MEDIAMTX_DIR" mediamtx
chmod +x "${MEDIAMTX_DIR}/mediamtx"

# ── 执行后验证 ──
# 重点验冲突面：import ultralytics 会连带 import cv2，是 opencv-python(非 headless) 与
# headless 共存、以及 numpy 被顶到 2.x（撞 torch ABI）最容易暴露的入口。
echo ""
echo "验证安装..."
# YOLO_CONFIG_DIR 锁死项目内 .ultralytics（与 app/settings.py 一致），令安装期验证也不踩 /tmp 回退。
mkdir -p .ultralytics
YOLO_CONFIG_DIR="$PWD/.ultralytics" python - <<'PY' || { echo "ERROR: Python 依赖自检失败" >&2; exit 1; }
import torch, numpy, cv2, ultralytics
from ultralytics import YOLO  # 触发 ultralytics 完整导入图（含 cv2）
assert torch.cuda.is_available(), "CUDA 不可用"
assert numpy.__version__.startswith("1.26"), f"numpy 被顶到 {numpy.__version__}（撞 torch ABI）"
print(f"torch {torch.__version__} | numpy {numpy.__version__} | cv2 {cv2.__version__} "
      f"| ultralytics {ultralytics.__version__} | CUDA OK")
PY
".ffmpeg/bin/ffmpeg" -version | head -1
"${MEDIAMTX_DIR}/mediamtx" --version

echo ""
echo "─────────────────────────────────────────────────────────────"
echo "安装完成。ffmpeg 钉版已部署到项目内 .ffmpeg/，后端启动自动探测，无需配置 .env。"
echo "─────────────────────────────────────────────────────────────"
