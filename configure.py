#!/usr/bin/env python3

from __future__ import annotations

import getpass
import json
import os
import subprocess
import tomllib
from pathlib import Path

CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "doubao-voice-input"
    / "config.toml"
)


def prompt(label: str, current: str = "", secret: bool = False) -> str:
    suffix = " [保持不变]" if current else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{label}{suffix}: ").strip()
    return value or current


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def main() -> int:
    current: dict[str, object] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as file:
            current = tomllib.load(file)

    api_key = prompt("豆包 API Key", str(current.get("api_key", "")), secret=True)
    hotkey = prompt("按住说话快捷键", str(current.get("hotkey", "Super+space")))
    source = prompt(
        "PipeWire 输入源，留空使用默认麦克风", str(current.get("source", ""))
    )

    if not api_key:
        print("API Key 不能为空。")
        return 1

    resource_id = str(current.get("resource_id", "volc.seedasr.sauc.duration"))
    endpoint = str(
        current.get(
            "endpoint",
            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
        )
    )
    enable_ddc = "true" if bool(current.get("enable_ddc", False)) else "false"
    end_window_size = int(current.get("end_window_size", 800))

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.touch(mode=0o600, exist_ok=True)
    CONFIG_PATH.chmod(0o600)
    CONFIG_PATH.write_text(
        "\n".join(
            [
                f"api_key = {toml_string(api_key)}",
                f"resource_id = {toml_string(resource_id)}",
                f"endpoint = {toml_string(endpoint)}",
                f"hotkey = {toml_string(hotkey)}",
                f"source = {toml_string(source)}",
                f"enable_ddc = {enable_ddc}",
                f"end_window_size = {end_window_size}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    CONFIG_PATH.chmod(0o600)
    print(f"配置已写入 {CONFIG_PATH}")
    result = subprocess.run(
        ["systemctl", "--user", "restart", "doubao-voice-input.service"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        print("语音服务已重启。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
