"""storage/utils —— 数据层通用能力，供 storage 各域文件共用。

    from app.storage.utils import root as _root   # 存储根、域名白名单、目录定位
    from app.storage.utils import fs as _fs       # 盘上原语：整体替换、原子删除、建一级目录

标记型 `__init__.py`：纯 docstring、零 re-export，消费方走深路径。
"""
