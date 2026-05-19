# @purpose: 本地 TTS 引擎适配（在主 app 侧；通过子进程+HTTP 调用 runtime/serve.py 完成实际合成）
# @layer: adapter
# @contract:
#   - LocalTTSEngine(runtime_dir, model_store, output_format, ffmpeg_path, target).{
#       output_extension, synthesize, list_voices, close}
# @depends:
#   - asyncio, atexit, contextlib, json, logging, socket, subprocess, sys (stdlib)
#   - urllib.request, urllib.error (stdlib HTTP client)
#   - pathlib
#   - ../contract/models.py: Emotion, TTSModel
#   - ../contract/errors.py: TTSError
#   - ../contract/ports.py: ModelStore
#   - （无 ffmpeg 依赖：合成路径不转码，导出阶段才走 _ffmpeg.transcode）
# @invariants:
#   - 平台门禁：sys.platform != 'win32' 时 synthesize 抛 TTSError("暂不支持")；list_voices 不抛
#   - 运行时门禁：runtime_dir/VERSION 不存在 → synthesize 抛 TTSError 提示去「软件设置」装运行时
#   - 模型门禁：voice 在 ModelStore 中不存在 → synthesize 抛 TTSError
#   - 子进程：一个 LocalTTSEngine 实例持有 1 个长驻 serve.py 子进程；首次 synthesize 时懒启动
#     - 端口选择：bind 到 127.0.0.1:0 取得 OS 分配的端口后立即 close 再传给子进程（轻微 race，本机可接受）
#     - 启动后轮询 GET /health 最多 30s；超时 → TTSError + terminate
#     - 子进程死亡时下一次 synthesize 自动重启
#     - close() 主动结束子进程；模块 atexit 钩兜底
#   - HTTP 调用：urllib + asyncio.to_thread；POST /synthesize 收 audio/wav bytes
#   - voice 入参假定已被 dispatch 层剥掉 "local:" 前缀（裸 model_id）
#   - output_extension：固定 "wav"（runtime 原生输出，不再 transcode；导出阶段统一转码）
#   - backend 自动推导：target → serve.py --backend auto（serve.py detect_backend 自动选）；
#     用户不再可见 backend 维度，旧 settings.json 的 backend 字段被 Pydantic 忽略

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import socket
import subprocess
import os
import sys
import threading
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from contract.errors import TTSError
from contract.models import Emotion
from contract.ports import ModelStore

# 不再 import _ffmpeg：合成路径不转码（设计：与导出解耦）

logger = logging.getLogger(__name__)

_VERSION_FILE = "VERSION"
_SERVE_PY = "serve.py"
_PYTHON_EXE = Path("python") / "python.exe"  # 运行时包内 embeddable python 位置
_HEALTH_TIMEOUT_SEC = 30.0
_HEALTH_INTERVAL_SEC = 0.3
# 默认单次合成 HTTP 超时（秒）；可经构造参数覆盖（cfg.tts.local.synthesize_timeout_sec）
# 首次合成需懒加载基础模型（BERT+HuBERT≈1.5GB）+ voice 权重（GPT≈150MB+SoVITS≈80MB）
# 加上 CPU 推理本身，单次首调可能 300-600s；后续调用快得多但仍可能 30s+（长句）
_DEFAULT_SYNTHESIZE_TIMEOUT_SEC = 600.0


def _translate_runtime_error(raw: str) -> str:
    """把 runtime 返回的英文/技术错误映射到用户友好的中文提示。

    raw 是 runtime serve.py 抛出来的错误（HTTPException.detail 或 RuntimeError 文案）。
    匹配按从具体到通用顺序；命中则返回"中文提示（原文片段）"。
    没命中则原文回传。
    """
    if not raw:
        return "未知错误"
    low = raw.lower()

    # 显存/内存类
    if "out of memory" in low or "oom" in low or "outofmemoryerror" in low:
        if "cuda" in low or "gpu" in low:
            return f"显存不足，请关闭其他占用 GPU 的程序后重试（原文：{raw[:200]}）"
        return f"内存不足，请关闭部分程序后重试（原文：{raw[:200]}）"

    # GPU 不可用
    if "no cuda gpus are available" in low or "cuda is not available" in low:
        return f"未检测到可用的 GPU，已退回 CPU 模式（原文：{raw[:200]}）"

    # 模型/参考音文件缺失
    if "model file missing" in low or "ref_audio" in low or "ref.wav" in low:
        return f"模型文件或参考音频丢失，请重新导入该 voice 模型（原文：{raw[:200]}）"
    if "filenotfounderror" in low or "no such file" in low:
        return f"运行时找不到文件（运行时包损坏或模型不完整？请尝试重装）：{raw[:200]}"

    # 运行时初始化类（统一对 low 做匹配；中文字符 .lower() 不变）
    if "torch 加载失败" in low or "无法 import gpt_sovits" in low or "tts_config 构造失败" in low:
        return f"本地 TTS 引擎初始化失败，运行时包可能损坏，请到「模型管理」重装运行时（原文：{raw[:200]}）"
    if "tts pipeline 构造失败" in low:
        return f"本地 TTS 模型加载失败，可能基础模型未下载完整或与代码不兼容（原文：{raw[:200]}）"

    # voice 权重加载类
    if "加载模型权重失败" in low or "init_t2s_weights" in low or "init_vits_weights" in low:
        return f"voice 模型权重加载失败，模型文件可能损坏或与 GPT-SoVITS v2 配置不兼容（原文：{raw[:200]}）"

    # 推理类
    if "推理返回空音频" in low or "推理返回空" in low:
        return f"合成返回空音频，文本/参考音频可能有问题（请检查 ref_text 是否匹配 ref_audio 内容；原文：{raw[:200]}）"
    if "推理失败" in low or "gpt-sovits 推理" in low:
        return f"GPT-SoVITS 推理出错：{raw[:300]}"

    # 通用
    return raw[:500]

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
        target: str = "cpu",
        synthesize_timeout_sec: float = _DEFAULT_SYNTHESIZE_TIMEOUT_SEC,
        log_dir: Optional[Path] = None,
        sample_steps: int = 8,
    ) -> None:
        self._runtime_dir = Path(runtime_dir)
        self._models = model_store
        # 本地引擎始终返回原始 WAV；不再 transcode（合成路径与导出解耦的设计）
        # 转码统一在 synthesis/exporter.py 导出 zip 阶段做
        self._format = "wav"
        self._target = target
        self._synthesize_timeout = max(30.0, float(synthesize_timeout_sec))
        self._sample_steps = max(2, min(32, int(sample_steps)))
        self._log_dir = Path(log_dir) if log_dir else None
        self._log_file: Optional[Path] = None
        self._log_handle = None  # 文件句柄，传给 Popen 当 stdout/stderr
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._spawn_lock = asyncio.Lock()
        logger.info(
            "local_tts engine: format=wav (always) target=%s timeout=%.0fs"
            " sample_steps=%d runtime=%s log_dir=%s",
            self._target, self._synthesize_timeout,
            self._sample_steps, self._runtime_dir, self._log_dir,
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
        if proc is not None:
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
        self._close_log_file()

    def _tail_log_file(self, n_lines: int = 30) -> str:
        """读 runtime 日志文件末尾 n_lines 行；用于错误诊断。"""
        if self._log_file is None or not self._log_file.exists():
            return "(无日志文件)"
        try:
            # 简单读全文取尾部；runtime 日志通常 < 10MB，性能 OK
            text = self._log_file.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            return "\n".join(lines[-n_lines:])
        except Exception as e:
            return f"(读日志失败: {e})"

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
                f"本地 TTS 运行时未安装（target={self._target}）。请到「软件设置」安装"
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
                    "sample_steps": self._sample_steps,
                },
                self._synthesize_timeout,
            )
        except HTTPError as e:
            detail = ""
            with contextlib.suppress(Exception):
                raw_body = e.read().decode("utf-8", errors="ignore")
                # 尝试解析 FastAPI 标准错误 JSON {"detail": "..."}
                with contextlib.suppress(Exception):
                    parsed = json.loads(raw_body)
                    if isinstance(parsed, dict) and "detail" in parsed:
                        detail = str(parsed["detail"])
                detail = detail or raw_body
            friendly = _translate_runtime_error(detail) if detail else f"HTTP {e.code} {e.reason}"
            log_hint = f"\nruntime 详细日志：{self._log_file}" if self._log_file else ""
            raise TTSError(f"本地 TTS 合成失败：{friendly}{log_hint}") from e
        except URLError as e:
            # 子进程可能死了，capture 日志尾部再 clear
            tail = self._tail_log_file(50)
            log_hint = f"\n完整日志：{self._log_file}" if self._log_file else ""
            self.close()
            reason = getattr(e, "reason", e)
            hint = "运行时进程可能崩溃或未就绪"
            if "timed out" in str(reason).lower() or "timeout" in str(reason).lower():
                hint = (
                    f"合成超时（{self._synthesize_timeout:.0f}s）；"
                    f"首次合成需加载基础模型可能较慢，可在「软件设置」加大超时"
                )
            raise TTSError(
                f"本地 TTS 运行时通信失败：{hint}（{reason}）"
                f"{log_hint}\nruntime 日志末尾：\n{tail}"
            ) from e
        # 7. 直接返回原始 WAV，不转码（导出 zip 时 synthesis.exporter 才走 ffmpeg）
        return wav

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
            # -X utf8: 强制 Python stdout/stderr 用 UTF-8，避免 Windows 默认 cp936 把中文 log
            #   写成乱码（serve.py 抛中文 RuntimeError 时尤其重要）
            cmd = [
                str(python_exe),
                "-X", "utf8",
                str(serve_py),
                "--port", str(port),
                "--model-root", str(model_root),
                "--backend", "auto",
            ]
            # 打开 runtime 日志文件（合并 stdout + stderr，写文件而不是 PIPE —— 否则 buffer
            # 满了子进程会阻塞，是经典的 Popen 坑：之前 502/500 实际就是这个炸的）
            self._open_log_file(port)
            logger.info(
                "spawn local-tts runtime: %s  (log → %s)",
                " ".join(cmd), self._log_file or "<stderr>",
            )

            # 给子进程注入 UTF-8 编码环境，配合 serve.py:_force_utf8_io() 防止中文乱码：
            # - PYTHONIOENCODING=utf-8: Python sys.stdout/stderr 与 -X utf8 一致走 UTF-8
            # - PYTHONUTF8=1: 等价开关，对部分老依赖兜底（PEP 540）
            # 若不设，onnxruntime/torch 等 C 扩展输出会按 Windows cp936 写到同一文件，
            # 与 Python UTF-8 输出混杂导致后期 reader 解码乱码
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"

            popen_kwargs: dict = {
                "cwd": str(self._runtime_dir),
                "stdout": self._log_handle if self._log_handle else subprocess.DEVNULL,
                "stderr": subprocess.STDOUT,  # 合并到 stdout 同一个文件，traceback 不丢
                "env": child_env,
            }
            # Windows: 不弹黑色控制台（pywebview 父进程是 GUI，spawn 控制台子进程默认会弹窗）
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
            except OSError as e:
                self._close_log_file()
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
                self._close_log_file()
                raise

            self._proc = proc
            self._port = port
            return port

    def _open_log_file(self, port: int) -> None:
        """打开 runtime 日志文件；失败则回落 DEVNULL（不阻塞启动）。"""
        self._close_log_file()
        if self._log_dir is None:
            return
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._log_file = self._log_dir / "local-tts-runtime.log"
            # append binary 模式：子进程会按 OS 默认编码写（Windows GBK / Linux UTF-8）
            self._log_handle = self._log_file.open("ab")
            # 写一条分隔头，方便用户找最新一次运行的日志段
            import datetime
            header = (
                f"\n==== local-tts runtime start: "
                f"{datetime.datetime.now().isoformat(timespec='seconds')} port={port} ====\n"
            ).encode("utf-8")
            self._log_handle.write(header)
            self._log_handle.flush()
        except Exception as e:
            logger.warning("打开 runtime 日志文件失败，子进程 stdout/stderr 将丢弃: %s", e)
            self._log_handle = None
            self._log_file = None

    def _close_log_file(self) -> None:
        if self._log_handle is not None:
            with contextlib.suppress(Exception):
                self._log_handle.close()
        self._log_handle = None

    async def _wait_health(self, port: int, proc: subprocess.Popen) -> None:
        url = f"http://127.0.0.1:{port}/health"
        loop = asyncio.get_event_loop()
        deadline = loop.time() + _HEALTH_TIMEOUT_SEC
        while loop.time() < deadline:
            if proc.poll() is not None:
                # 进程提前死亡 —— stderr 已经写到日志文件，捞最后 30 行给用户看
                tail = self._tail_log_file(30)
                log_hint = f"完整日志：{self._log_file}" if self._log_file else "（无日志）"
                raise TTSError(
                    f"本地 TTS 运行时启动失败（退出码 {proc.returncode}）。{log_hint}\n"
                    f"日志末尾：\n{tail}"
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
