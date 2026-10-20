#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "websockets>=15",
#   "wcwidth>=0.2",
# ]
# ///

from __future__ import annotations

import asyncio
import gzip
import json
import os
import shutil
import signal
import struct
import sys
import termios
import tty
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import websockets
from wcwidth import wcwidth

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
PCM_CHUNK_BYTES = 6400

HDR_CONFIG = bytes([0x11, 0x10, 0x11, 0x00])
HDR_AUDIO = bytes([0x11, 0x20, 0x01, 0x00])
HDR_AUDIO_LAST = bytes([0x11, 0x22, 0x01, 0x00])


class UserError(RuntimeError):
    pass


@dataclass(slots=True)
class AsrUpdate:
    text: str
    is_last_package: bool


def build_frame(header: bytes, payload: bytes) -> bytes:
    compressed = gzip.compress(payload, mtime=0)
    return header + struct.pack(">I", len(compressed)) + compressed


def request_config() -> bytes:
    payload = {
        "user": {
            "uid": "kde-terminal-voice-input",
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
            "enable_ddc": False,
            "show_utterances": True,
            "result_type": "full",
            "end_window_size": 800,
        },
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def parse_response(frame: bytes) -> AsrUpdate | None:
    if len(frame) < 8:
        raise UserError("服务返回了不完整的响应帧")

    header_size = (frame[0] & 0x0F) * 4
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    compression = frame[2] & 0x0F
    offset = header_size

    if message_type == 0x0F:
        if len(frame) < offset + 8:
            raise UserError("服务返回了不完整的错误帧")
        code = struct.unpack(">I", frame[offset : offset + 4])[0]
        size = struct.unpack(">I", frame[offset + 4 : offset + 8])[0]
        body = frame[offset + 8 : offset + 8 + size]
        if compression == 1:
            body = gzip.decompress(body)
        detail = body.decode("utf-8", errors="replace")
        raise UserError(f"豆包 ASR 错误 {code}: {detail}")

    if message_type != 0x09:
        return None

    if flags & 0x01:
        offset += 4
    if flags & 0x04:
        offset += 4
    if len(frame) < offset + 4:
        raise UserError("服务响应缺少 payload 长度")

    size = struct.unpack(">I", frame[offset : offset + 4])[0]
    body = frame[offset + 4 : offset + 4 + size]
    if len(body) != size:
        raise UserError("服务响应 payload 不完整")
    if compression == 1:
        body = gzip.decompress(body)

    data: dict[str, Any] = json.loads(body)
    code = data.get("code")
    if code not in (None, 0, 20000000):
        detail = data.get("message") or data
        raise UserError(f"豆包 ASR 错误 {code}: {detail}")

    result = data.get("result") or {}
    if isinstance(result, list):
        result = result[0] if result else {}
    text = result.get("text") or ""
    return AsrUpdate(text=text, is_last_package=bool(flags & 0x02))


def _visible_tail(text: str, width: int) -> str:
    used = 0
    start = len(text)
    while start > 0:
        char_width = max(wcwidth(text[start - 1]), 0)
        if used + char_width > width - 1:
            break
        used += char_width
        start -= 1
    if start == 0:
        return text
    return "…" + text[start:]


class StreamPrinter:
    def __init__(self) -> None:
        self.is_tty = sys.stdout.isatty()
        self.width = max(shutil.get_terminal_size((80, 24)).columns - 2, 20)
        self.text = ""

    def status(self, message: str) -> None:
        if self.is_tty:
            sys.stdout.write("\r\x1b[2K" + message + "\n")
        else:
            print(message, file=sys.stderr)
        sys.stdout.flush()

    def update(self, text: str) -> None:
        if not text or text == self.text:
            return
        self.text = text
        if self.is_tty:
            sys.stdout.write("\r\x1b[2K" + _visible_tail(text, self.width))
            sys.stdout.flush()
        else:
            print(text, flush=True)

    def finish(self, text: str) -> None:
        self.text = text
        if self.is_tty:
            sys.stdout.write("\r\x1b[2K")
        print("最终结果：")
        print(text or "（未识别到内容）")
        sys.stdout.flush()


def wait_for_y() -> None:
    if not sys.stdin.isatty():
        raise UserError("必须在交互式终端中运行")

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    print("按 y 开始识别，Ctrl+C 退出：", end="", flush=True)
    try:
        tty.setcbreak(fd)
        while True:
            char = os.read(fd, 1)
            if not char:
                raise KeyboardInterrupt
            if char in (b"y", b"Y"):
                print()
                return
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def capture_command() -> list[str]:
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
    source = os.environ.get("VOICE_INPUT_SOURCE")
    if source:
        command.extend(["--device", source])
    return command


async def capture_audio(
    process: asyncio.subprocess.Process,
    audio_queue: asyncio.Queue[bytes | None],
    stop_event: asyncio.Event,
    errors: list[Exception],
) -> None:
    try:
        assert process.stdout is not None
        while data := await process.stdout.read(16384):
            await audio_queue.put(data)

        returncode = await process.wait()
        assert process.stderr is not None
        stderr = (await process.stderr.read()).decode("utf-8", errors="replace").strip()
        if not stop_event.is_set() and returncode not in (0, -signal.SIGTERM):
            detail = stderr or f"parec 退出码 {returncode}"
            raise UserError(f"麦克风采集失败：{detail}")
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
        stop_event.set()
    finally:
        await audio_queue.put(None)


async def send_audio(
    websocket: Any,
    audio_queue: asyncio.Queue[bytes | None],
    stop_event: asyncio.Event,
    errors: list[Exception],
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
        errors.append(exc)
        stop_event.set()


async def receive_updates(
    websocket: Any,
    printer: StreamPrinter,
    stop_event: asyncio.Event,
    final_event: asyncio.Event,
    errors: list[Exception],
) -> None:
    try:
        async for message in websocket:
            if not isinstance(message, bytes):
                continue
            update = parse_response(message)
            if update is None:
                continue
            printer.update(update.text)
            if update.is_last_package:
                final_event.set()
                return
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)
    finally:
        stop_event.set()


async def connect_websocket(api_key: str, resource_id: str, endpoint: str) -> Any:
    headers = {
        "X-Api-Key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Connect-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    try:
        websocket = await websockets.connect(
            endpoint,
            additional_headers=headers,
            compression=None,
            max_size=16 * 1024 * 1024,
            open_timeout=10,
            close_timeout=3,
            ping_interval=20,
        )
    except Exception as exc:
        raise UserError(
            f"WebSocket 连接失败：{exc}。请确认豆包流式语音识别 2.0 已开通、"
            "Resource ID 与你的计费方式一致。"
        ) from exc

    response = getattr(websocket, "response", None)
    if response is not None:
        log_id = response.headers.get("X-Tt-Logid")
        if log_id:
            print(f"X-Tt-Logid: {log_id}", file=sys.stderr)
    return websocket


async def run_session(api_key: str, resource_id: str, endpoint: str) -> None:
    if shutil.which("parec") is None:
        raise UserError("缺少 parec，请先安装 pulseaudio-utils")

    stop_event = asyncio.Event()
    final_event = asyncio.Event()
    errors: list[Exception] = []
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    printer = StreamPrinter()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    process = await asyncio.create_subprocess_exec(
        *capture_command(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    capture_task = asyncio.create_task(
        capture_audio(process, audio_queue, stop_event, errors)
    )
    websocket = None
    sender_task = None
    receiver_task = None

    try:
        printer.status("正在连接豆包 ASR...")
        connect_task = asyncio.create_task(
            connect_websocket(api_key, resource_id, endpoint)
        )
        stop_task = asyncio.create_task(stop_event.wait())
        done, _ = await asyncio.wait(
            {connect_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done and not connect_task.done():
            connect_task.cancel()
            await asyncio.gather(connect_task, return_exceptions=True)
            return
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        websocket = await connect_task

        await websocket.send(build_frame(HDR_CONFIG, request_config()))
        sender_task = asyncio.create_task(
            send_audio(websocket, audio_queue, stop_event, errors)
        )
        receiver_task = asyncio.create_task(
            receive_updates(
                websocket,
                printer,
                stop_event,
                final_event,
                errors,
            )
        )
        printer.status("录音中，按 Ctrl+C 停止。")

        await stop_event.wait()

        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(capture_task, timeout=3)
        except TimeoutError:
            process.kill()
            await capture_task

        if sender_task is not None:
            await sender_task

        printer.status("正在生成最终结果...")
        if final_event.is_set():
            pass
        elif receiver_task is not None and receiver_task.done():
            await receiver_task
        else:
            try:
                await asyncio.wait_for(final_event.wait(), timeout=12)
            except TimeoutError as exc:
                errors.append(UserError("等待最终识别结果超时"))
                raise errors[-1] from exc

        if errors:
            raise errors[0]
        printer.finish(printer.text)
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
        loop.remove_signal_handler(signal.SIGINT)


def main() -> int:
    api_key = os.environ.get("VOLCENGINE_ASR_API_KEY", "").strip()
    if not api_key:
        print(
            "缺少环境变量 VOLCENGINE_ASR_API_KEY。\n"
            "运行示例：\n"
            "  export VOLCENGINE_ASR_API_KEY='你的 Key'\n"
            "  doubao-voice-input",
            file=sys.stderr,
        )
        return 2

    resource_id = os.environ.get(
        "VOLCENGINE_ASR_RESOURCE_ID", DEFAULT_RESOURCE_ID
    ).strip()
    endpoint = os.environ.get("VOLCENGINE_ASR_ENDPOINT", ENDPOINT).strip()

    try:
        wait_for_y()
        asyncio.run(run_session(api_key, resource_id, endpoint))
        return 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except UserError as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n运行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
