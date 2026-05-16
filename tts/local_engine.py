# @purpose: 本地 TTS 引擎适配（在主 app 侧；通过子进程+HTTP 调用 runtime/serve.py 完成实际合成）
# @layer: adapter
# @contract:
#   - LocalTTSEngine(runtime_dir, model_store, output_format, ffmpeg_path, backend).{
#       output_extension, synthesize, list_voices, close}
# @depends:
#   - asyncio, atexit, contextlib, json, logging, socket, subprocess, sys (stdlib)
#   - urllib.request, urllib.error (stdlib HTTP client)
#   - pathlib
#   - ../contract/models.py: Emotion, TTSModel
#   - ../contract/errors.py: TTSError
#   - ../contract/ports.py: ModelStore
#   - ./_ffmpeg.py: resolve_ffmpeg, transcode
# @invariants:
#   - 平台门禁：sys.platform != 'win32' 时 synthesize 抛 TTSError("暂不支持")；list_voices 不抛
#   - 运行时门禁：runtime_dir/VERSION 不存在 → synthesize 抛 TTSError 提示去「模型管理」装运行时
#   - 模型门禁：voice 在 ModelStore 中不存在 → synthesize 抛 TTSError
#   - 子进程：一个 LocalTTSEngine 实例持有 1 个长驻 serve.py 子进程；首次 synthesize 时懒启动
#     - 端口选择：bind 到 127.0.0.1:0 取得 OS 分配的端口后立即 close 再传给子进程（轻微 race，本机可接受）
#     - 启动后轮询 GET /health 最多 30s；超时 → TTSError + terminate
#     - 子进程死亡时下一次 synthesize 自动重启
#     - close() 主动结束子进程；模块 atexit 钩兜底
#   - HTTP 调用：urllib + asyncio.to_thread；POST /synthesize 收 audio/wav bytes
#   - voice 入参假定已被 dispatch 层剥掉 "local:" 前缀（裸 model_id）
#   - output_extension：runtime 产 wav；ogg 经 _ffmpeg.transcode 转码（缺 ffmpeg 时构造期降级 wav）

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from contract.errors import TTSError
from contract.models import Emotion
from contract.ports import ModelStore

from ._ffmpeg import resolve_ffmpeg, transcode

logger = logging.getLogger(__name__)

_VERSION_FILE = "VERSION"
_SERVE_PY = "serve.py"
_PYTHON_EXE = Path("python") / "python.exe"  # 运行时包内 embeddable python 位置
_HEALTH_TIMEOUT_SEC = 30.0
_HEALTH_INTERVAL_SEC = 0.3
_SYNTHESIZE_TIMEOUT_SEC = 120.0

# 进程退出时兜底清理所有引擎的子进程
_ALL_PROCS: list[subprocess.Popen] = []
_ALL_PROCS_LOCK = threading.Lock()


def _cleanup_all_procs() -> None:
    with _ALL_PROCS_LOCK:
        procs = list(_ALL_PROCS)
        _ALL_PROCS.clear()
    for p in procs:
        if p.poll() is None:
            with contextlib.suppress(Exception):
                p.terminate()


atexit.register(_cleanup_all_procs)


def _pick_free_port() -> int:
    """让 OS 分配一个 free port；存在轻微 race（释放与子进程 bind 之间），本机使用可接受。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http_get_json(url: str, timeout: float = 2.0) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_post_json_for_bytes(url: str, body: dict, timeout: float) -> bytes:
    data = json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "audio/wav"},
        method="POST",
    )
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


class LocalTTSEngine:
    def __init__(
        self,
        runtime_dir: Path,
        model_store: ModelStore,
        output_format: str = "ogg",
        ffmpeg_path: Optional[str] = None,
        backend: str = "auto",
    ) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._models = model_store
        self._ffmpeg = resolve_ffmpeg(ffmpeg_path)
        requested = output_format
        fmt = output_format if output_format in ("ogg", "mp3", "wav") else "wav"
        if fmt == "ogg" and self._ffmpeg is None:
            fmt = "wav"  # local 原生产 wav；缺 ffmpeg 时无法转 ogg，降级到 wav
        elif fmt == "mp3":
            # local 无 mp3 编码器；与 edge 不一致时仅当 single-engine 配置才走 mp3
            fmt = "wav"
        self._format = fmt
        self._backend = backend
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._spawn_lock = asyncio.Lock()
        logger.info(
            "local_tts engine: requested=%s effective=%s backend=%s runtime=%s ffmpeg=%s",
            requested, self._format, self._backend, self._runtime_dir,
            self._ffmpeg or "<none>",
        )

    @property
    def output_extension(self) -> str:
        return self._format

    async def list_voices(self) -> list[dict]:
        """列出已安装的本地模型作为可选 voice；不依赖运行时是否安装。"""
        installed = self._models.list_installed()
        return [
            {
                "id": m.id,
                "name": m.name or m.id,
                "lang": m.language,
                "character": m.character,
                "description": m.description,
                "license": m.license,
                "license_url": m.license_url,
            }
            for m in installed
        ]

    def close(self) -> None:
        """主动终止子进程；invalidate_caches 时由调用方触发。"""
        proc = self._proc
        self._proc = None
        self._port = None
        if proc is None:
            return
        with _ALL_PROCS_LOCK:
            with contextlib.suppress(ValueError):
                _ALL_PROCS.remove(proc)
        if proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    proc.kill()

    async def synthesize(
        self,
        text: str,
        voice: str,
        emotion: Emotion,
        rate: float = 1.0,
        pitch: float = 1.0,
        volume: float = 1.0,
    ) -> bytes:
        # 1. 平台门禁
        if sys.platform != "win32":
            raise TTSError(
                f"本地 TTS 引擎当前仅支持 Windows，检测到平台 {sys.platform}（暂不支持）"
            )
        # 2. 运行时门禁
        if not (self._runtime_dir / _VERSION_FILE).exists():
            raise TTSError(
                "本地 TTS 运行时未安装。请到「模型管理」页点击「安装本地 TTS 引擎」"
            )
        # 3. 输入校验
        if not voice:
            raise TTSError("voice 为空")
        if not text.strip():
            raise TTSError("文本为空")
        # 4. 模型门禁
        model = self._models.get(voice)
        if model is None:
            raise TTSError(
                f"未找到本地模型: {voice}。请到「模型管理」页下载或导入对应模型"
            )
        # 5. 确保 runtime 子进程运行
        port = await self._ensure_runtime_running()
        # 6. 调 runtime HTTP API 拿 WAV
        try:
            wav = await asyncio.to_thread(
                _http_post_json_for_bytes,
                f"http://127.0.0.1:{port}/synthesize",
                {
                    "text": text,
                    "model_id": voice,
                    "emotion": emotion.value if hasattr(emotion, "value") else str(emotion),
                    "rate": rate,
                    "pitch": pitch,
                    "volume": volume,
                },
                _SYNTHESIZE_TIMEOUT_SEC,
            )
        except HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                detail = e.read().decode("utf-8", errors="ignore")
            raise TTSError(f"本地 TTS 合成失败（HTTP {e.code}）：{detail or e.reason}") from e
        except URLError as e:
            # 子进程可能死了，clear 以便下次重启
            self.close()
            raise TTSError(f"本地 TTS 运行时通信失败：{e.reason}") from e
        # 7. 转码（如需要）
        if self._format == "wav":
            return wav
        return await transcode(wav, self._format, self._ffmpeg)

    async def _ensure_runtime_running(self) -> int:
        """懒启动 runtime 子进程；幂等。"""
        # fast path：进程仍存活
        if self._proc is not None and self._proc.poll() is None and self._port is not None:
            return self._port
        async with self._spawn_lock:
            # double-check 锁内重检
            if self._proc is not None and self._proc.poll() is None and self._port is not None:
                return self._port
            # 上次的进程死了，清理
            if self._proc is not None:
                self.close()

            python_exe = self._runtime_dir / _PYTHON_EXE
            serve_py = self._runtime_dir / _SERVE_PY
            if not python_exe.exists():
                raise TTSError(f"runtime python 不存在：{python_exe}（运行时包损坏？请重装）")
            if not serve_py.exists():
                raise TTSError(f"runtime serve.py 不存在：{serve_py}（运行时包损坏？请重装）")

            port = _pick_free_port()
            model_root = self._models.root()
            cmd = [
                str(python_exe),
                str(serve_py),
                "--port", str(port),
                "--model-root", str(model_root),
                "--backend", self._backend,
            ]
            logger.info("spawn local-tts runtime: %s", " ".join(cmd))
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self._runtime_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as e:
                raise TTSError(f"启动本地 TTS 运行时失败：{e}") from e
            with _ALL_PROCS_LOCK:
                _ALL_PROCS.append(proc)

            # 轮询 /health
            try:
                await self._wait_health(port, proc)
            except TTSError:
                with contextlib.suppress(Exception):
                    proc.terminate()
                with _ALL_PROCS_LOCK, contextlib.suppress(ValueError):
                    _ALL_PROCS.remove(proc)
                raise

            self._proc = proc
            self._port = port
            return port

    async def _wait_health(self, port: int, proc: subprocess.Popen) -> None:
        url = f"http://127.0.0.1:{port}/health"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _HEALTH_TIMEOUT_SEC
        while loop.time() < deadline:
            if proc.poll() is not None:
                # 进程提前死亡，捞 stderr 提示
                stderr = ""
                if proc.stderr is not None:
                    with contextlib.suppress(Exception):
                        stderr = proc.stderr.read() or ""
                raise TTSError(
                    f"本地 TTS 运行时启动失败（退出码 {proc.returncode}）：{stderr.strip()[:500]}"
                )
            try:
                resp = await asyncio.to_thread(_http_get_json, url, 1.0)
                if resp.get("status") == "ready":
                    logger.info("runtime ready: %s", resp)
                    return
            except (URLError, HTTPError, OSError):
                pass
            await asyncio.sleep(_HEALTH_INTERVAL_SEC)
        raise TTSError(f"本地 TTS 运行时 {_HEALTH_TIMEOUT_SEC:.0f}s 内未就绪")

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()
