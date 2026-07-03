"""
客户端队列管理器单例模块

按「读多写少」的中台形态实现：读全程无锁（原子读不可变快照引用），写极少走单锁
copy-on-write 换引用（不阻塞读）。

注册表是一本**不可变 dict**（`_runs`），由单一引用变量持有：
- 读者原子读 `self._runs` 后直接读/迭代——它永不就地变更，故无需锁；
- 写者在 `_wlock` 下复制一份、改完、原子换引用（`self._runs = new`）；
- CPython 中「读引用」「换引用」均为原子操作，读者要么看旧全量、要么看新全量，永不撕裂。

前提：COW 写是 O(N) 拷贝，N=并发 run 数（个位数）、写极稀，可忽略。
"""

import logging
import threading
import types
from typing import Any, Dict, List, Mapping, Optional

from .config import get_client_config
from .queues import ClientQueues

logger = logging.getLogger("app.services.client.manager")

# 加载客户端配置（单例）
_client_config = get_client_config()


class ClientManager:
    """客户端队列注册表（COW 中台，支持依赖注入）。

    键 = **`task_id`(int)**（由 RunController 决定并传入）；CQ 由 RunController 建好后
    `set` 换槽，本类只做哑存储、不建 CQ。

    读接口（无锁）：`get`(只读按键) / `has_client` / `snapshot`(零拷贝只读视图)
      / `get_client_by_task_id`(直取,同 get) / `find_by_source_ip`(扫描,匹配首个)
      / `get_all_queue_depths` / `get_client_count` / `get_status_summary`。
    写接口（`_wlock` + COW 换引用）：`set`(换槽) / `remove` / `remove_if` / `clear_all`。
    """

    def __init__(self, config=None):
        """初始化 ClientManager。

        Args:
            config: 可选配置对象（便于测试注入 mock 配置）。
        """
        self._config = config or _client_config

        # 不可变注册表快照：task_id → ClientQueues。永不就地变更，写时整体换引用。
        self._runs: Dict[int, ClientQueues] = {}
        self._wlock = threading.Lock()  # 只串行「写」（create / remove），不阻塞读

        # per-task 生命周期锁（RLock）：护一次 start/teardown 事务，供 RunController
        # / api / HealthMonitor 共用串行化同一 task 的启停。与 _wlock 是两把不同的锁：
        # _wlock 全局极短护换引用；_task_locks[task_id] per-task 长持护跨服务事务。
        self._task_locks: Dict[int, threading.RLock] = {}
        self._task_locks_guard = threading.Lock()

        logger.info("[ClientManager] Initialized")

    # ── 任务级锁（per-task 生命周期事务锁）────────────────────

    def lock_for(self, task_id: int) -> threading.RLock:
        """返回该 task_id 的生命周期 RLock（get-or-create）。

        供 RunController.start_run / stop_run 及 HealthMonitor 共用，串行化同一 task 的
        启停事务。RLock：同线程可重入（start_run 持锁内再调 stop_run 不自死锁）。
        """
        with self._task_locks_guard:
            lk = self._task_locks.get(task_id)
            if lk is None:
                lk = threading.RLock()
                self._task_locks[task_id] = lk
            return lk

    # ── 读接口（无锁）───────────────────────────────────────────

    def get(self, task_id: int) -> Optional[ClientQueues]:
        """只读按键获取 CQ；不存在返回 None（无锁，数据面用）。"""
        return self._runs.get(task_id)

    def has_client(self, task_id: int) -> bool:
        """检查该 task 是否有活跃 run（无锁）。"""
        return task_id in self._runs

    def get_client_by_task_id(self, task_id: int) -> Optional[ClientQueues]:
        """按 task_id 直取 ClientQueues（键即 task_id，O(1)；等价 get）。"""
        return self._runs.get(task_id)

    def find_by_source_ip(self, source_ip: str) -> Optional[ClientQueues]:
        """按 source_ip 查 ClientQueues（扫描当前快照，**匹配首个**命中）。

        边界垫片用：前端 `/terminate`、WS `/ai/video` 仍以 `?client_id=<source_ip>` 调用，
        由此把 source_ip 解析回当前 run。同 source_ip 并发多 run 时命中扫描到的第一个
        （业务不保证 source_ip 唯一，故取首个即可）。
        """
        for cq in self._runs.values():  # 原子读引用后迭代不可变快照
            if cq.source_ip == source_ip:
                return cq
        return None

    def snapshot(self) -> Mapping[int, ClientQueues]:
        """返回所有 run 的**零拷贝只读视图**（键=task_id，安全迭代，切勿修改）。"""
        return types.MappingProxyType(self._runs)

    def get_all_queue_depths(self) -> Dict[int, Dict[str, int]]:
        """所有 run 的队列深度统计。

        格式：{task_id: {ca_ready, ca_raw, ca_processed, has_rendered}}
        """
        runs = self._runs  # 原子读一份不可变快照
        return {tid: cq.get_queue_depths() for tid, cq in runs.items()}

    def get_client_count(self) -> int:
        """当前客户端总数（无锁）。"""
        return len(self._runs)

    def get_status_summary(self) -> Dict:
        """整体状态摘要（用于监控和调试）。"""
        snapshot = self._runs  # 不可变，无需拷贝
        total_frames = 0
        clients_status = {}
        for task_id, cq in snapshot.items():
            depths = cq.get_queue_depths()
            clients_status[task_id] = depths
            total_frames += (
                depths.get("ca_ready", 0)
                + depths.get("ca_raw", 0)
                + depths.get("ca_processed", 0)
            )
        return {
            "client_count": len(snapshot),
            "total_queued_frames": total_frames,
            "clients": clients_status,
        }

    # ── 写接口（_wlock + COW）──────────────────────────────────

    def set(self, task_id: int, cq: ClientQueues) -> None:
        """原子装入/替换 task_id 槽位为一个已建好的（不可变身份）CQ。

        供 `RunController.start_run` 路径：每次 run 建**新** CQ 后整体换槽（不在旧 CQ 上原地改）。
        `_wlock` 下 COW 换引用发布，读者原子读引用即看到全新对象——观察不到半建态。
        旧槽引用被丢弃，其 decoder/actor 持到释放后 GC。
        """
        with self._wlock:
            new = dict(self._runs)
            new[task_id] = cq
            self._runs = new
        logger.info(f"[ClientManager] set run: task_id={task_id}")

    def remove(self, task_id: int, cleanup: bool = True) -> Dict[str, Any]:
        """注销该 task 的 run，可选清理其队列资源。

        `cleanup` 在换引用之后、锁外执行（不占写锁）。

        Returns:
            {client_id, removed, cleaned, error}（client_id 键名沿用，值为 task_id）
        """
        result: Dict[str, Any] = {
            "client_id": task_id,
            "removed": False,
            "cleaned": False,
            "error": None,
        }

        with self._wlock:
            cq = self._runs.get(task_id)
            if cq is None:
                result["error"] = "client_not_found"
                logger.warning(f"尝试移除不存在的 run: task_id={task_id}")
                return result
            new = dict(self._runs)  # COW 删除
            del new[task_id]
            self._runs = new
            result["removed"] = True

        # 清理在锁外执行（减少写锁持有时间）
        if cleanup:
            try:
                cq.clear()
                result["cleaned"] = True
                logger.info(f"run 队列已清理: task_id={task_id}")
            except Exception as e:
                result["error"] = str(e)
                logger.error("清理 run 队列失败 task_id=%s: %s", task_id, e, exc_info=True)

        logger.info(f"run 已移除: task_id={task_id}")
        return result

    def remove_if(
        self, task_id: int, expected_cq: ClientQueues, cleanup: bool = True
    ) -> bool:
        """对象身份 fence 删除：仅当 `registry[task_id] is expected_cq` 才移除。

        防止迟到 cleanup 误删「同键新实例」（重启/切换后装入的新 CQ）。命中即删并返回 True，
        否则不动、返回 False。清理在锁外执行。
        """
        with self._wlock:
            cur = self._runs.get(task_id)
            if cur is not expected_cq:
                return False
            new = dict(self._runs)
            del new[task_id]
            self._runs = new

        if cleanup:
            try:
                expected_cq.clear()
            except Exception as e:
                logger.error("清理 run 队列失败 task_id=%s: %s", task_id, e, exc_info=True)
        logger.info(f"run 已移除(identity-fenced): task_id={task_id}")
        return True

    def clear_all(self) -> List[Dict[str, Any]]:
        """清空所有 run 资源（服务关闭时的全局清理）。"""
        results = []
        for task_id in list(self._runs.keys()):  # 快照键列表
            result = self.remove(task_id, cleanup=True)
            results.append(
                {
                    "client_id": task_id,
                    "success": result["removed"] and result["cleaned"],
                    "error": result.get("error"),
                }
            )
        logger.info(f"所有 run 资源已清理 (总数: {len(results)})")
        return results


# 全局单例实例
client_manager = ClientManager()
