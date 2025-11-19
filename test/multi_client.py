"""
multi_client.py

模拟多个客户端同时运行：
- camera clients (上传帧到 `/inspection/upload_frame` 或 ws `/inspection/upload_stream?client_id=...`)
- display clients (连接 `/ai/video?client_id=...` 接收推理后的 base64 jpeg)

用法示例：
python test/multi_client.py --num 50 --mode http --frame test_frame.jpg
python test/multi_client.py --num 100 --mode websocket --frame test_frame.jpg --display

注意：本脚本依赖 `websockets` 和 `aiohttp`。如果没有，请先安装：
pip install aiohttp websockets

"""

import argparse
import asyncio
import base64
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import websockets
import cv2
import numpy as np


def _color_from_index(i: int):
    # 产生可辨识的 BGR 颜色（cv2 使用 BGR）
    r = (i * 97) % 256
    g = (i * 193) % 256
    b = (i * 61) % 256
    return (int(b), int(g), int(r))


def _make_marked_frame_b64(frame_path: str, color_bgr, mark_size=24):
    """在顶部左上角绘制一个填充方块作为 client 标记，然后编码为 base64 JPG。"""
    if frame_path and os.path.exists(frame_path):
        img = cv2.imread(frame_path)
        if img is None:
            # 创建一张空白图
            img = np.zeros((480, 640, 3), dtype=np.uint8)
    else:
        img = np.zeros((480, 640, 3), dtype=np.uint8)

    # 在左上角绘制填充方块 (0,0) ~ (mark_size, mark_size)
    cv2.rectangle(img, (0, 0), (mark_size, mark_size), color_bgr, thickness=-1)

    success, buf = cv2.imencode('.jpg', img)
    if not success:
        raise RuntimeError('encode failed')
    b64 = base64.b64encode(buf.tobytes()).decode('utf-8')
    return b64


async def http_upload_worker(server_url, client_id, frame_b64, send_interval, run_event):
    url = f"{server_url}/inspection/upload_frame"
    data = {"client_id": client_id, "frame": frame_b64}
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession() as session:
        while run_event.is_set():
            try:
                async with session.post(url, data=data, timeout=timeout) as resp:
                    _ = await resp.text()
            except Exception as e:
                # 简短打印错误，继续重试
                print(f"[HTTP uploader {client_id}] error: {e}")
            await asyncio.sleep(send_interval)


async def ws_upload_worker(server_ws_url, client_id, frame_b64, send_interval, run_event):
    """WebSocket 上传器（发送已包含 client 标记的 base64 帧）"""
    uri = f"{server_ws_url}/inspection/upload_stream?client_id={client_id}"
    try:
        async with websockets.connect(uri, ping_interval=20, max_size=None) as ws:
            while run_event.is_set():
                try:
                    await ws.send(frame_b64)
                    # 等待短时确认（可为空）
                    try:
                        resp = await asyncio.wait_for(ws.recv(), timeout=2)
                    except asyncio.TimeoutError:
                        resp = None
                    # 可选：打印或记录 resp
                except Exception as e:
                    print(f"[WS uploader {client_id}] send error: {e}")
                    break
                await asyncio.sleep(send_interval)
    except Exception as e:
        print(f"[WS uploader {client_id}] connect error: {e}")


async def ws_display_worker(server_ws_url, client_id, expected_color_bgr, stats, stats_lock, run_event, output_dir=None, save_every=5):
    """Display worker: 接收推理后图像并验证左上标记颜色是否被保留。

    - expected_color_bgr: tuple(B, G, R)
    - stats: dict 用于记录每个 client 的统计
    - stats_lock: asyncio.Lock
    """
    uri = f"{server_ws_url}/ai/video?client_id={client_id}"
    try:
        async with websockets.connect(uri, ping_interval=20, max_size=None) as ws:
            count = 0
            while run_event.is_set():
                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"[WS display {client_id}] recv error: {e}")
                    break

                # 统一为 str
                if isinstance(data, (bytes, bytearray)):
                    try:
                        data = data.decode('utf-8', errors='ignore')
                    except Exception:
                        continue

                if not (isinstance(data, str) and data.startswith('data:image')):
                    continue

                try:
                    _, b64 = data.split(',', 1)
                    img_bytes = base64.b64decode(b64)
                    arr = np.frombuffer(img_bytes, np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is None:
                        raise RuntimeError('decode failed')

                    # 验证左上像素区间颜色 (取 (5,5) 位置)
                    px = frame[5, 5]
                    # px is BGR
                    expected = np.array(expected_color_bgr, dtype=np.int32)
                    diff = np.abs(px.astype(np.int32) - expected)
                    ok = bool((diff <= 20).all())

                    async with stats_lock:
                        rec = stats.setdefault(client_id, {"recv": 0, "ok": 0, "last_ok": False})
                        rec["recv"] += 1
                        if ok:
                            rec["ok"] += 1
                            rec["last_ok"] = True
                        else:
                            rec["last_ok"] = False

                    # 可选保存
                    if output_dir and (count % save_every == 0):
                        fname = os.path.join(output_dir, f"{client_id}_{int(time.time())}_{count}.jpg")
                        with open(fname, 'wb') as f:
                            f.write(img_bytes)

                    count += 1

                except Exception as e:
                    print(f"[WS display {client_id}] decode/verify error: {e}")
                    async with stats_lock:
                        rec = stats.setdefault(client_id, {"recv": 0, "ok": 0, "last_ok": False})
                        rec["recv"] += 1
                        rec["last_ok"] = False
                    continue
    except Exception as e:
        print(f"[WS display {client_id}] connect error: {e}")


def prepare_frame_b64(frame_path):
    if not os.path.exists(frame_path):
        raise FileNotFoundError(frame_path)
    with open(frame_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


async def _reporter(stats, stats_lock, run_event, interval=5):
    while run_event.is_set():
        await asyncio.sleep(interval)
        async with stats_lock:
            total = len(stats)
            ok_clients = sum(1 for v in stats.values() if v.get("last_ok"))
            total_recv = sum(v.get("recv", 0) for v in stats.values())
            total_ok = sum(v.get("ok", 0) for v in stats.values())
            print(f"[report] clients={total}, last_ok={ok_clients}, recv={total_recv}, ok={total_ok}")


async def run_multi(num_clients, server_http, server_ws, mode, frame_path, send_interval, display, output_dir):
    run_event = asyncio.Event()
    run_event.set()

    tasks = []
    stats = {}
    stats_lock = asyncio.Lock()

    # create output dir
    if display and output_dir:
        os.makedirs(output_dir, exist_ok=True)

    for i in range(num_clients):
        client_id = f"client_{i}_{uuid.uuid4().hex[:6]}"
        color = _color_from_index(i)
        # 预生成每个 client 的带标记帧 base64（减少循环开销）
        marked_b64 = _make_marked_frame_b64(frame_path, color)

        if mode == 'http':
            tasks.append(asyncio.create_task(http_upload_worker(server_http, client_id, marked_b64, send_interval, run_event)))
        else:
            tasks.append(asyncio.create_task(ws_upload_worker(server_ws, client_id, marked_b64, send_interval, run_event)))

        if display:
            # start display client for each one (can be heavy if many clients)
            tasks.append(asyncio.create_task(ws_display_worker(server_ws, client_id, color, stats, stats_lock, run_event, output_dir)))

    # 启动汇报任务
    tasks.append(asyncio.create_task(_reporter(stats, stats_lock, run_event, interval=5)))

    print(f"启动 {num_clients} 个上传客户端 (mode={mode}), display={display}")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--num', type=int, default=10, help='并发客户端数量')
    parser.add_argument('--mode', choices=['http', 'websocket'], default='http')
    parser.add_argument('--frame', default='test_frame.jpg', help='用于上传的静态帧')
    parser.add_argument('--send-interval', type=float, default=0.5, help='每个客户端发送帧的间隔(s)')
    parser.add_argument('--server-http', default='http://127.0.0.1:8000', help='服务 HTTP 地址')
    parser.add_argument('--server-ws', default='ws://127.0.0.1:8000', help='服务 WS 地址')
    parser.add_argument('--display', action='store_true', help='是否为每个客户端启动 display (接收推理结果)')
    parser.add_argument('--output-dir', default='multi_output', help='保存接收帧的目录（若 --display）')

    args = parser.parse_args()

    # 使用原始文件路径，run_multi 会为每个 client 生成带标记的帧
    frame_path = args.frame

    try:
        asyncio.run(run_multi(args.num, args.server_http, args.server_ws, args.mode, frame_path, args.send_interval, args.display, args.output_dir))
    except KeyboardInterrupt:
        print('已中断')


if __name__ == '__main__':
    main()
