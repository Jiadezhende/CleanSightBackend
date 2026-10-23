"""health_monitor 自用的 topic 声明。

    sess.channel(RECONNECT_STATE).get()   # None = 不在重连中

重连状态挂在 run 上：run 关闭（含 `/terminate` 正在重连的 run）即随之释放，不留残留条目。
"""

from app.services.session.core import Latest, Topic

# health_monitor 自写自读：进入重连时写 ReconnectState，重连成功 / 放弃时 clear
RECONNECT_STATE = Topic("RECONNECT_STATE", Latest)
