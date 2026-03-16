"""
sync_agent.py — 医院 DZM 机侧数据同步脚本
功能：枚举 SQL Server 所有视图 → 导出 CSV → gzip 压缩 → SFTP 上传
打包：pyinstaller --onefile sync_agent.py
"""

import configparser
import csv
import gzip
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import paramiko
import pyodbc

# PyInstaller --onefile 时 __file__ 指向临时解压目录；用 sys.executable 定位 .exe 所在目录
BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.ini"
PROGRESS_FILE = BASE_DIR / "sync_progress.json"
LOCK_FILE = BASE_DIR / "sync.lock"
LOG_FILE = BASE_DIR / "sync.log"
TEMP_DIR = BASE_DIR / "temp"

FETCH_BATCH = 5000  # 流式读取每批行数（32GB内存可适当调大）


# ─── 初始化 ────────────────────────────────────────────────────────────────────

def setup_logging():
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE, encoding="utf-8")
    return config


# ─── SQL Server ────────────────────────────────────────────────────────────────

def get_db_connection(config: configparser.ConfigParser):
    s = config["sqlserver"]
    conn_str = (
        f"DRIVER={{{s['driver']}}};"
        f"SERVER={s['server']};"
        f"DATABASE={s['database']};"
        f"UID={s['username']};"
        f"PWD={s['password']};"
    )
    return pyodbc.connect(conn_str, timeout=30)


def try_enable_snapshot(conn) -> bool:
    """
    检查当前数据库是否开启了 SNAPSHOT 隔离级别。
    若已开启，在此连接上开启快照事务，返回 True。
    若未开启，记录警告并继续（数据一致性降级为尽力而为），返回 False。
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT snapshot_isolation_state FROM sys.databases WHERE name = DB_NAME()"
    )
    row = cursor.fetchone()
    snapshot_on = row and row[0] == 1

    if snapshot_on:
        conn.autocommit = False
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SNAPSHOT")
        logging.info("snapshot isolation: ON — all views will share the same point-in-time")
    else:
        logging.warning(
            "snapshot isolation: OFF — views may be captured at different moments. "
            "Ask DBA to run: ALTER DATABASE ... SET ALLOW_SNAPSHOT_ISOLATION ON"
        )

    return snapshot_on


def get_all_views(conn) -> list[str]:
    cursor = conn.cursor()
    cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS ORDER BY TABLE_NAME")
    return [row[0] for row in cursor.fetchall()]


def export_view_to_csv(conn, view_name: str, csv_path: Path):
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM [{view_name}]")
    columns = [desc[0] for desc in cursor.description]

    total = 0
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        while batch := cursor.fetchmany(FETCH_BATCH):
            writer.writerows(batch)
            total += len(batch)

    logging.info(f"  exported {total} rows → {csv_path.name}")


def compress_file(csv_path: Path, gz_path: Path):
    with open(csv_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        f_out.write(f_in.read())


# ─── SFTP 上传（断点续传）──────────────────────────────────────────────────────

CHUNK_SIZE = 65536  # 64 KB


def upload_with_resume(sftp: paramiko.SFTPClient, local_path: Path, remote_path: str):
    local_size = local_path.stat().st_size
    part_path = remote_path + ".part"

    # 检查服务端是否有未完成的分片
    try:
        remote_size = sftp.stat(part_path).st_size
        if remote_size >= local_size:
            remote_size = 0  # 残留文件异常，重传
    except FileNotFoundError:
        remote_size = 0

    mode = "ab" if remote_size > 0 else "wb"
    logging.info(
        f"  uploading {local_path.name}  "
        f"local={local_size}B  resume_from={remote_size}B"
    )

    with open(local_path, "rb") as f:
        f.seek(remote_size)
        with sftp.open(part_path, mode) as remote_file:
            remote_file.set_pipelined(True)
            while chunk := f.read(CHUNK_SIZE):
                remote_file.write(chunk)

    # 原子重命名，避免接收侧读到不完整文件
    try:
        sftp.remove(remote_path)
    except FileNotFoundError:
        pass
    sftp.rename(part_path, remote_path)
    logging.info(f"  upload done → {remote_path}")


def upload_with_retry(
    sftp: paramiko.SFTPClient,
    local_path: Path,
    remote_path: str,
    max_retries: int = 3,
) -> bool:
    for attempt in range(1, max_retries + 1):
        try:
            upload_with_resume(sftp, local_path, remote_path)
            return True
        except Exception as e:
            wait = 2 ** attempt
            logging.warning(
                f"  upload failed (attempt {attempt}/{max_retries}): {e} — retry in {wait}s"
            )
            time.sleep(wait)
    logging.error(f"  gave up uploading {local_path.name}")
    return False


# ─── 进度持久化 ────────────────────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "done": [],
        "failed": [],
    }


def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


# ─── 主流程 ────────────────────────────────────────────────────────────────────

def process_view(conn, sftp, view_name: str, remote_dir: str) -> bool:
    csv_path = TEMP_DIR / f"{view_name}.csv"
    gz_path = TEMP_DIR / f"{view_name}.csv.gz"
    remote_path = f"{remote_dir}/{view_name}.csv.gz"

    try:
        export_view_to_csv(conn, view_name, csv_path)
        compress_file(csv_path, gz_path)
        csv_path.unlink()

        ok = upload_with_retry(sftp, gz_path, remote_path)
        gz_path.unlink(missing_ok=True)
        return ok
    except Exception as e:
        logging.error(f"  error processing {view_name}: {e}")
        for p in [csv_path, gz_path]:
            if p.exists():
                p.unlink()
        return False


_KEY_FILE = "hospital_sync_key"


def _resolve_key() -> str:
    """frozen exe 时从打包资源目录读取私钥；开发模式下从脚本同级目录读取。"""
    if getattr(sys, "frozen", False):
        return str(Path(sys._MEIPASS) / _KEY_FILE)
    return str(BASE_DIR / _KEY_FILE)


def open_sftp(config: configparser.ConfigParser):
    s = config["sftp"]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        hostname=s["host"],
        port=int(s.get("port", 22)),
        username=s["username"],
        password=s.get("password") or None,
        key_filename=_resolve_key(),
        timeout=30,
    )
    return ssh, ssh.open_sftp()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def acquire_lock() -> bool:
    """写入锁文件（含 PID）。
    若锁文件存在但对应进程已死（强杀残留），自动清除后继续。
    """
    if LOCK_FILE.exists():
        pid = LOCK_FILE.read_text().strip()
        if _pid_alive(pid):
            logging.warning(f"another instance is running (PID {pid}), exiting")
            return False
        logging.warning(f"stale lock detected (PID {pid} is gone), removing")
        LOCK_FILE.unlink()
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def main():
    setup_logging()
    logging.info("=" * 60)
    logging.info("sync_agent started")

    if not acquire_lock():
        sys.exit(0)

    config = load_config()
    TEMP_DIR.mkdir(exist_ok=True)

    # 清理上次异常退出遗留的临时文件
    for f in TEMP_DIR.glob("*"):
        f.unlink(missing_ok=True)

    conn = get_db_connection(config)
    use_snapshot = try_enable_snapshot(conn)
    views = get_all_views(conn)
    logging.info(f"found {len(views)} views in SQL Server")

    progress = load_progress()
    done_set = set(progress["done"])
    remote_dir = config["sftp"]["remote_dir"]

    ssh, sftp = open_sftp(config)

    failed = []
    for view_name in views:
        if view_name in done_set:
            logging.info(f"skip (already done): {view_name}")
            continue

        logging.info(f">>> {view_name}")
        ok = process_view(conn, sftp, view_name, remote_dir)
        if ok:
            progress["done"].append(view_name)
            save_progress(progress)
        else:
            failed.append(view_name)

    # 对失败的视图再整体重试一轮
    if failed:
        logging.info(f"retrying {len(failed)} failed views ...")
        still_failed = []
        for view_name in failed:
            logging.info(f">>> retry {view_name}")
            ok = process_view(conn, sftp, view_name, remote_dir)
            if ok:
                progress["done"].append(view_name)
                save_progress(progress)
            else:
                still_failed.append(view_name)
        failed = still_failed

    sftp.close()
    ssh.close()

    # 快照事务：所有视图导出完毕后统一提交（只读事务，提交仅是释放快照资源）
    if use_snapshot:
        conn.commit()
    conn.close()

    # 全部成功后清理进度文件，下次从头跑
    if not failed:
        PROGRESS_FILE.unlink(missing_ok=True)

    logging.info(
        f"done — synced: {len(progress['done'])}, failed: {len(failed)}"
    )
    if failed:
        logging.error(f"failed views: {failed}")
    logging.info("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.warning("interrupted by user — progress saved, will resume next run")
    finally:
        release_lock()
