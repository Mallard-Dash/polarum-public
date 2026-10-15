#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$HOME/.local/share/polarum"
CONFIG_DIR="$HOME/.config/polarum"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$BIN_DIR"
# Replace the installed package and launcher; the config directory is never touched.
rm -rf "$APP_DIR/polarum" "$APP_DIR/polarum.py"
cp -r polarum "$APP_DIR/polarum"
find "$APP_DIR/polarum" -name __pycache__ -type d -prune -exec rm -rf {} +
cp polarum_launcher.py "$APP_DIR/polarum_launcher.py"
chmod +x "$APP_DIR/polarum_launcher.py"
ln -sf "$APP_DIR/polarum_launcher.py" "$BIN_DIR/polarum"

if [[ ! -f "$CONFIG_DIR/nodes.toml" ]]; then
    cp nodes.toml "$CONFIG_DIR/nodes.toml"
    echo "Created $CONFIG_DIR/nodes.toml"
else
    echo "Keeping existing config: $CONFIG_DIR/nodes.toml"
    echo "NOTE: compare it with ./nodes.toml for new options."
fi

echo "Installed Polarum."
echo "Run: polarum"
