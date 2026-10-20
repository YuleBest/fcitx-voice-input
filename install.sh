#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
INSTALL_DIR="$DATA_HOME/doubao-voice-input"
SERVICE_DIR="$CONFIG_HOME/systemd/user"
CONFIG_DIR="$CONFIG_HOME/doubao-voice-input"

cmake -S "$ROOT" -B "$ROOT/work/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_INSTALL_PREFIX=/usr \
    -DFCITX_INSTALL_USE_FCITX_SYS_PATHS=ON
cmake --build "$ROOT/work/build"
sudo cmake --install "$ROOT/work/build"

install -d "$INSTALL_DIR" "$SERVICE_DIR" "$CONFIG_DIR" "$HOME/.local/bin"
install -m 755 "$ROOT/service.py" "$INSTALL_DIR/service.py"
install -m 755 "$ROOT/configure.py" "$HOME/.local/bin/doubao-voice-input-setup"
install -m 755 "$ROOT/doubao-voice-input.py" "$HOME/.local/bin/doubao-voice-input"
install -m 644 "$ROOT/packaging/doubao-voice-input.service" \
    "$SERVICE_DIR/doubao-voice-input.service"

if [[ ! -e "$CONFIG_DIR/config.toml" ]]; then
    install -m 600 "$ROOT/config.example.toml" "$CONFIG_DIR/config.toml"
fi

fcitx5-remote --check >/dev/null 2>&1 || fcitx5 -d
fcitx5 -r -d

systemctl --user daemon-reload
systemctl --user enable --now doubao-voice-input.service
systemctl --user restart doubao-voice-input.service

printf '\n安装完成。\n'
printf '配置命令：doubao-voice-input-setup\n'
printf '服务状态：systemctl --user status doubao-voice-input.service\n'
