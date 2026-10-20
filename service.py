#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "dbus-next>=0.2.3",
#   "websockets>=15",
# ]
# ///

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import shutil
import signal
import stat
import struct
import sys
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

FCITX_BUS = "org.fcitx.Fcitx5"
FCITX_PATH = "/doubao_voice_input"
FCITX_INTERFACE = "org.fcitx.Fcitx.DoubaoVoiceInput1"

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
RESOURCE_ID = "volc.seedasr.sauc.duration"
PCM_CHUNK_BYTES = 6400
MAX_RECORDING_SECONDS = 120

HDR_CONFIG = bytes([0x11, 0x10, 0x11, 0x00])
HDR_AUDIO = bytes([0x11, 0x20, 0x01, 0x00])
HDR_AUDIO_LAST = bytes([0x11, 0x22, 0x01, 0x00])

CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "doubao-voice-input"
    / "config.toml"
)

LOG = logging.getLogger("doubao-voice-input")


class VoiceInputError(RuntimeError):
    pass


@dataclass(slots=True)
class Config:
    api_key: str = ""
    resource_id: str = RESOURCE_ID
    endpoint: str = ENDPOINT
    hotkey: str = "Super+space"
    source: str = ""
    enable_ddc: bool = False
    end_window_size: int = 800


@dataclass(slots=True)
class AsrUpdate:
    text: str
    is_last_package: bool


def load_config() -> Config:
    values: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        mode = stat.S_IMODE(CONFIG_PATH.stat().st_mode)
        if mode & 0o077:
            LOG.warning("%s should only be readable by the current user", CONFIG_PATH)
        with CONFIG_PATH.open("rb") as file:
            values = tomllib.load(file)

    end_window_size = int(values.get("end_window_size", 800))
    end_window_size = min(max(end_window_size, 300), 5000)
    return Config(
        api_key=os.environ.get(
            "VOLCENGINE_ASR_API_KEY", str(values.get("api_key", ""))
        ).strip(),
        resource_id=os.environ.get(
            "VOLCENGINE_ASR_RESOURCE_ID",
            str(values.get("resource_id", RESOURCE_ID)),
        ).strip(),
        endpoint=os.environ.get(
            "VOLCENGINE_ASR_ENDPOINT", str(values.get("endpoint", ENDPOINT))
        ).strip(),
        hotkey=os.environ.get(
            "DOUBAO_VOICE_HOTKEY", str(values.get("hotkey", "Super+space"))
        ).strip(),
        source=os.environ.get(
            "VOICE_INPUT_SOURCE", str(values.get("source", ""))
        ).strip(),
        enable_ddc=bool(values.get("enable_ddc", False)),
        end_window_size=end_window_size,
    )


async def notify(title: str, body: str, *, urgent: bool = False) -> None:
    executable = shutil.which("notify-send")
    if not executable:
        return
    command = [
        executable,
        "--app-name=豆包语音输入",
        f"--urgency={'critical' if urgent else 'normal'}",
        title,
        body,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
    except OSError:
        LOG.exception("Failed to show notification")


async def copy_to_clipboard(text: str) -> bool:
    executable = shutil.which("wl-copy")
    if not executable:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert process.stdin is not None
        await process.communicate(text.encode("utf-8"))
        return process.returncode == 0
    except OSError:
        LOG.exception("Failed to copy text to the clipboard")
        return False


def build_frame(header: bytes, payload: bytes) -> bytes:
    compressed = gzip.compress(payload, mtime=0)
    return header + struct.pack(">I", len(compressed)) + compressed


def request_config(config: Config) -> bytes:
    payload = {
        "user": {
            "uid": "kde-doubao-voice-input",
            "platform": "Linux",
        },
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_nonstream": True,
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": config.enable_ddc,
            "show_utterances": True,
            "result_type": "full",
            "end_window_size": config.end_window_size,
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def parse_response(frame: bytes) -> AsrUpdate | None:
    if len(frame) < 8:
        raise VoiceInputError("服务返回了不完整的响应帧")

    header_size = (frame[0] & 0x0F) * 4
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    compression = frame[2] & 0x0F
    offset = header_size

    if message_type == 0x0F:
        if len(frame) < offset + 8:
            raise VoiceInputError("服务返回了不完整的错误帧")
        code = struct.unpack(">I", frame[offset : offset + 4])[0]
        size = struct.unpack(">I", frame[offset + 4 : offset + 8])[0]
        body = frame[offset + 8 : offset + 8 + size]
        if compression == 1:
            body = gzip.decompress(body)
        detail = body.decode("utf-8", errors="replace")
        raise VoiceInputError(f"豆包 ASR 错误 {code}: {detail}")

    if message_type != 0x09:
        return None
    if flags & 0x01:
        offset += 4
    if flags & 0x04:
        offset += 4
    if len(frame) < offset + 4:
        raise VoiceInputError("服务响应缺少 payload 长度")

    size = struct.unpack(">I", frame[offset : offset + 4])[0]
    body = frame[offset + 4 : offset + 4 + size]
    if len(body) != size:
        raise VoiceInputError("服务响应 payload 不完整")
    if compression == 1:
        body = gzip.decompress(body)

    data: dict[str, Any] = json.loads(body)
    code = data.get("code")
    if code not in (None, 0, 20000000):
        detail = data.get("message") or data
        raise VoiceInputError(f"豆包 ASR 错误 {code}: {detail}")

    result = data.get("result") or {}
    if isinstance(result, list):
        result = result[0] if result else {}
    return AsrUpdate(
        text=result.get("text") or "",
        is_last_package=bool(flags & 0x02),
    )


def capture_command(source: str) -> list[str]:
    command = [
        "parec",
        "--format=s16le",
        "--rate=16000",
        "--channels=1",
        "--raw",
        "--latency-msec=50",
        "--process-time-msec=20",
        "--client-name=doubao-voice-input",
    ]
    if source:
        command.extend(["--device", source])
    return command


class FcitxBridge:
    def __init__(self, interface: Any) -> None:
        self.interface = interface

    async def set_trigger_key(self, hotkey: str) -> bool:
        try:
            return await self.interface.call_set_trigger_key(hotkey)
        except Exception:
            LOG.exception("Failed to set trigger key")
            return False

    async def set_preedit(self, text: str) -> bool:
        try:
            return await self.interface.call_set_preedit(text)
        except Exception:
            LOG.exception("Failed to update preedit")
            return False

    async def commit_text(self, text: str) -> bool:
        try:
            return await self.interface.call_commit_text(text)
        except Exception:
            LOG.exception("Failed to commit text")
            return False

    async def clear_preedit(self) -> bool:
        try:
            return await self.interface.call_clear_preedit()
        except Exception:
            LOG.exception("Failed to clear preedit")
            return False


class RecognitionSession:
    def __init__(self, config: Config, bridge: FcitxBridge) -> None:
        self.config = config
        self.bridge = bridge
        self.stop_event = asyncio.Event()
        self.cancel_event = asyncio.Event()
        self.final_event = asyncio.Event()
        self.text = ""
        self.errors: list[Exception] = []

    def request_stop(self) -> None:
        self.stop_event.set()

    def request_cancel(self) -> None:
        self.cancel_event.set()
        self.stop_event.set()

    async def _capture_audio(
        self,
        process: asyncio.subprocess.Process,
        audio_queue: asyncio.Queue[bytes | None],
    ) -> None:
        try:
            assert process.stdout is not None
            while data := await process.stdout.read(16384):
                await audio_queue.put(data)
            returncode = await process.wait()
            assert process.stderr is not None
            stderr = (
                (await process.stderr.read()).decode("utf-8", errors="replace").strip()
            )
            if not self.stop_event.is_set() and returncode not in (0, -signal.SIGTERM):
                raise VoiceInputError(
                    f"麦克风采集失败：{stderr or f'parec 退出码 {returncode}'}"
                )
        except Exception as exc:  # noqa: BLE001
            self.errors.append(exc)
            self.stop_event.set()
        finally:
            await audio_queue.put(None)

    async def _send_audio(
        self,
        websocket: Any,
        audio_queue: asyncio.Queue[bytes | None],
    ) -> None:
        buffer = bytearray()
        try:
            while True:
                data = await audio_queue.get()
                if data is None:
                    if buffer:
                        await websocket.send(build_frame(HDR_AUDIO, bytes(buffer)))
                    await websocket.send(build_frame(HDR_AUDIO_LAST, b""))
                    return
                buffer.extend(data)
                while len(buffer) >= PCM_CHUNK_BYTES:
                    chunk = bytes(buffer[:PCM_CHUNK_BYTES])
                    del buffer[:PCM_CHUNK_BYTES]
                    await websocket.send(build_frame(HDR_AUDIO, chunk))
        except Exception as exc:  # noqa: BLE001
            self.errors.append(exc)
            self.stop_event.set()

    async def _receive_updates(self, websocket: Any) -> None:
        try:
            async for message in websocket:
                if not isinstance(message, bytes):
                    continue
                update = parse_response(message)
                if update is None:
                    continue
                if update.text and update.text != self.text:
                    self.text = update.text
                    if not await self.bridge.set_preedit(update.text):
                        self.errors.append(VoiceInputError("目标输入框已失去焦点"))
                        self.request_cancel()
                        return
                if update.is_last_package:
                    self.final_event.set()
                    return
        except Exception as exc:  # noqa: BLE001
            self.errors.append(exc)
        finally:
            self.stop_event.set()

    async def _connect(self) -> Any:
        headers = {
            "X-Api-Key": self.config.api_key,
            "X-Api-Resource-Id": self.config.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Connect-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }
        try:
            websocket = await websockets.connect(
                self.config.endpoint,
                additional_headers=headers,
                compression=None,
                max_size=16 * 1024 * 1024,
                open_timeout=10,
                close_timeout=3,
                ping_interval=20,
            )
        except Exception as exc:
            raise VoiceInputError(
                "无法连接豆包 ASR，请检查服务开通状态、Resource ID 和网络"
            ) from exc

        response = getattr(websocket, "response", None)
        if response is not None:
            log_id = response.headers.get("X-Tt-Logid")
            if log_id:
                LOG.info("X-Tt-Logid: %s", log_id)
        return websocket

    async def run(self) -> None:
        if not self.config.api_key:
            raise VoiceInputError(f"请在 {CONFIG_PATH} 中配置 api_key")
        if shutil.which("parec") is None:
            raise VoiceInputError("缺少 parec，请安装 pulseaudio-utils")

        process = await asyncio.create_subprocess_exec(
            *capture_command(self.config.source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        capture_task = asyncio.create_task(self._capture_audio(process, audio_queue))
        websocket = None
        sender_task = None
        receiver_task = None

        try:
            await self.bridge.set_preedit("正在聆听…")
            connect_task = asyncio.create_task(self._connect())
            stop_wait_task = asyncio.create_task(self.stop_event.wait())
            done, _ = await asyncio.wait(
                {connect_task, stop_wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_wait_task in done and not connect_task.done():
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
                await self.bridge.clear_preedit()
                return
            stop_wait_task.cancel()
            await asyncio.gather(stop_wait_task, return_exceptions=True)
            websocket = await connect_task

            await websocket.send(build_frame(HDR_CONFIG, request_config(self.config)))
            sender_task = asyncio.create_task(self._send_audio(websocket, audio_queue))
            receiver_task = asyncio.create_task(self._receive_updates(websocket))

            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=MAX_RECORDING_SECONDS
                )
            except TimeoutError:
                LOG.info("Recording reached the maximum duration")

            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(capture_task, timeout=3)
            except TimeoutError:
                process.kill()
                await capture_task

            if self.cancel_event.is_set() or self.errors:
                await self.bridge.clear_preedit()
                if self.errors:
                    raise self.errors[0]
                return

            if sender_task is not None:
                await sender_task
            if self.errors:
                await self.bridge.clear_preedit()
                raise self.errors[0]

            if not self.final_event.is_set():
                try:
                    await asyncio.wait_for(self.final_event.wait(), timeout=12)
                except TimeoutError as exc:
                    raise VoiceInputError("等待最终识别结果超时") from exc

            if self.errors:
                await self.bridge.clear_preedit()
                raise self.errors[0]
            if not self.text:
                await self.bridge.clear_preedit()
                return

            if not await self.bridge.commit_text(self.text):
                if await copy_to_clipboard(self.text):
                    await notify("豆包语音输入", "目标输入框失焦，文字已复制到剪贴板")
                else:
                    raise VoiceInputError("无法提交文字，也无法写入剪贴板")
        finally:
            if websocket is not None:
                with suppress(Exception):
                    await websocket.close()
            if sender_task is not None and not sender_task.done():
                sender_task.cancel()
                await asyncio.gather(sender_task, return_exceptions=True)
            if receiver_task is not None and not receiver_task.done():
                receiver_task.cancel()
                await asyncio.gather(receiver_task, return_exceptions=True)
            if process.returncode is None:
                process.terminate()
            if not capture_task.done():
                capture_task.cancel()
            await asyncio.gather(capture_task, return_exceptions=True)


class Coordinator:
    def __init__(self, config: Config, bridge: FcitxBridge) -> None:
        self.config = config
        self.bridge = bridge
        self.session: RecognitionSession | None = None
        self.task: asyncio.Task[None] | None = None
        self.tasks: set[asyncio.Task[Any]] = set()

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def on_key_event(self, pressed: bool) -> None:
        if pressed:
            if self.session is not None or self.task is not None:
                return
            self.session = RecognitionSession(self.config, self.bridge)
            self.task = asyncio.create_task(self._run_session(self.session))
        elif self.session is not None:
            self.session.request_stop()

    def on_cancel(self) -> None:
        if self.session is not None:
            self.session.request_cancel()

    def on_rejected(self, reason: str) -> None:
        self._spawn(notify("豆包语音输入", reason))

    async def _run_session(self, session: RecognitionSession) -> None:
        try:
            await session.run()
        except Exception as exc:
            LOG.exception("Recognition session failed")
            await session.bridge.clear_preedit()
            await notify("豆包语音输入失败", str(exc), urgent=True)
        finally:
            if self.session is session:
                self.session = None
                self.task = None

    async def stop(self) -> None:
        if self.session is not None:
            self.session.request_cancel()
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, timeout=5)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        await self.bridge.clear_preedit()


async def run_service() -> None:
    config = load_config()
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, shutdown.set)
    loop.add_signal_handler(signal.SIGINT, shutdown.set)

    try:
        while not shutdown.is_set():
            bus = None
            coordinator = None
            try:
                bus = await MessageBus(bus_type=BusType.SESSION).connect()
                introspection = await bus.introspect(FCITX_BUS, FCITX_PATH)
                proxy = bus.get_proxy_object(FCITX_BUS, FCITX_PATH, introspection)
                interface = proxy.get_interface(FCITX_INTERFACE)
                bridge = FcitxBridge(interface)
                coordinator = Coordinator(config, bridge)

                interface.on_dictation_key_event(coordinator.on_key_event)
                interface.on_cancel_requested(coordinator.on_cancel)
                interface.on_session_rejected(coordinator.on_rejected)

                if not await bridge.set_trigger_key(config.hotkey):
                    raise VoiceInputError(f"Fcitx 不接受快捷键 {config.hotkey!r}")
                LOG.info(
                    "Ready: hotkey=%s resource=%s",
                    config.hotkey,
                    config.resource_id,
                )

                disconnect_task = asyncio.create_task(bus.wait_for_disconnect())
                shutdown_task = asyncio.create_task(shutdown.wait())
                done, _ = await asyncio.wait(
                    {disconnect_task, shutdown_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if shutdown_task in done and not disconnect_task.done():
                    disconnect_task.cancel()
                    await asyncio.gather(disconnect_task, return_exceptions=True)
                shutdown_task.cancel()
                await asyncio.gather(shutdown_task, return_exceptions=True)
            except Exception as exc:  # noqa: BLE001
                if not shutdown.is_set():
                    LOG.warning("Waiting for Fcitx5: %s", exc)
                    await asyncio.sleep(2)
            finally:
                if coordinator is not None:
                    await coordinator.stop()
                if bus is not None:
                    bus.disconnect()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(run_service())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        LOG.exception("Service crashed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
