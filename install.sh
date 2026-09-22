#!/bin/bash
# 安装：在目标机一次装齐。三类物料各有一条来源，无多源 fallback：
#   核心 torch 闭包    → 源机 ${BASE_URL}/wheelhouse/，pip 流式拉、不落盘，逐 wheel 校 SHA
#   其余 Python 依赖   → 清华源在线拉（requirements/prod.txt）
#   ffmpeg / mediamtx → 源机 ${BASE_URL}/vendor/，解包到项目内 .ffmpeg/ 与 mediamtx/
#
# 三者统一走源机，不再有「本地物料」与「上游在线下载」两条旁路——保证每台机器装到的
# 是同一批物料。源机物料由构建机 ./build.sh 产出后 rsync 过去。
# 用法: ./install.sh   （全程在项目目录内安装，免 sudo）
#       换源机：BASE_URL=http://<IP>:<端口> ./install.sh

set -euo pipefail

cd "$(dirname "$0")"

# ══════════════ 配置 ══════════════
# 源机物料基址。写死默认值，环境变量可临时覆盖（换源机、或指向本机起的临时 HTTP 服务）。
BASE_URL="${BASE_URL:-http://49.234.120.241:8088}"
# 在线镜像。
PYPI_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
# ═════════════════════════════════

# ── 执行前环境检查 ──
[ "$(uname -s)" = "Linux" ]  || { echo "ERROR: 仅支持 Linux（当前 $(uname -s)）" >&2; exit 1; }
[ "$(uname -m)" = "x86_64" ] || { echo "ERROR: 仅支持 x86_64（当前 $(uname -m)）" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: 缺少 python3" >&2; exit 1; }
# 生产统一 Python 3.10：wheelhouse 按构建机 3.10 打 cp 标签，生产须精确一致，否则 --no-index 找不到匹配 wheel。
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3,10) else 1)' \
    || { echo "ERROR: 生产要求 Python 3.10（当前 $(python3 -V)）" >&2; exit 1; }
[ -n "$BASE_URL" ] \
    || { echo "ERROR: BASE_URL 为空（源机地址是必需的，见本脚本开头的配置块）" >&2; exit 1; }

# 极简下载（无多源 fallback）。
dl() {  # $1=目标路径 $2=URL
    command -v curl >/dev/null || { echo "ERROR: 需要 curl 才能从 URL 拉物料" >&2; exit 1; }
    mkdir -p "$(dirname "$1")"
    echo "      下载 $2"
    curl -fL --retry 3 -o "$1" "$2" || { echo "ERROR: 下载失败：$2" >&2; exit 1; }
}

# ── 虚拟环境 ──
if [ ! -f .venv/bin/activate ]; then
    echo "创建虚拟环境 .venv ..."
    python3 -m venv .venv
fi
source .venv/bin/activate

# ── [1] Python 依赖：torch 闭包（源机 HTTP 流式）+ 其余在线 ──
echo "[1/3] Python 依赖"
# --find-links 接受 HTTP 目录页；--no-cache-dir 令流式拉的 wheel 不落 7G 缓存。
# **本脚本不指定 torch 版本**：wheelhouse 里就那一版，它本身即钉版（由 build.sh 产出）。
torch_links="${BASE_URL%/}/wheelhouse/"
echo "      核心 torch 闭包（${torch_links}）..."
{
    # wheel 不落盘、无法事后 sha256sum -c，故把源机 SHA256SUMS 转成 pip --require-hashes
    # 清单——pip 边流式下载边逐 wheel 校 SHA，任一校不过即中止。
    sums_tmp="$(mktemp -d)"
    dl "$sums_tmp/SHA256SUMS" "${torch_links}SHA256SUMS"
    # SHA256SUMS 行：<sha>  <wheel名>。wheel 名内 distribution 的 '-' 已转 '_'，按 '-' 切 f1=包名 f2=版本，
    # 同名同版本若有多份则聚合成一行多 --hash，还原成 pip 要求的 name==version --hash=sha256:<sha>。
    awk '{ f=$2; sub(/^\*/,"",f); if (split(f,a,"-")<2) next;
           k=a[1]"=="a[2]; h[k]=h[k]" --hash=sha256:"$1 }
         END { for (k in h) print k h[k] }' \
        "$sums_tmp/SHA256SUMS" > "$sums_tmp/torch-reqs.txt"
    [ -s "$sums_tmp/torch-reqs.txt" ] \
        || { echo "ERROR: 源机 wheelhouse/SHA256SUMS 为空或无法解析" >&2; rm -rf "$sums_tmp"; exit 1; }
    # 明文 HTTP 的 find-links 默认被 pip 视为不可信主机而忽略（叠加 --no-index 会导致找不到任何 wheel），
    # 按 BASE_URL 的 host 显式放行；HTTPS 源机走不到这里（也无需放行）。
    base_host="$(printf '%s' "$BASE_URL" | sed -E 's#^[a-z]+://([^:/]+).*#\1#')"
    pip install --no-index --find-links "$torch_links" --trusted-host "$base_host" --no-cache-dir \
        --require-hashes -r "$sums_tmp/torch-reqs.txt"
    rm -rf "$sums_tmp"
}
# 小包始终在线从清华源拉；本地有 wheelhouse 目录则一并作 find-links 兜底。
extra_links=""; [ -d wheelhouse ] && extra_links="--find-links wheelhouse"
echo "      其余依赖（在线，${PYPI_INDEX_URL}）..."
pip install -r requirements/prod.txt -i "$PYPI_INDEX_URL" $extra_links

# ultralytics 会拉入 opencv-python，与 headless 版共享 cv2/ 文件，卸载非 headless 会连带
# 删共享模块。force-reinstall 默认连依赖一起重装会把 numpy 顶到 2.x（撞 torch ABI），
# 故 --no-deps 只重铺 cv2、不碰 numpy；随后显式复位 numpy 以防已被顶。
echo "      修复 opencv headless..."
pip uninstall -y opencv-python opencv-python-headless 2>/dev/null || true
pip install -i "$PYPI_INDEX_URL" $extra_links --no-deps --force-reinstall "opencv-python-headless<4.12.0"
pip install -i "$PYPI_INDEX_URL" $extra_links "numpy==1.26.4"

# ── [2] ffmpeg → 项目内 .ffmpeg/（与 mediamtx 同为项目内二进制，免 sudo）──
# 必须钉版：ffmpeg 4.x/8.x 对 -hls_fmp4_init_filename 解析差异巨大，见 docs/kb/DESIGN_HLS_TIMELINE.md。
ff_asset="vendor/ffmpeg/ffmpeg-linux-x64.tar.xz"
echo "[2/3] ffmpeg → .ffmpeg/"
dl "$ff_asset" "${BASE_URL%/}/vendor/ffmpeg/ffmpeg-linux-x64.tar.xz"
# 不校 SHA；xz -t 足以抓住下载截断/损坏（钉版由源机上那一份物料保证）。
xz -t "$ff_asset" || { echo "ERROR: ffmpeg 压缩包损坏" >&2; exit 1; }
ff_tmp="$(mktemp -d)"; trap 'rm -rf "$ff_tmp"' EXIT
tar xf "$ff_asset" -C "$ff_tmp"
ff_inner="$(find "$ff_tmp" -maxdepth 1 -type d -name 'ffmpeg-*' | head -1)"
[ -x "$ff_inner/bin/ffmpeg" ] || { echo "ERROR: 解压结构异常，找不到 bin/ffmpeg" >&2; exit 1; }
rm -rf .ffmpeg
mv "$ff_inner" .ffmpeg

# ── [3] mediamtx → mediamtx/（只取二进制，保留同目录随 git 走的 mediamtx.yml / LICENSE）──
mtx_asset="vendor/mediamtx/mediamtx-linux-x64.tar.gz"
echo "[3/3] mediamtx → mediamtx/"
dl "$mtx_asset" "${BASE_URL%/}/vendor/mediamtx/mediamtx-linux-x64.tar.gz"
# 与 ffmpeg 的 xz -t 对称：靠 gzip CRC 抓住截断/损坏。
gzip -t "$mtx_asset" || { echo "ERROR: mediamtx 压缩包损坏" >&2; exit 1; }
tar xzf "$mtx_asset" -C mediamtx mediamtx
chmod +x mediamtx/mediamtx

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
"mediamtx/mediamtx" --version
