#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/novashell"
BIN_PATH="/usr/local/bin/nova"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing NovaShell to ${INSTALL_DIR}..."
sudo rm -rf "${INSTALL_DIR}"
sudo mkdir -p "${INSTALL_DIR}"
sudo cp -r "${SRC_DIR}/novashell" "${INSTALL_DIR}/"

# Copy default plugins if present
if [ -d "${HOME}/.novashell/plugins" ]; then
    sudo mkdir -p "${INSTALL_DIR}/default_plugins"
    sudo cp -r "${HOME}/.novashell/plugins/"*.py "${INSTALL_DIR}/default_plugins/" 2>/dev/null || true
fi

# Create isolated venv and install dependencies
sudo python3 -m venv "${INSTALL_DIR}/.venv"
sudo "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip -q
sudo "${INSTALL_DIR}/.venv/bin/pip" install google-genai rich -q

# Create global executable wrapper
sudo tee "${BIN_PATH}" > /dev/null << 'WRAPPER'
#!/usr/bin/env bash
set -e
USER_PLUGINS="${HOME}/.novashell/plugins"
if [ ! -d "${USER_PLUGINS}" ] && [ -d "/opt/novashell/default_plugins" ]; then
    mkdir -p "${USER_PLUGINS}"
    cp -n /opt/novashell/default_plugins/*.py "${USER_PLUGINS}/" 2>/dev/null || true
fi
export PYTHONPATH="/opt/novashell:${PYTHONPATH:-}"
exec /opt/novashell/.venv/bin/python -m novashell.cli "$@"
WRAPPER

sudo chmod +x "${BIN_PATH}"
echo "Done! Any user can now run: nova setup"
