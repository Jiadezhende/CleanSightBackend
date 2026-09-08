"""
告警追溯集成测试

无需 FFmpeg 推流或推理引擎，仅需后端服务可达（/traceback、/media 路由）。
测试前在数据库和文件系统中预置数据，测试结束后无论成功与否都自动清理。

用法:
    python integration_tests/test_traceback.py [options]

参数:
    --server   <host>   服务器地址（默认: localhost）
    --task_id  <int>    测试任务 ID（默认: 9900001，避开真实数据）
    --alarm_id <int>    测试告警 ID（默认: 9900001，避开真实数据）

测试项:
    T1  GET /traceback/task/{task_id}/playlist.m3u8  — VOD 播放列表
    T2  GET /traceback/task/{task_id}/timeline  — 时间轴打点
    T3  跟进 T1 playlist 里的段 URL  — 媒体段可达
"""

import argparse
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from integration_tests.utils import APIClient, DatabaseHelper, seed_hls_segments


# ---------------------------------------------------------------------------
# 测试数据参数
# ---------------------------------------------------------------------------

# 3 段 × 10 秒，告警落在第 2 段（15s 处）
_N_SEGMENTS = 3
_SEGMENT_DURATION_S = 10
# 录制起点：1 小时前（避免与正在运行的任务时间戳重叠）
_RECORD_START_OFFSET_S = 3600


def _build_test_timestamps(n: int = _N_SEGMENTS) -> tuple:
    """返回 (ts_us_list, alarm_detected_at_ms)。

    ts_us_list 为 n 个相隔 10s 的微秒时间戳列表。
    alarm_detected_at_ms 落在第 2 段起点 +5s 处，即录制时间跨度之内——timeline
    才会把它作为一个 event 打在进度条上。
    """
    base_s = int(time.time()) - _RECORD_START_OFFSET_S
    ts_us_list = [
        (base_s + i * _SEGMENT_DURATION_S) * 1_000_000 for i in range(n)
    ]
    # 第 2 段（index 1）起点 +5s
    alarm_detected_at_ms = ts_us_list[1] // 1000 + 5_000
    return ts_us_list, alarm_detected_at_ms


# ---------------------------------------------------------------------------
# 测试 Fixture（上下文管理器）
# ---------------------------------------------------------------------------


@contextmanager
def traceback_test_fixture(task_id: int, alarm_id: int, server: str):
    """预置测试所需数据，退出时无条件清理。

    Yields:
        dict with keys: task_id, step_id, alarm_id,
                        ts_us_list, alarm_detected_at_ms, task_dir
    """
    step_id = 1  # 测漏阶段
    ts_us_list, alarm_detected_at_ms = _build_test_timestamps()
    task_dir: Optional[Path] = None

    print(f"\n[Setup] task_id={task_id}, step_id={step_id}, alarm_id={alarm_id}")
    print(f"[Setup] 时间段: {_N_SEGMENTS} 段 × {_SEGMENT_DURATION_S}s")
    print(f"[Setup] alarm detected_at = {alarm_detected_at_ms} ms（第 2 段内）")

    try:
        # 1. DB: 任务记录（source_ip 仍写入但只用于运行时 ClientManager；
        #    追溯不再依赖该字段）
        DatabaseHelper.create_test_task(
            task_id, source_ip=f"test.tb.{task_id}", current_step=str(step_id)
        )

        # 2. DB: 告警记录
        DatabaseHelper.create_test_alarm(
            alarm_id=alarm_id,
            task_id=task_id,
            detected_at_ms=alarm_detected_at_ms,
            alarm_type="bubble",
            severity="high",
            message="integration test alarm",
            step_id=step_id,
            step_name="测漏",
        )

        # 3. 文件系统: HLS 段（新路径 {base_dir}/{task_id}/{step_id}/）
        task_dir = seed_hls_segments(task_id, step_id, ts_us_list)

        yield {
            "task_id": task_id,
            "step_id": step_id,
            "alarm_id": alarm_id,
            "ts_us_list": ts_us_list,
            "alarm_detected_at_ms": alarm_detected_at_ms,
            "task_dir": task_dir,
            "base_url": f"http://{server}:8000",
        }

    finally:
        print("\n[Teardown] 清理测试数据...")
        DatabaseHelper.cleanup_test_alarms_for_task(task_id)
        DatabaseHelper.cleanup_test_task(task_id)
        if task_dir and task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
            # 若 task_id 上层目录为空则一并删除
            parent = task_dir.parent
            try:
                parent.rmdir()
            except OSError:
                pass
            print(f"✅ 删除 HLS 目录: {task_dir}")


# ---------------------------------------------------------------------------
# 断言辅助
# ---------------------------------------------------------------------------


def _assert(condition: bool, label: str, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    suffix = f"  ({detail})" if detail and not condition else ""
    print(f"  [{status}] {label}{suffix}")
    return condition


def _get(url: str, timeout: int = 10) -> requests.Response:
    return requests.get(url, timeout=timeout)


# ---------------------------------------------------------------------------
# 3 个测试
# ---------------------------------------------------------------------------


def test_playlist(ctx: Dict[str, Any]) -> bool:
    """T1: GET /traceback/task/{task_id}/playlist.m3u8?step_id=..."""
    url = (
        f"{ctx['base_url']}/traceback/task/{ctx['task_id']}/playlist.m3u8"
        f"?step_id={ctx['step_id']}"
    )
    print(f"\nT1 playlist  →  {url}")

    resp = _get(url)
    ok = True
    ok &= _assert(resp.status_code == 200, f"HTTP 200 (got {resp.status_code})")
    if resp.status_code != 200:
        print(f"     响应体: {resp.text[:300]}")
        return False

    content = resp.text
    ok &= _assert("#EXTM3U" in content, "#EXTM3U 存在")
    ok &= _assert("#EXT-X-ENDLIST" in content, "#EXT-X-ENDLIST 存在（VOD）")
    ok &= _assert(
        "mpegurl" in resp.headers.get("content-type", "").lower(),
        f"Content-Type 含 mpegurl (got {resp.headers.get('content-type')})",
    )
    # 只数段行：#EXT-X-MAP 的 init URL 也含 /media/，必须按「非 # 开头」排掉
    seg_lines = [
        l.strip()
        for l in content.splitlines()
        if l.strip() and not l.startswith("#") and "/media/segment/" in l
    ]
    ok &= _assert(
        len(seg_lines) == _N_SEGMENTS,
        f"包含 {_N_SEGMENTS} 个段 URL (found={len(seg_lines)})",
    )
    if seg_lines:
        ctx["_segment_url"] = seg_lines[0]
    return ok


def test_timeline(ctx: Dict[str, Any]) -> bool:
    """T2: GET /traceback/task/{task_id}/timeline?step_id=..."""
    url = (
        f"{ctx['base_url']}/traceback/task/{ctx['task_id']}/timeline"
        f"?step_id={ctx['step_id']}"
    )
    print(f"\nT2 timeline  →  {url}")

    resp = _get(url)
    ok = True
    ok &= _assert(resp.status_code == 200, f"HTTP 200 (got {resp.status_code})")
    if resp.status_code != 200:
        print(f"     响应体: {resp.text[:300]}")
        return False

    data = resp.json()
    events = data.get("events", [])
    alarm_events = [e for e in events if e.get("alarm_id") == ctx["alarm_id"]]
    ok &= _assert(
        len(alarm_events) == 1,
        f"events 中包含 alarm_id={ctx['alarm_id']} (found={len(alarm_events)})",
    )
    ok &= _assert(data.get("duration_ms", 0) > 0, f"duration_ms > 0 (got {data.get('duration_ms')})")
    return ok


def test_media_segment(ctx: Dict[str, Any]) -> bool:
    """T3: 跟进 T1 playlist 里的第一条段 URL 请求媒体段"""
    url = ctx.get("_segment_url")
    if not url:
        print("\nT3 media_seg  →  跳过（T1 未拿到段 URL）")
        return False

    print(f"\nT3 media_seg →  {url[:80]}...")

    resp = _get(url)
    ok = _assert(
        resp.status_code in (200, 206),
        f"HTTP 200/206 (got {resp.status_code})",
    )
    if not ok:
        print(f"     响应体: {resp.text[:200]}")
    return ok


# ---------------------------------------------------------------------------
# 主测试流程
# ---------------------------------------------------------------------------


def run_traceback_test(args) -> bool:
    print("\n" + "=" * 60)
    print("CleanSight 告警追溯集成测试")
    print(f"  服务器: {args.server}:8000")
    print(f"  task_id: {args.task_id} | alarm_id: {args.alarm_id}")
    print("=" * 60)

    api = APIClient(f"http://{args.server}:8000")
    if not api.check_health():
        raise SystemExit("后端 API 不可达，请先启动后端服务")
    print(f"后端 API 正常: http://{args.server}:8000")

    results: Dict[str, bool] = {}

    with traceback_test_fixture(args.task_id, args.alarm_id, args.server) as ctx:
        results["T1 playlist  "] = test_playlist(ctx)
        results["T2 timeline  "] = test_timeline(ctx)
        results["T3 media_seg "] = test_media_segment(ctx)

    print("\n" + "=" * 60)
    print("测试结果汇总:")
    all_pass = True
    for label, passed in results.items():
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label}")
        if not passed:
            all_pass = False

    total = len(results)
    passed_count = sum(results.values())
    print(f"\n{passed_count}/{total} 通过")
    print("=" * 60)
    return all_pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="CleanSight 告警追溯集成测试（无需 FFmpeg，只需后端可达）"
    )
    parser.add_argument("--server", default="localhost", help="服务器地址（默认: localhost）")
    parser.add_argument(
        "--task_id",
        type=int,
        default=9900001,
        help="测试任务 ID（默认: 9900001，避开真实数据）",
    )
    parser.add_argument(
        "--alarm_id",
        type=int,
        default=9900001,
        help="测试告警 ID（默认: 9900001，避开真实数据）",
    )
    args = parser.parse_args()

    try:
        success = run_traceback_test(args)
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
