# 开发

## 构建

```bash
cmake -S . -B work/build -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DCMAKE_INSTALL_PREFIX=/usr \
  -DFCITX_INSTALL_USE_FCITX_SYS_PATHS=ON
cmake --build work/build
sudo cmake --install work/build
```

## 安装用户服务

```bash
./install.sh
doubao-voice-input-setup
systemctl --user restart doubao-voice-input.service
```

## 检查

```bash
uvx ruff check service.py configure.py doubao-voice-input.py
uvx ruff format --check service.py configure.py doubao-voice-input.py
shellcheck install.sh
fcitx5-diagnose
systemctl --user status doubao-voice-input.service
```

终端识别原型仍可通过 `doubao-voice-input` 运行，用于单独检查麦克风、网络和 API Key。
