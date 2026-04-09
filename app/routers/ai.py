import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.models.frame import ProcessedFrame
from app.services import ai
from app.services.stream import stream_service

router = APIRouter(prefix="/ai", tags=["ai"])
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan():
    """简单AI服务生命周期管理：启动推理管理器"""
    # 启动 AI 推理服务
    ai.start()
    logger.info("[AIRouter] Inference service started")

    try:
        yield
    finally:
        # 停止 AI 推理服务（含结算告警落盘）
        ai.stop()
        logger.info("[AIRouter] Inference service stopped")
        # 推理停止后再终止流解码器（保证 in-flight 帧处理完毕）
        stream_service.shutdown()
        logger.info("[AIRouter] Stream service stopped")


@router.websocket("/video")
async def websocket_video_endpoint(websocket: WebSocket):
    """
    WebSocket端点：/ai/video?client_id=xxx
    - 要求客户端在连接时通过查询参数 `client_id` 指定自身 ID
    - 服务器持续向该 client_id 推送最新推理结果（Base64 JPEG）
    """
    # 获取 client_id
    client_id = websocket.query_params.get("client_id")
    if not client_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info(
        f"[WebSocket] 连接已建立: client_id={client_id}, remote={websocket.client}"
    )

    # 帧率控制和去重
    last_sent_timestamp = 0.0  # 上一帧的时间戳（来自帧本身）
    last_sent_time = 0.0  # 上一次发送的系统时间
    frame_interval = 1.0 / 30  # 30fps
    frames_sent = 0
    last_log_time = time.time()

    shutdown_event: asyncio.Event = websocket.app.state.shutdown_event

    try:
        while not shutdown_event.is_set():
            processed_frame: ProcessedFrame = ai.get_result(client_id, as_model=True)  # type: ignore

            if processed_frame is None:
                await asyncio.sleep(0.01)  # 减少轮询间隔
                continue

            # 去重：检查时间戳，避免重复发送同一帧
            current_timestamp = (
                processed_frame.raw_timestamp.timestamp()
                if processed_frame.raw_timestamp
                else time.time()
            )
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

            # 使用模型中的 Base64 编码图像
            try:
                data_url = (
                    f"data:image/jpeg;base64,{processed_frame.processed_frame_b64}"
                )
                await websocket.send_text(data_url)

                # 更新发送记录
                last_sent_timestamp = current_timestamp
                last_sent_time = current_time
                frames_sent += 1

                # 每5秒输出统计
                if current_time - last_log_time >= 5.0:
                    elapsed = current_time - last_log_time
                    fps = frames_sent / elapsed
                    logger.debug(
                        f"[WebSocket] client={client_id}: 发送 {frames_sent}帧/{elapsed:.1f}秒 = {fps:.1f}fps"
                    )
                    frames_sent = 0
                    last_log_time = current_time

            except WebSocketDisconnect:
                # 客户端正常断开连接
                logger.info(f"[WebSocket] 客户端正常断开: client_id={client_id}")
                break

            except (ConnectionResetError, BrokenPipeError):
                # 网络连接被重置
                logger.info(f"[WebSocket] 连接重置: client_id={client_id}")
                break

            except Exception:
                # 其他未预期的发送错误
                logger.error(
                    f"[WebSocket] 发送异常: client_id={client_id}", exc_info=True
                )
                break

    except Exception:
        # 捕获并记录未预期异常，便于诊断
        logger.error(f"[WebSocket] 未捕获异常: client_id={client_id}", exc_info=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info(f"[WebSocket] 连接已关闭: client_id={client_id}")


