#!/usr/bin/env bash
set -euo pipefail

PKG_NAME="novashell"
VERSION="1.4.0"
ARCH="all"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${SRC_DIR}/build_deb"
PKG_DIR="${BUILD_ROOT}/${PKG_NAME}_${VERSION}_${ARCH}"
OUT_DEB="${SRC_DIR}/${PKG_NAME}_${VERSION}_${ARCH}.deb"

rm -rf "${BUILD_ROOT}"
mkdir -p "${PKG_DIR}/DEBIAN"
mkdir -p "${PKG_DIR}/opt/novashell/default_plugins"
mkdir -p "${PKG_DIR}/usr/local/bin"
mkdir -p "${PKG_DIR}/usr/share/man/man1"

# 1) Copy Python package (excluding __pycache__)
cp -r "${SRC_DIR}/novashell" "${PKG_DIR}/opt/novashell/"
find "${PKG_DIR}/opt/novashell" -type d -name "__pycache__" -exec rm -rf {} +

# 2) Copy default plugins
if [ -d "${HOME}/.novashell/plugins" ]; then
    cp "${HOME}/.novashell/plugins/"*.py "${PKG_DIR}/opt/novashell/default_plugins/" 2>/dev/null || true
fi

# 3) Executable wrapper (/usr/local/bin/nova)
cat << 'WRAPPER' > "${PKG_DIR}/usr/local/bin/nova"
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
chmod 0755 "${PKG_DIR}/usr/local/bin/nova"

# 4) Manual page (man nova)
cat << 'MANPAGE' | gzip -9n > "${PKG_DIR}/usr/share/man/man1/nova.1.gz"
.TH NOVA 1 "November 2026" "NovaShell 1.4.0" "User Commands"
.SH NAME
nova \- Enterprise-ready AI Systems Operator for Linux
.SH SYNOPSIS
.B nova
[\fIQUERY\fR]
.br
.B nova
\fB\-\-plan\fR \fIQUERY\fR
.br
.B nova
{\fBdoctor\fR|\fBsetup\fR|\fBplugins\fR|\fBcheck\fR|\fBaudit\fR|\fBhistory\fR|\fBundo\fR|\fBreset\fR}
.SH DESCRIPTION
NovaShell is an autonomous Linux systems engineering agent with a Zero-Trust Security Guard, automatic secret redaction, live diff previews, Time-Machine rollback snapshots, and dynamic plugins.
.SH COMMANDS
.TP
.B nova doctor
Run a system readiness and security health check.
.TP
.B nova setup
Configure Gemini API key (0600 permissions), model, and strict mode.
.TP
.B nova \-\-plan QUERY
Inspect the system in read-only mode and generate an execution plan.
.TP
.B nova check COMMAND
Classify a shell command (SAFE, CONFIRM, or BLOCKED) without executing it.
.TP
.B nova undo [ID]
Restore a file or reverse a system command from Time-Machine snapshots.
.TP
.B nova audit [\-\-export csv|json]
View or export the SQLite compliance audit trail.
.TP
.B nova reset
Clear persistent session chat history.
MANPAGE

# 5) DEBIAN/control
cat << CONTROL > "${PKG_DIR}/DEBIAN/control"
Package: ${PKG_NAME}
Version: ${VERSION}
Section: admin
Priority: optional
Architecture: ${ARCH}
Depends: python3 (>= 3.10), python3-venv, python3-pip, sudo
Maintainer: Hany Mamdouh <hanymmdouh55@gmail.com>
Description: NovaShell - Enterprise AI Systems Operator for Linux
 Autonomous Ubuntu & RHEL systems engineering CLI featuring a Zero-Trust
 Security Guard, secret redaction, Time-Machine rollback, and dynamic plugins.
CONTROL

# 6) DEBIAN/postinst (sets up isolated venv on target machine)
cat << 'POSTINST' > "${PKG_DIR}/DEBIAN/postinst"
#!/usr/bin/env bash
set -e
if [ "$1" = "configure" ]; then
    echo "Setting up NovaShell Python environment in /opt/novashell/.venv..."
    python3 -m venv /opt/novashell/.venv
    /opt/novashell/.venv/bin/pip install --upgrade pip -q
    /opt/novashell/.venv/bin/pip install google-genai rich -q
    echo "NovaShell installed successfully. Run 'nova setup' to configure your API key."
fi
exit 0
POSTINST
chmod 0755 "${PKG_DIR}/DEBIAN/postinst"

# 7) DEBIAN/postrm (cleans up venv on remove/purge)
cat << 'POSTRM' > "${PKG_DIR}/DEBIAN/postrm"
#!/usr/bin/env bash
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    rm -rf /opt/novashell/.venv
fi
exit 0
POSTRM
chmod 0755 "${PKG_DIR}/DEBIAN/postrm"

# 8) Build the .deb package
dpkg-deb --build --root-owner-group "${PKG_DIR}" "${OUT_DEB}"
rm -rf "${BUILD_ROOT}"
echo "Built package: ${OUT_DEB}"
