"""
直观验证 RateLimitStore / AntiScanStore 的过期回收行为。

使用极短的 window（2s），在控制台实时打印字典大小，
观察清理线程触发前后的变化。

运行方式（项目根目录）：
    python scripts/test_gateway_eviction.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.utils.gateway import AntiScanStore, IPWhitelistStore, RateLimitStore

WINDOW = 2          # 窗口（秒）：超短，方便演示
N_IPS = 200         # 模拟的不同 IP 数量
POLL_INTERVAL = 0.5 # 打印间隔（秒）


def fmt(label: str, n: int, width: int = 6) -> str:
    bar = "#" * min(n, 50)
    return f"  {label:<26} {n:>{width}}  {bar}"


def main():
    whitelist = IPWhitelistStore(allowed=frozenset(), ban_duration=3600)

    rate_store = RateLimitStore(
        limit=1,
        window=WINDOW,
        ban_store=whitelist,
        ban_threshold=10,   # 高阈值，避免演示中触发封禁
        ban_window=WINDOW,
    )
    antiscan = AntiScanStore(
        threshold=50,       # 高阈值，避免演示中触发封禁
        window=WINDOW,
        whitelist_store=whitelist,
    )

    # ── 第一步：写入大量 IP ──────────────────────────────────────────────────
    print(f"\n[1] 写入 {N_IPS} 个不同 IP …")
    for i in range(N_IPS):
        ip = f"10.0.{i // 256}.{i % 256}"
        rate_store.is_allowed(ip)   # 第一次允许 → 写入 _buckets
        rate_store.is_allowed(ip)   # 第二次超限 → 写入 _violations
        antiscan.record_error(ip, 404)

    print(f"  写入完成，开始监控（window={WINDOW}s，清理线程周期={WINDOW}s）\n")
    print(f"  {'字段':<26} {'当前 key 数':>6}  {'(每 # = 4 个 IP)':}")
    print("  " + "-" * 60)

    # ── 第二步：轮询打印，直到字典清空或超时 ─────────────────────────────────
    deadline = time.monotonic() + WINDOW * 4
    while time.monotonic() < deadline:
        rb = len(rate_store._buckets)
        rv = len(rate_store._violations)
        ae = len(antiscan._errors)

        print(fmt("RateLimitStore._buckets", rb))
        print(fmt("RateLimitStore._violations", rv))
        print(fmt("AntiScanStore._errors", ae))

        if rb == 0 and rv == 0 and ae == 0:
            print("\n  所有字典已清空 — 回收正常！\n")
            return

        print()
        time.sleep(POLL_INTERVAL)

    print("\n  超时：字典未在预期时间内清空，请检查实现。\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
