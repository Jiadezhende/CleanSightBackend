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

    键 = `run_key`（== `str(task_id)`，由 RunController 决定并传入）；CQ 由 RunController 建好后
    `set` 换槽，本类只做哑存储、不建 CQ。

    读接口（无锁）：`get`(只读按键) / `has_client` / `snapshot`(零拷贝只读视图)
      / `get_client_by_task_id`(扫描) / `find_by_source_ip`(扫描,匹配首个)
      / `get_all_queue_depths` / `get_client_count` / `get_status_summary`。
    写接口（`_wlock` + COW 换引用）：`set`(换槽) / `remove` / `remove_if` / `clear_all`。
    """

    def __init__(self, config=None):
        """初始化 ClientManager。

        Args:
            config: 可选配置对象（便于测试注入 mock 配置）。
        """
        self._config = config or _client_config

        # 不可变注册表快照：client_id → ClientQueues。永不就地变更，写时整体换引用。
        self._runs: Dict[str, ClientQueues] = {}
        self._wlock = threading.Lock()  # 只串行「写」（create / remove），不阻塞读

        # per-client 任务级生命周期锁（RLock）：护一次 start/teardown 事务，供 RunController
        # / api / HealthMonitor 共用串行化同 client 的启停。与 _wlock 是两把不同的锁：
        # _wlock 全局极短护换引用；_task_locks[cid] per-client 长持护跨服务事务。
        self._task_locks: Dict[str, threading.RLock] = {}
        self._task_locks_guard = threading.Lock()

        logger.info("[ClientManager] Initialized")

    # ── 任务级锁（per-client 生命周期事务锁）────────────────────

    def lock_for(self, client_id: str) -> threading.RLock:
        """返回该 client_id 的生命周期 RLock（get-or-create）。

        供 RunController.start_run / stop_run 及 HealthMonitor 共用，串行化同一 client 的
        启停事务。RLock：同线程可重入（start_run 持锁内再调 stop_run 不自死锁）。
        """
        with self._task_locks_guard:
            lk = self._task_locks.get(client_id)
            if lk is None:
                lk = threading.RLock()
                self._task_locks[client_id] = lk
            return lk

    # ── 读接口（无锁）───────────────────────────────────────────

    def get(self, client_id: str) -> Optional[ClientQueues]:
        """只读按键获取 CQ；不存在返回 None（无锁，数据面用）。"""
        return self._runs.get(client_id)

    def has_client(self, client_id: str) -> bool:
        """检查客户端是否存在（无锁）。"""
        return client_id in self._runs

    def get_client_by_task_id(self, task_id: int) -> Optional[ClientQueues]:
        """按 task_id 查 ClientQueues（扫描当前快照，无双向索引）。"""
        for cq in self._runs.values():  # 原子读引用后迭代不可变快照
            if cq.get_task_id() == task_id:
                return cq
        return None

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

    def snapshot(self) -> Mapping[str, ClientQueues]:
        """返回所有客户端的**零拷贝只读视图**（安全迭代，切勿修改）。"""
        return types.MappingProxyType(self._runs)

    def get_all_queue_depths(self) -> Dict[str, Dict[str, int]]:
        """所有客户端的队列深度统计。

        格式：{client_id: {ca_ready, ca_raw, ca_processed, has_rendered}}
        """
        runs = self._runs  # 原子读一份不可变快照
        return {cid: cq.get_queue_depths() for cid, cq in runs.items()}

    def get_client_count(self) -> int:
        """当前客户端总数（无锁）。"""
        return len(self._runs)

    def get_status_summary(self) -> Dict:
        """整体状态摘要（用于监控和调试）。"""
        snapshot = self._runs  # 不可变，无需拷贝
        total_frames = 0
        clients_status = {}
        for client_id, cq in snapshot.items():
            depths = cq.get_queue_depths()
            clients_status[client_id] = depths
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

    def set(self, client_id: str, cq: ClientQueues) -> None:
        """原子装入/替换 client_id 槽位为一个已建好的（不可变身份）CQ。

        供 `RunController.start_run` 路径：每次 run 建**新** CQ 后整体换槽（不在旧 CQ 上原地改）。
        `_wlock` 下 COW 换引用发布，读者原子读引用即看到全新对象——观察不到半建态。
        旧槽引用被丢弃，其 decoder/actor 持到释放后 GC。
        """
        with self._wlock:
            new = dict(self._runs)
            new[client_id] = cq
            self._runs = new
        logger.info(f"[ClientManager] set run: {client_id}")

    def remove(self, client_id: str, cleanup: bool = True) -> Dict[str, Any]:
        """注销客户端，可选清理其队列资源。

        `cleanup` 在换引用之后、锁外执行（不占写锁）。

        Returns:
            {client_id, removed, cleaned, error}
        """
        result: Dict[str, Any] = {
            "client_id": client_id,
            "removed": False,
            "cleaned": False,
            "error": None,
        }

        with self._wlock:
            cq = self._runs.get(client_id)
            if cq is None:
                result["error"] = "client_not_found"
                logger.warning(f"尝试移除不存在的客户端: {client_id}")
                return result
            new = dict(self._runs)  # COW 删除
            del new[client_id]
            self._runs = new
            result["removed"] = True

        # 清理在锁外执行（减少写锁持有时间）
        if cleanup:
            try:
                cq.clear()
                result["cleaned"] = True
                logger.info(f"客户端队列已清理: {client_id}")
            except Exception as e:
                result["error"] = str(e)
                logger.error("清理客户端队列失败 %s: %s", client_id, e, exc_info=True)

        logger.info(f"客户端已移除: {client_id}")
        return result

    def remove_if(
        self, client_id: str, expected_cq: ClientQueues, cleanup: bool = True
    ) -> bool:
        """对象身份 fence 删除：仅当 `registry[client_id] is expected_cq` 才移除。

        防止迟到 cleanup 误删「同键新实例」（重启/切换后装入的新 CQ）。命中即删并返回 True，
        否则不动、返回 False。清理在锁外执行。
        """
        with self._wlock:
            cur = self._runs.get(client_id)
            if cur is not expected_cq:
                return False
            new = dict(self._runs)
            del new[client_id]
            self._runs = new

        if cleanup:
            try:
                expected_cq.clear()
            except Exception as e:
                logger.error("清理客户端队列失败 %s: %s", client_id, e, exc_info=True)
        logger.info(f"客户端已移除(identity-fenced): {client_id}")
        return True

    def clear_all(self) -> List[Dict[str, Any]]:
        """清空所有客户端资源（服务关闭时的全局清理）。"""
        results = []
        for client_id in list(self._runs.keys()):  # 快照键列表
            result = self.remove(client_id, cleanup=True)
            results.append(
                {
                    "client_id": client_id,
                    "success": result["removed"] and result["cleaned"],
                    "error": result.get("error"),
                }
            )
        logger.info(f"所有客户端资源已清理 (总数: {len(results)})")
        return results


# 全局单例实例
client_manager = ClientManager()
