# fcitx-voice-input

KDE Plasma 6 Wayland 下的按住说话输入工具。按住 `Win+Space` 开始识别，文字会作为输入法预编辑内容实时出现；松键提交最终结果，按 `Esc` 取消。

## 依赖

- KDE Plasma 6 与 Fcitx5
- PipeWire 或 PulseAudio 提供的 `parec`
- `uv`、CMake、Ninja 和 Fcitx5 开发头文件

## 安装

```bash
sudo apt install fcitx5-modules-dev libfcitx5core-dev libfcitx5utils-dev cmake ninja-build
./install.sh
doubao-voice-input-setup
```

配置会保存在 `~/.config/doubao-voice-input/config.toml`，权限固定为 `600`。语音服务由 `doubao-voice-input.service` 用户服务管理，登录后自动启动。

## 使用

在任意支持 Fcitx5 的输入框中：

1. 按住 `Win+Space`。
2. 说话，识别结果会实时显示在光标位置。
3. 松开快捷键，准确结果会提交到输入框。
4. 识别过程中按 `Esc` 会取消本次输入。

快捷键和麦克风输入源可以在配置文件中修改，修改后重启服务：

```bash
systemctl --user restart doubao-voice-input.service
```

## 排查

```bash
systemctl --user status doubao-voice-input.service
journalctl --user -u doubao-voice-input.service -f
fcitx5-diagnose
```

日志中的 `X-Tt-Logid` 可用于向火山引擎排查识别请求。
