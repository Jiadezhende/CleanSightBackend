"""
data_receiver.py — 业务服务器侧接收脚本
功能：轮询 pending 目录 → 解压 .csv.gz → 保存 .csv → 归档原文件 → 定期清理
运行：python data_receiver.py  （建议配置为 systemd service）
"""

import gzip
import logging
import logging.handlers
import shutil
import time
import urllib.request
import urllib.error
import json
from datetime import datetime, timedelta
from pathlib import Path

# ─── 目录配置 ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent

PENDING_DIR = BASE_DIR / "data" / "pending"   # SFTP 落盘目录
CSV_DIR     = BASE_DIR / "data" / "csv"       # 解压后 CSV 输出目录（同名覆盖）
DONE_DIR    = BASE_DIR / "data" / "done"      # 已处理归档目录
LOG_FILE    = BASE_DIR / "receiver.log"

POLL_INTERVAL  = 30    # 轮询间隔（秒）
MIN_FILE_AGE   = 3     # 文件稳定判断：至少静止 N 秒才处理
DONE_KEEP_DAYS = 7     # done/ 目录保留天数，超期自动删除

# TODO: 确认业务服务接口路径后填入，None 表示暂不通知
IMPORT_NOTIFY_URL: str | None = None  # e.g. "http://localhost:8080/api/import-csv"

# ─── 后处理说明 ────────────────────────────────────────────────────────────────
#
# 当 receiver 成功解压一个视图文件后，会向 IMPORT_NOTIFY_URL 发送 POST 请求：
#
#   POST <IMPORT_NOTIFY_URL>
#   Content-Type: application/json
#   {"csv_path": "/opt/hospital_sync/data/csv/<view_name>.csv"}
#
# 业务服务收到请求后，自行读取该 CSV 并导入 PostgreSQL。
# 推荐实现方式（PostgreSQL COPY 命令，速度最快）：
#
#   COPY <target_table> FROM '<csv_path>'
#        WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
#
# 注意事项：
#   1. csv/ 目录下同名文件会被直接覆盖，始终是最新一次同步的完整数据
#   2. 建议导入前先 TRUNCATE 目标表，再 COPY，保证数据与视图完全一致
#   3. csv_path 是服务器本地绝对路径，业务服务需有读取权限
#   4. 接口应返回 2xx；失败时 receiver 只记日志，不重试（下次同步会重发通知）
# ──────────────────────────────────────────────────────────────────────────────


# ─── 初始化 ────────────────────────────────────────────────────────────────────

def setup():
    for d in [PENDING_DIR, CSV_DIR, DONE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


# ─── 文件处理 ──────────────────────────────────────────────────────────────────

def is_stable(path: Path) -> bool:
    return (time.time() - path.stat().st_mtime) >= MIN_FILE_AGE


def decompress(gz_path: Path) -> Path:
    """解压 .csv.gz → CSV_DIR/<view_name>.csv，同名直接覆盖（只保留最新数据）"""
    view_name = gz_path.stem
    if not view_name.endswith(".csv"):
        view_name += ".csv"
    csv_path = CSV_DIR / view_name

    with gzip.open(gz_path, "rb") as f_in, open(csv_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    return csv_path


def archive(gz_path: Path):
    """将原始 .csv.gz 移入 done/ 归档，文件名加时间戳"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = DONE_DIR / f"{gz_path.stem}_{ts}{gz_path.suffix}"
    shutil.move(str(gz_path), dest)


def notify_import(csv_path: Path):
    """通知业务服务导入指定 CSV 文件，接口路径确定后填写 IMPORT_NOTIFY_URL。"""
    if not IMPORT_NOTIFY_URL:
        return
    payload = json.dumps({"csv_path": str(csv_path)}).encode()
    req = urllib.request.Request(
        IMPORT_NOTIFY_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logging.info(f"  notify import: {resp.status} ← {IMPORT_NOTIFY_URL}")
    except urllib.error.URLError as e:
        logging.error(f"  notify import failed: {e}")


def process_file(gz_path: Path):
    logging.info(f"processing: {gz_path.name}")
    try:
        csv_path = decompress(gz_path)
        size_kb = csv_path.stat().st_size // 1024
        logging.info(f"  → {csv_path.name}  ({size_kb} KB)")
        archive(gz_path)
        notify_import(csv_path)
    except Exception as e:
        logging.error(f"  failed: {gz_path.name} — {e}")


# ─── 定期清理 ──────────────────────────────────────────────────────────────────

def cleanup_done():
    """删除 done/ 中超过 DONE_KEEP_DAYS 天的归档文件"""
    cutoff = datetime.now() - timedelta(days=DONE_KEEP_DAYS)
    removed = 0
    for f in DONE_DIR.glob("*.csv.gz"):
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink()
            removed += 1
    if removed:
        logging.info(f"cleanup: removed {removed} file(s) older than {DONE_KEEP_DAYS} days")


# ─── 主循环 ────────────────────────────────────────────────────────────────────

def main():
    setup()
    logging.info("=" * 60)
    logging.info(f"data_receiver started  (poll every {POLL_INTERVAL}s)")
    logging.info(f"  pending : {PENDING_DIR}")
    logging.info(f"  csv out : {CSV_DIR}")
    logging.info(f"  archive : {DONE_DIR}  (keep {DONE_KEEP_DAYS} days)")
    logging.info("=" * 60)

    last_cleanup = datetime.min

    while True:
        gz_files = sorted(PENDING_DIR.glob("*.csv.gz"))
        if gz_files:
            stable = [f for f in gz_files if is_stable(f)]
            logging.info(f"found {len(gz_files)} file(s), {len(stable)} stable")
            for gz_path in stable:
                process_file(gz_path)

        # 每天清理一次 done/ 目录
        if (datetime.now() - last_cleanup).total_seconds() > 86400:
            cleanup_done()
            last_cleanup = datetime.now()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
