import os
import subprocess
import threading
import time
import selectors
import queue
import traceback
from typing import Optional, Dict, Callable

import numpy as np
import cv2
import logging

logger = logging.getLogger("app.services.stream_service")

from app.services import ai

# configuration
FFMPEG_BIN = os.environ.get("FFMPEG_PATH", "ffmpeg")
DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_CHANNELS = 3
CHUNK_READ = 32768
PER_STREAM_MAX_PENDING = 3


class FFmpegDecoder:
    def __init__(self, manager, client_id: str, stream_url: str, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT, fps=30, pix_fmt="bgr24", protocol_opts=None, auto_restart=True, max_restarts=5):
        self.manager = manager
        self.client_id = client_id
        self.stream_url = stream_url
        self.width = width
        self.height = height
        self.fps = fps
        self.pix_fmt = pix_fmt
        self.protocol_opts = protocol_opts or []
        self.auto_restart = auto_restart
        self.max_restarts = max_restarts

        self.frame_size = width * height * DEFAULT_CHANNELS
        self.buffer = bytearray()
        self.proc: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._stderr_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None
        self.restart_count = 0
        self.lock = threading.Lock()

        # metrics
        self.frames_received = 0
        self.frames_dropped = 0
        self.logger = logging.getLogger(f"app.services.stream_service.FFmpegDecoder.{self.client_id}")

    def _build_cmd(self):
        cmd = [FFMPEG_BIN]
        cmd += self.protocol_opts
        cmd += [
            "-i", self.stream_url,
            "-map", "0:v:0",
            "-vsync", "drop",
            "-vf", f"scale={self.width}:{self.height},fps={self.fps}",
            "-pix_fmt", self.pix_fmt,
            "-f", "rawvideo",
            "pipe:1",
        ]
        return cmd

    def start(self):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._stop_event.clear()
            self.buffer = bytearray()
            cmd = self._build_cmd()
            try:
                self.logger.info("starting ffmpeg: %s", " ".join(cmd))
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                self.logger.info("ffmpeg started pid=%s", getattr(self.proc, 'pid', None))
            except FileNotFoundError:
                self.logger.exception("ffmpeg binary not found")
                raise RuntimeError(f"FFmpeg not found: {FFMPEG_BIN}")

            # set non-blocking on POSIX
            if os.name != "nt":
                try:
                    fd = self.proc.stdout.fileno()
                    os.set_blocking(fd, False)
                except Exception:
                    pass

            self._stderr_thread = threading.Thread(target=self._read_stderr_loop, daemon=True, name=f"stderr-{self.client_id}")
            self._stderr_thread.start()

            if os.name == "nt":
                self._reader_thread = threading.Thread(target=self._windows_reader_loop, daemon=True, name=f"reader-{self.client_id}")
                self._reader_thread.start()

            self.restart_count = 0
            self.logger.debug("decoder start complete")
    def stop(self, wait: float = 2.0):
        with self.lock:
            self._stop_event.set()
            if self.proc is None:
                return
            try:
                if self.proc.poll() is None:
                    self.logger.info("terminating ffmpeg pid=%s", getattr(self.proc, 'pid', None))
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=wait)
                    except Exception:
                        self.logger.warning("ffmpeg did not exit gracefully, killing pid=%s", getattr(self.proc, 'pid', None))
                        self.proc.kill()
                try:
                    if self.proc.stdout:
                        self.proc.stdout.close()
                except Exception:
                    pass
                try:
                    if self.proc.stderr:
                        self.proc.stderr.close()
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self.proc = None
                self.logger.info("decoder stopped")

    def restart(self):
        with self.lock:
            self.logger.info("restarting decoder (count before restart=%s)", self.restart_count)
            self.stop()
            time.sleep(0.5)
            try:
                self.start()
                self.restart_count += 1
                self.logger.info("restart succeeded (count=%s)", self.restart_count)
            except Exception:
                self.logger.exception("restart failed")

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def on_stdout_ready(self):
        if not self.is_alive():
            return
        try:
            fd = self.proc.stdout.fileno()
            chunk = os.read(fd, CHUNK_READ)
            if not chunk:
                return
            self._process_bytes(chunk)
        except BlockingIOError:
            return
        except Exception:
            self.logger.exception("error reading stdout")

    def _windows_reader_loop(self):
        proc = self.proc
        if proc is None:
            return
        try:
            while not self._stop_event.is_set() and self.is_alive():
                chunk = proc.stdout.read(CHUNK_READ)
                if not chunk:
                    break
                self._process_bytes(chunk)
        except Exception:
            self.logger.exception("windows reader exception")

    def _read_stderr_loop(self):
        proc = self.proc
        if proc is None:
            return
        try:
            while not self._stop_event.is_set() and self.is_alive():
                line = proc.stderr.readline()
                if not line:
                    break
                try:
                    s = line.decode(errors='ignore').strip()
                except Exception:
                    s = str(line)
                # log at debug level to avoid noisy INFO
                self.logger.debug("ffmpeg stderr: %s", s)
        except Exception:
            pass

    def _standardize_frame(self, frm: np.ndarray) -> np.ndarray:
        if frm is None:
            return frm
        if not isinstance(frm, np.ndarray):
            frm = np.array(frm)
        if frm.ndim == 2:
            frm = cv2.cvtColor(frm, cv2.COLOR_GRAY2BGR)
        if frm.shape[2] == 4:
            frm = frm[:, :, :3]
        if frm.dtype != np.uint8:
            try:
                frm = np.clip(frm, 0, 255).astype(np.uint8)
            except Exception:
                frm = frm.astype(np.uint8, copy=False)
        MODEL_INPUT_WIDTH = int(os.environ.get('MODEL_INPUT_WIDTH', 0))
        MODEL_INPUT_HEIGHT = int(os.environ.get('MODEL_INPUT_HEIGHT', 0))
        MODEL_INPUT_COLOR = os.environ.get('MODEL_INPUT_COLOR', 'bgr').lower()
        if MODEL_INPUT_WIDTH > 0 and MODEL_INPUT_HEIGHT > 0:
            try:
                frm = cv2.resize(frm, (MODEL_INPUT_WIDTH, MODEL_INPUT_HEIGHT), interpolation=cv2.INTER_LINEAR)
            except Exception:
                pass
        if MODEL_INPUT_COLOR == 'rgb':
            try:
                frm = cv2.cvtColor(frm, cv2.COLOR_BGR2RGB)
            except Exception:
                pass
        frm = np.ascontiguousarray(frm)
        return frm

    def _process_bytes(self, chunk: bytes):
        if not chunk:
            return
        self.buffer += chunk
        while len(self.buffer) >= self.frame_size:
            frame_data = bytes(self.buffer[: self.frame_size])
            del self.buffer[: self.frame_size]
            # backpressure
            pending = self.manager.get_pending_count(self.client_id)
            if pending >= PER_STREAM_MAX_PENDING:
                self.frames_dropped += 1
                # log occasional drop
                if self.frames_dropped % 50 == 0:
                    self.logger.warning("dropped %s frames due to backpressure", self.frames_dropped)
                continue
            try:
                arr = np.frombuffer(frame_data, dtype=np.uint8).reshape((self.height, self.width, 3))
                std = self._standardize_frame(arr)
                ai.submit_frame(self.client_id, std)
                self.frames_received += 1
                # log every N frames to observe liveness
                if self.frames_received % 300 == 0:
                    self.logger.info("received %s frames", self.frames_received)
            except Exception:
                self.frames_dropped += 1
                self.logger.exception("error processing frame bytes")


class StreamService:
    def __init__(self):
        self.decoders: Dict[str, FFmpegDecoder] = {}
        self.sel = selectors.DefaultSelector() if os.name != 'nt' else None
        self.lock = threading.Lock()
        self.metrics = {}

    def start_stream(self, client_id: str, stream_url: str, fps: int = 30, protocol: str = 'RTMP'):
        with self.lock:
            if client_id in self.decoders:
                raise RuntimeError(f"stream {client_id} already started")
            logger.info("start_stream client=%s protocol=%s url=%s", client_id, protocol, stream_url)
            protocol_opts = []
            if protocol == 'RTSP':
                protocol_opts = [
                    "-rtsp_transport", "udp",
                    "-fflags", "nobuffer",
                    "-flags", "low_delay",
                    "-analyzeduration", "1000000",
                    "-probesize", "1000000",
                ]
            dec = FFmpegDecoder(manager=self, client_id=client_id, stream_url=stream_url, fps=fps, protocol_opts=protocol_opts)
            self.decoders[client_id] = dec
            self.metrics[client_id] = {"frames_received": 0, "frames_dropped": 0, "restarts": 0}
            dec.start()
            logger.info("stream started client=%s pid=%s", client_id, getattr(dec.proc, 'pid', None))
            if self.sel is not None and dec.proc and dec.proc.stdout:
                try:
                    self.sel.register(dec.proc.stdout.fileno(), selectors.EVENT_READ, data=dec)
                except Exception:
                    pass

    def stop_stream(self, client_id: str):
        with self.lock:
            dec = self.decoders.pop(client_id, None)
            if not dec:
                return
            logger.info("stopping stream client=%s", client_id)
            if self.sel is not None and dec.proc and dec.proc.stdout:
                try:
                    self.sel.unregister(dec.proc.stdout.fileno())
                except Exception:
                    pass
            dec.stop()
            self.metrics.pop(client_id, None)
            logger.info("stream stopped client=%s", client_id)

    def has_stream(self, client_id: str) -> bool:
        with self.lock:
            dec = self.decoders.get(client_id)
            return dec is not None and dec.is_alive()

    def get_pending_count(self, client_id: str) -> int:
        # placeholder for pending counter; keep simple
        return 0

    def run_once(self, timeout: float = 0.05):
        if self.sel is None:
            return
        events = self.sel.select(timeout=timeout)
        for key, mask in events:
            dec: FFmpegDecoder = key.data
            try:
                dec.on_stdout_ready()
            except Exception:
                traceback.print_exc()


# singleton service instance
stream_service = StreamService()
