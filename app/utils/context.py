"""
CleanSight 简单上下文管理（基于 threading.local）

提供跨函数传递 client_id 的简单机制（可选使用）

设计原则：
- 使用 threading.local() 而非复杂的 ContextVar
- 显式传递参数优先，上下文传递作为补充
- 适用于并发 <15 的小规模系统
"""

import threading
from typing import Optional

# 线程本地存储
_context = threading.local()


def set_client_id(client_id: str):
    """设置当前线程的 client_id

    Args:
        client_id: 客户端标识

    示例:
        # 在函数入口设置
        def process_task(client_id: str):
            set_client_id(client_id)
            # ... 后续函数可以通过 get_client_id() 获取
    """
    _context.client_id = client_id


def get_client_id() -> Optional[str]:
    """获取当前线程的 client_id

    Returns:
        str: client_id，如果未设置则返回 None

    示例:
        client_id = get_client_id()
        if client_id:
            logger.info(f"Processing for client: {client_id}")
    """
    return getattr(_context, "client_id", None)


def clear_client_id():
    """清除当前线程的 client_id

    示例:
        try:
            set_client_id(client_id)
            # ... 处理任务
        finally:
            clear_client_id()  # 清理上下文
    """
    if hasattr(_context, "client_id"):
        delattr(_context, "client_id")


def set_task_id(task_id: int):
    """设置当前线程的 task_id

    Args:
        task_id: 任务ID
    """
    _context.task_id = task_id


def get_task_id() -> Optional[int]:
    """获取当前线程的 task_id

    Returns:
        int: task_id，如果未设置则返回 None
    """
    return getattr(_context, "task_id", None)


def clear_task_id():
    """清除当前线程的 task_id"""
    if hasattr(_context, "task_id"):
        delattr(_context, "task_id")


def clear_context():
    """清除当前线程的所有上下文

    示例:
        try:
            set_client_id(client_id)
            set_task_id(task_id)
            # ... 处理任务
        finally:
            clear_context()  # 一次性清理所有上下文
    """
    clear_client_id()
    clear_task_id()


# ============================================================================
# 上下文管理器（推荐使用）
# ============================================================================


class ClientContext:
    """客户端上下文管理器

    使用 with 语句自动管理上下文的设置和清理

    示例:
        with ClientContext(client_id="192.168.1.100", task_id=123):
            # ... 在此作用域内，所有函数都可以获取 client_id 和 task_id
            process_frame()
            save_result()
        # 退出 with 语句后自动清理上下文
    """

    def __init__(self, client_id: str = None, task_id: int = None): # type: ignore
        """初始化上下文管理器

        Args:
            client_id: 客户端ID
            task_id: 任务ID
        """
        self.client_id = client_id
        self.task_id = task_id
        self.prev_client_id = None
        self.prev_task_id = None

    def __enter__(self):
        """进入上下文：保存旧值，设置新值"""
        # 保存旧值（支持嵌套上下文）
        self.prev_client_id = get_client_id()
        self.prev_task_id = get_task_id()

        # 设置新值
        if self.client_id is not None:
            set_client_id(self.client_id)
        if self.task_id is not None:
            set_task_id(self.task_id)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文：恢复旧值"""
        # 恢复旧值
        if self.prev_client_id is not None:
            set_client_id(self.prev_client_id)
        else:
            clear_client_id()

        if self.prev_task_id is not None:
            set_task_id(self.prev_task_id)
        else:
            clear_task_id()

        return False  # 不抑制异常


# ============================================================================
# 使用建议
# ============================================================================

"""
使用建议：

1. **优先使用显式参数传递**（推荐）：
   ```python
   def process_frame(client_id: str, frame: np.ndarray):
       logger.info(f"Processing frame for {client_id}")
       # ...
   ```

2. **需要跨多层函数传递时使用上下文**：
   ```python
   with ClientContext(client_id=client_id, task_id=task_id):
       process_pipeline()  # 内部多个函数可以通过 get_client_id() 获取
   ```

3. **装饰器自动提取 client_id**：
   - log_call、retry、timing 等装饰器会自动尝试从参数或上下文中提取 client_id
   - 因此即使使用上下文传递，也能正确记录到日志

4. **线程安全**：
   - threading.local() 保证每个线程有独立的上下文
   - 多线程环境下不会互相干扰
"""
