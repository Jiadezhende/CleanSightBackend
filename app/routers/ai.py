import asyncio
import base64
import logging
import time
from contextlib import asynccontextmanager

import cv2
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.client import client_manager
from app.services.inference.instance import inference_manager
from app.services.stream import stream_service

router = APIRouter(prefix="/ai", tags=["ai"])
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan():
    """简单AI服务生命周期管理：启动推理管理器"""
    # 启动 AI 推理服务
    inference_manager.start()
    logger.info("[AIRouter] Inference service started")

    try:
        yield
    finally:
        # lifespan finally 执行时 uvicorn 已 cancel 所有 WebSocket 任务，
        # 事件循环无其他等待方，直接同步调用即可。
        try:
            inference_manager.stop()
            logger.info("[AIRouter] Inference service stopped")
        except Exception:
            logger.exception("[AIRouter] Error stopping inference service")
        try:
            stream_service.shutdown()
            logger.info("[AIRouter] Stream service stopped")
        except Exception:
            logger.exception("[AIRouter] Error shutting down stream service")


@router.websocket("/video")
async def websocket_video_endpoint(websocket: WebSocket):
    """
    WebSocket端点：两种并列的请求模式（互斥，task_id 优先），非新旧之分：
    - `?task_id=xxx` → `client_manager.get(task_id)`，锁定**某一次具体 run**（不可变运行键）；
      run 结束即止、不跟随新任务。适合溯源 / 针对某次任务的监看。
    - `?client_id=<source_ip>` → 每轮 `find_by_source_ip` 解析该**点位**的当前 live run
      （命中多个取最晚启动者），任务来了显示、走了黑屏、换 run 自动跟随。适合大屏 / 固定点位常亮。
    每轮读其最新渲染结果（Base64 JPEG）持续推送。两参皆缺 → 关闭（1008）。

    协议：帧为 `data:image/jpeg;base64,...` 文本；当从"曾推帧 → 无 run"跳变时
    额外发一次 `{"type":"idle"}` 控制帧，供大屏清屏黑屏（任务结束）。前端按前缀
    区分：`data:` 开头当图渲染，否则按 JSON 控制帧解析。持续无 run 期间保持静默
    （不重复发 idle），任务中卡顿（cq 在但无新帧）保持最后一帧、不发 idle。
    """
    # 双模解析（task_id 优先）：构造 per-loop 解析器 + 日志标签
    task_id_raw = websocket.query_params.get("task_id")
    client_id = websocket.query_params.get("client_id")  # 点位模式：source_ip（非遗留参）

    if task_id_raw is not None:
        try:
            task_id = int(task_id_raw)
        except ValueError:
            await websocket.close(code=1008)
            return
        resolve = lambda: client_manager.get(task_id)  # noqa: E731
        label = f"task_id={task_id}"
    elif client_id:
        resolve = lambda: client_manager.find_by_source_ip(client_id)  # noqa: E731
        label = f"client_id={client_id}"
    else:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info(f"[WebSocket] 连接已建立: {label}, remote={websocket.client}")

    # 帧率控制和去重
    last_sent_timestamp = 0.0  # 上一帧的时间戳（来自帧本身）
    last_sent_time = 0.0  # 上一次发送的系统时间
    frame_interval = 1.0 / 30  # 30fps
    frames_sent = 0
    last_log_time = time.time()
    streaming = False  # 是否处于推帧态：仅在"曾推帧 → 无 run"跳变时发一次 idle

    shutdown_event: asyncio.Event = websocket.app.state.shutdown_event

    # 后台接收任务：监听 CLOSE 帧或客户端断开。
    # 当 uvicorn 优雅关闭时它会向 WebSocket 发送 CLOSE 帧，
    # receive() 将返回 {"type": "websocket.disconnect"}，使本任务结束。
    # 这样即使 shutdown_event 尚未置位，主循环也能及时退出。
    async def _recv_until_disconnect():
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
        except Exception:
            return

    disconnect_task = asyncio.create_task(_recv_until_disconnect())

    try:
        while not shutdown_event.is_set() and not disconnect_task.done():
            # 每轮解析当前 run（task_id 直查 / source_ip 匹配首个）
            cq = resolve()

            # 无 run（无任务 / 任务已结束）：仅在"曾推帧 → 现无 run"跳变时发一次
            # idle 让大屏清屏黑屏；持续无 run 期间保持静默、不重复发（近零流量）。
            # 与"任务中卡顿"区分：那时 cq 仍在，走下方分支保持最后一帧、不发 idle。
            if cq is None:
                if streaming:
                    streaming = False
                    try:
                        await websocket.send_text('{"type":"idle"}')
                    except Exception:
                        break
                await asyncio.sleep(0.05)
                continue

            frame = cq.get_latest_rendered()
            if frame is None:
                # cq 在但暂无渲染帧（任务刚起 / 卡顿）：保持上一帧，不发 idle
                await asyncio.sleep(0.01)  # 减少轮询间隔
                continue

            # 去重：在编码前按帧时间戳判重，避免重复编码同一帧
            current_timestamp = frame.timestamp
            if current_timestamp <= last_sent_timestamp:
                await asyncio.sleep(0.01)
                continue

            # 帧率控制：确保发送间隔不小于 frame_interval
            current_time = time.time()
            if last_sent_time > 0:
                time_since_last = current_time - last_sent_time
                if time_since_last < frame_interval:
                    await asyncio.sleep(frame_interval - time_since_last)
                    current_time = time.time()  # 更新时间

            # 边界编码：domain Frame → JPEG base64 data URL（仅此一处，内联）
            try:
                _, buf = cv2.imencode(".jpg", frame.frame)
                b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
                data_url = f"data:image/jpeg;base64,{b64}"
                await websocket.send_text(data_url)

                # 更新发送记录
                last_sent_timestamp = current_timestamp
                last_sent_time = current_time
                frames_sent += 1
                streaming = True  # 进入/维持推帧态：任务结束转 None 时才发 idle

                # 每5秒输出统计
                if current_time - last_log_time >= 5.0:
                    elapsed = current_time - last_log_time
                    fps = frames_sent / elapsed
                    logger.debug(
                        f"[WebSocket] {label}: 发送 {frames_sent}帧/{elapsed:.1f}秒 = {fps:.1f}fps"
                    )
                    frames_sent = 0
                    last_log_time = current_time

            except WebSocketDisconnect:
                # 客户端正常断开连接
                logger.info(f"[WebSocket] 客户端正常断开: {label}")
                break

            except (ConnectionResetError, BrokenPipeError):
                # 网络连接被重置
                logger.info(f"[WebSocket] 连接重置: {label}")
                break

            except Exception:
                # 其他未预期的发送错误
                logger.error(
                    f"[WebSocket] 发送异常: {label}", exc_info=True
                )
                break

    except asyncio.CancelledError:
        # uvicorn 超时后强制取消任务时触发（6s 优雅关闭超时）
        # 不重新抛出：handler 正常返回，避免 uvicorn 记录 "Exception in ASGI application"
        logger.info(f"[WebSocket] 任务被取消（服务关闭）: {label}")
    except Exception:
        # 捕获并记录未预期异常，便于诊断
        logger.error(f"[WebSocket] 未捕获异常: {label}", exc_info=True)
    finally:
        disconnect_task.cancel()
        try:
            await asyncio.wait_for(disconnect_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"[WebSocket] 连接已关闭: {label}")


