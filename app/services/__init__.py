# services package
from . import ai

# 导出 ClientManager 单例
try:
    from .client import client_manager
except ImportError:
    try:
        from .client_manager import client_manager
    except ImportError:
        client_manager = None
