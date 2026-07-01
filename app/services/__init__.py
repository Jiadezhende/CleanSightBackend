# services package

# 导出 ClientManager 单例
try:
    from .client import client_manager
except ImportError:
    client_manager = None
