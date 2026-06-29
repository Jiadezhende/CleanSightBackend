"""跨服务共享的纯数据契约（dataclass / enum）。

无框架依赖（除 numpy）、无 service 逻辑、无 ORM。routers / inference / persistence /
client 均从此处取契约，依赖方向单向（domain 不依赖任何 service）。
按 concern 分文件：frame / detection / render / alarm / task。调用方从子模块显式 import。
"""
