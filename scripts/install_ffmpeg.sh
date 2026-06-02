#!/bin/bash
# 安装钉版的 BtbN ffmpeg static build 到 /opt/ffmpeg-static
#
# 为什么必须钉版本：见 docs/HLS_TIMELINE_PITFALL.md。
# ffmpeg 4.x / 8.x 对 `-hls_fmp4_init_filename` 解析行为差异巨大，生产
# 必须用 7.x 稳定版（或确认过的版本），跨机器、跨次部署用同一个二进制。
#
# 用法：
#   bash scripts/install_ffmpeg.sh                              # 在线下载 → 装到 /opt/ffmpeg-static
#   bash scripts/install_ffmpeg.sh /custom/path                 # 自定义安装根
#   bash scripts/install_ffmpeg.sh /opt /path/to/tar.xz         # 离线：使用已下载好的 tarball
#   FFMPEG_TARBALL=/path/to/tar.xz bash scripts/install_ffmpeg.sh   # 同上，环境变量形式
#
# 离线场景（服务器不通外网）：本地 Windows 下载 .tar.xz，scp 到服务器，
# 跑 `sudo bash scripts/install_ffmpeg.sh /opt ~/ffmpeg-n7.1.4-linux64-gpl-7.1.tar.xz`。
# 脚本会自动跳过下载步骤，直接走 SHA256 校验 → 解压 → 切 symlink。
#
# 跑完后把这行写进生产配置文件 .env（CLEANSIGHT_ENV=prod 加载的就是它；dev 用 .env.dev）：
#   CLEANSIGHT_FFMPEG_PATH=/opt/ffmpeg-static/bin/ffmpeg
#
# 幂等：重复跑只会无操作（已安装且版本一致时直接退出）。

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────
# 配置区：升级 ffmpeg 时只改这三行
# ─────────────────────────────────────────────────────────────────────

# BtbN release tag。⚠️ BtbN 不按 patch 版本发 tag（不存在 "n7.1.4" 这个 tag/资产），只有两类：
#   - latest        滚动，内容会变，不可钉
#   - autobuild-日期  不可变快照 —— 要钉版就用这个
# 7.1.4 的构建藏在某个日期快照里（资产名是 ffmpeg-n7.1.4-<n>-g<commit>-...）。
# 找法：releases 页搜含 "ffmpeg-n7.1.4" 的 autobuild release，取其 tag。
# ⚠️ 旧 autobuild 会被 BtbN 定期清理 → 届时此 URL 会 404。长期可靠做法见脚本头：
#    首次下好 tarball 留存，之后用离线参数安装，不再依赖 BtbN 保留期。
FFMPEG_BUILD_TAG="autobuild-2026-06-01-15-02"

# 资产文件名。ffmpeg-nX.Y.Z-<n>-g<commit>-linux64-gpl-7.1.tar.xz：
#   - nX.Y.Z-<n>-g<commit> = ffmpeg 版本 + 距该 tag 的提交数 + 构建 commit（本资产即 7.1.4）
#   - linux64 = amd64 Linux
#   - gpl 含 libx264 / libx265（HLS 编码必需）
#   - 末尾 -7.1 是 BtbN 构建基础设施版本号，跟 ffmpeg 版本无关
FFMPEG_ASSET="ffmpeg-n7.1.4-7-gadcf20da26-linux64-gpl-7.1.tar.xz"

# 对应资产的 SHA256。BtbN 不提供 .sha256 旁文件，需自行算：
#   下好 tarball 后跑 `sha256sum <asset>`，把结果填到这里。
#   留空 → 脚本跳过校验（首次试装可接受，生产环境务必填上）。
FFMPEG_SHA256=""

# ─────────────────────────────────────────────────────────────────────

INSTALL_ROOT="${1:-/opt}"
INSTALL_LINK="${INSTALL_ROOT}/ffmpeg-static"
INSTALL_VERSIONED="${INSTALL_ROOT}/ffmpeg-static-${FFMPEG_BUILD_TAG}"
DOWNLOAD_BASE="https://github.com/BtbN/FFmpeg-Builds/releases/download/${FFMPEG_BUILD_TAG}"

# 离线 tarball：第 2 个位置参数 > FFMPEG_TARBALL 环境变量 > 自动在常见位置找
PROVIDED_TARBALL="${2:-${FFMPEG_TARBALL:-}}"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# ─────────────────────────────────────────────────────────────────────
# 输出辅助
# ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────
# 前置检查
# ─────────────────────────────────────────────────────────────────────
[[ "$(uname -s)" == "Linux" ]]  || error "仅支持 Linux（当前: $(uname -s)）"
[[ "$(uname -m)" == "x86_64" ]] || error "仅支持 x86_64（当前: $(uname -m)）"
[[ "${FFMPEG_BUILD_TAG}" != "autobuild-PLEASE-FILL-ME-IN" ]] || \
    error "请先编辑本脚本顶部，把 FFMPEG_BUILD_TAG 填成真实的 BtbN release tag"

# 决定走在线下载还是离线 tarball 路径
if [[ -n "${PROVIDED_TARBALL}" ]]; then
    [[ -f "${PROVIDED_TARBALL}" ]] || error "指定的 tarball 不存在：${PROVIDED_TARBALL}"
    OFFLINE_MODE=1
else
    # 自动在常见位置找已下载好的 tarball（离线物料区 vendor/、项目根、~、/tmp）
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    for candidate in \
        "$(pwd)/vendor/ffmpeg/${FFMPEG_ASSET}" \
        "${SCRIPT_DIR}/../vendor/ffmpeg/${FFMPEG_ASSET}" \
        "$(pwd)/${FFMPEG_ASSET}" \
        "${SCRIPT_DIR}/${FFMPEG_ASSET}" \
        "${SCRIPT_DIR}/../${FFMPEG_ASSET}" \
        "${HOME}/${FFMPEG_ASSET}" \
        "/tmp/${FFMPEG_ASSET}"; do
        if [[ -f "${candidate}" ]]; then
            PROVIDED_TARBALL="${candidate}"
            OFFLINE_MODE=1
            info "自动发现已下载的 tarball：${candidate}"
            break
        fi
    done
fi

if [[ "${OFFLINE_MODE:-0}" -ne 1 ]]; then
    if command -v curl >/dev/null 2>&1; then
        DOWNLOAD="curl -fL --progress-bar -o"
    elif command -v wget >/dev/null 2>&1; then
        DOWNLOAD="wget -q --show-progress -O"
    else
        error "需要 curl 或 wget 之一，请先 apt install。或者本地下好 tarball 后传第二个参数：bash $0 ${INSTALL_ROOT} /path/to/${FFMPEG_ASSET}"
    fi
fi

# 需要 sudo 写 /opt
if [[ ! -w "${INSTALL_ROOT}" ]]; then
    if [[ "$EUID" -ne 0 ]]; then
        error "${INSTALL_ROOT} 不可写，请用 sudo 运行：sudo bash $0 ${INSTALL_ROOT}"
    fi
fi

# ─────────────────────────────────────────────────────────────────────
# 幂等检查：已安装且 tag 一致就跳过
# ─────────────────────────────────────────────────────────────────────
if [[ -L "${INSTALL_LINK}" ]] && [[ "$(readlink -f "${INSTALL_LINK}")" == "${INSTALL_VERSIONED}" ]] \
   && [[ -x "${INSTALL_LINK}/bin/ffmpeg" ]]; then
    CURRENT_VER="$("${INSTALL_LINK}/bin/ffmpeg" -version 2>/dev/null | head -1 || echo "?")"
    info "已安装且 tag 一致，跳过：${CURRENT_VER}"
    info "如需强制重装，先删 ${INSTALL_VERSIONED} 再跑本脚本"
    exit 0
fi

# ─────────────────────────────────────────────────────────────────────
# 准备 tarball：离线模式直接复制进 WORK_DIR，否则联网下载
# 都进 WORK_DIR 是为了下面的 tar / xz -t 用一致路径
# ─────────────────────────────────────────────────────────────────────
cd "${WORK_DIR}"
if [[ "${OFFLINE_MODE:-0}" -eq 1 ]]; then
    info "离线模式：使用 ${PROVIDED_TARBALL}"
    cp "${PROVIDED_TARBALL}" "${FFMPEG_ASSET}"
else
    info "下载 ffmpeg：${FFMPEG_ASSET}（tag=${FFMPEG_BUILD_TAG}）"
    ${DOWNLOAD} "${FFMPEG_ASSET}" "${DOWNLOAD_BASE}/${FFMPEG_ASSET}" \
        || error "下载失败：${DOWNLOAD_BASE}/${FFMPEG_ASSET}"
fi

# 大小 sanity check（小于 30MB 八成截断了）
SIZE_BYTES="$(stat -c%s "${FFMPEG_ASSET}")"
[[ "${SIZE_BYTES}" -gt 30000000 ]] || \
    error "tarball 只有 ${SIZE_BYTES} 字节，明显截断 —— 重新下载或检查上传完整性"

info "tarball 就绪：$(numfmt --to=iec "${SIZE_BYTES}")"

# ─────────────────────────────────────────────────────────────────────
# 校验 SHA256
#   优先用脚本顶部钉的 FFMPEG_SHA256；为空则回退到离线物料旁的
#   <tarball>.sha256 sidecar（build.sh 下载时自动生成），命中即强制校验。
# ─────────────────────────────────────────────────────────────────────
EXPECTED_SHA256="${FFMPEG_SHA256}"
if [[ -z "${EXPECTED_SHA256}" && "${OFFLINE_MODE:-0}" -eq 1 && -f "${PROVIDED_TARBALL}.sha256" ]]; then
    EXPECTED_SHA256="$(awk '{print $1}' "${PROVIDED_TARBALL}.sha256")"
    info "用物料旁 sidecar 的 SHA256：${PROVIDED_TARBALL}.sha256"
fi
if [[ -n "${EXPECTED_SHA256}" ]]; then
    info "校验 SHA256..."
    echo "${EXPECTED_SHA256}  ${FFMPEG_ASSET}" | sha256sum -c - \
        || error "SHA256 不匹配，下载产物可能被篡改或损坏"
    info "SHA256 OK"
else
    warn "无可用 SHA256（脚本未钉且无 sidecar），跳过校验（首次试装可接受，生产环境强烈建议补上）"
fi

# ─────────────────────────────────────────────────────────────────────
# 解压 + 原子符号链接切换
# ─────────────────────────────────────────────────────────────────────
info "校验压缩完整性..."
xz -t "${FFMPEG_ASSET}" || error "xz 校验失败，文件损坏"

info "解压到 ${INSTALL_VERSIONED}"
tar xf "${FFMPEG_ASSET}"
EXTRACTED_DIR="$(find . -maxdepth 1 -type d -name 'ffmpeg-*-linux64-gpl*' | head -1)"
[[ -n "${EXTRACTED_DIR}" ]] || error "解压结构异常，找不到 ffmpeg-*-linux64-gpl* 目录"

# 清掉旧的同 tag 残留（如果存在）
rm -rf "${INSTALL_VERSIONED}"
mv "${EXTRACTED_DIR}" "${INSTALL_VERSIONED}"

# 验证二进制能跑
[[ -x "${INSTALL_VERSIONED}/bin/ffmpeg" ]] || error "二进制结构异常，找不到 bin/ffmpeg"
"${INSTALL_VERSIONED}/bin/ffmpeg" -version >/dev/null 2>&1 \
    || error "${INSTALL_VERSIONED}/bin/ffmpeg 无法执行（可能架构不匹配或权限问题）"

# 原子切换 symlink
ln -sfn "${INSTALL_VERSIONED}" "${INSTALL_LINK}.new"
mv -T "${INSTALL_LINK}.new" "${INSTALL_LINK}"

# ─────────────────────────────────────────────────────────────────────
# 收尾
# ─────────────────────────────────────────────────────────────────────
echo
info "安装完成"
echo "  路径：${INSTALL_LINK}/bin/ffmpeg -> ${INSTALL_VERSIONED}/bin/ffmpeg"
echo "  版本：$("${INSTALL_LINK}/bin/ffmpeg" -version | head -1)"
echo
echo "下一步：把下面这行加进 backend 的生产配置 .env（CLEANSIGHT_ENV=prod 加载的就是它，已存在则替换）："
echo "  CLEANSIGHT_FFMPEG_PATH=${INSTALL_LINK}/bin/ffmpeg"
echo
echo "回滚：rm ${INSTALL_LINK} && ln -s <旧版本目录> ${INSTALL_LINK}"
