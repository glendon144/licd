#!/usr/bin/env bash
set -euo pipefail

# deploy-licd-remote.sh
#
# Conservative remote deployment script for licd.
#
# Usage:
#   ./scripts/deploy-licd-remote.sh blues.clonesvr.com
#   ./scripts/deploy-licd-remote.sh jazz.clonesvr.com
#   ./scripts/deploy-licd-remote.sh macmini.local
#
# Run this from the licd source directory on your workstation.
#
# What it does:
#   - creates a deployment archive from the local licd source tree
#   - copies the archive to the target host
#   - installs licd under /opt/licd
#   - on Linux: creates/uses a dedicated system user, /etc/default/licd,
#     and a systemd service
#   - on macOS: runs licd as the SSH login user via a LaunchDaemon and an
#     /opt/licd/licd.env file
#   - creates a Python virtualenv under /opt/licd/.venv
#   - binds licd to 127.0.0.1:8777 by default
#   - smoke-tests /health and /diagnostics on the target
#
# Security posture:
#   licd binds to localhost by default. Reach it via SSH tunnel unless and until
#   a higher-level policy says otherwise.

APP_NAME="licd"
SERVICE_NAME="licd"
SERVICE_USER="licd"
INSTALL_DIR="/opt/licd"
PORT="${PORT:-8777}"
HOST_BIND="${HOST_BIND:-127.0.0.1}"
MAC_LABEL="${MAC_LABEL:-ai.clones.licd}"
TARGET="${1:-}"

if [ -z "$TARGET" ]; then
    echo "Usage: $0 <target-host>"
    echo
    echo "Examples:"
    echo "  $0 blues.clonesvr.com"
    echo "  $0 jazz.clonesvr.com"
    echo "  $0 macmini.local"
    exit 2
fi

if [ ! -f "licd.py" ] && [ ! -f "README.md" ]; then
    echo "WARNING: This does not look like the licd source directory."
    echo "Current directory: $(pwd)"
    echo
    echo "Continue anyway? Type YES to continue:"
    read -r answer
    if [ "$answer" != "YES" ]; then
        echo "Aborted."
        exit 1
    fi
fi

LOCAL_SOURCE="$(pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE_BASENAME="licd-deploy-${STAMP}.tgz"
LOCAL_ARCHIVE="/tmp/${ARCHIVE_BASENAME}"
REMOTE_ARCHIVE="/tmp/${ARCHIVE_BASENAME}"
REMOTE_INSTALLER="/tmp/install-licd-${STAMP}.sh"

echo "==> Deploying ${APP_NAME} to ${TARGET}"
echo "==> Local source: ${LOCAL_SOURCE}"
echo "==> Remote archive: ${REMOTE_ARCHIVE}"
echo "==> Install dir: ${INSTALL_DIR}"
echo "==> Bind address: ${HOST_BIND}:${PORT}"
echo

cleanup() {
    rm -f "$LOCAL_ARCHIVE" /tmp/install-licd-remote.sh
}
trap cleanup EXIT

echo "==> Creating deployment archive"
tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='venv' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='storage' \
  --exclude='*.db' \
  --exclude='*.sqlite' \
  --exclude='*.sqlite3' \
  --exclude='run' \
  -czf "$LOCAL_ARCHIVE" .

ls -lh "$LOCAL_ARCHIVE"
echo

echo "==> Copying archive to target"
scp "$LOCAL_ARCHIVE" "${TARGET}:${REMOTE_ARCHIVE}"
echo

cat > /tmp/install-licd-remote.sh <<'REMOTE_SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-licd}"
SERVICE_NAME="${SERVICE_NAME:-licd}"
SERVICE_USER="${SERVICE_USER:-licd}"
INSTALL_DIR="${INSTALL_DIR:-/opt/licd}"
REMOTE_ARCHIVE="${REMOTE_ARCHIVE:?REMOTE_ARCHIVE is required}"
PORT="${PORT:-8777}"
HOST_BIND="${HOST_BIND:-127.0.0.1}"
VENV_DIR="${VENV_DIR:-$INSTALL_DIR/.venv}"
MAC_LABEL="${MAC_LABEL:-ai.clones.licd}"
TMP_DIR="$(mktemp -d)"
FETCH_DIR="$TMP_DIR/licd"
REMOTE_OS="$(uname -s)"
DEPLOY_USER="${DEPLOY_USER:-$(id -un)}"
DEPLOY_GROUP="$(id -gn "$DEPLOY_USER")"
RUN_SCRIPT="$INSTALL_DIR/run-licd.sh"
LINUX_ENV_FILE="/etc/default/$SERVICE_NAME"
MAC_ENV_FILE="$INSTALL_DIR/licd.env"
LINUX_SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
MAC_PLIST_FILE="/Library/LaunchDaemons/$MAC_LABEL.plist"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

write_env_file() {
    local env_file="$1"
    sudo tee "$env_file" >/dev/null <<EOF
# Environment for licd
# Managed by deploy-licd-remote.sh

LICD_BIND_HOST=$HOST_BIND
LICD_BIND_PORT=$PORT
LICD_DB=$INSTALL_DIR/storage/licd.db
LICD_HOSTS_FILE=/etc/hosts
LICD_HOST_ALLOWLIST=
LICD_TIMEOUT=2.0
LICD_TORTOISED_PORT=8770
LICD_CLONES_BRAIN_PORT=8765
LICD_DREAM_SERVER_PORT=8181
LICD_ECHO_VAULT_PORT=
LICD_REQUIRE_TOKEN=0
LICD_SHARED_TOKEN=
EOF
    sudo chmod 644 "$env_file"
}

write_run_script() {
    sudo tee "$RUN_SCRIPT" >/dev/null <<EOF
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$INSTALL_DIR"
SERVICE_NAME="$SERVICE_NAME"
VENV_DIR="\${VENV_DIR:-\$APP_DIR/.venv}"
ENV_FILE_MAC="\$APP_DIR/licd.env"
ENV_FILE_LINUX="/etc/default/$SERVICE_NAME"

if [ -f "\$ENV_FILE_MAC" ]; then
    . "\$ENV_FILE_MAC"
elif [ -f "\$ENV_FILE_LINUX" ]; then
    . "\$ENV_FILE_LINUX"
fi

cd "\$APP_DIR"
mkdir -p "\$APP_DIR/storage"

HOST="\${LICD_BIND_HOST:-127.0.0.1}"
PORT="\${LICD_BIND_PORT:-8777}"
DB="\${LICD_DB:-\$APP_DIR/storage/licd.db}"

exec "\$VENV_DIR/bin/python" "\$APP_DIR/licd.py" --host "\$HOST" --port "\$PORT" --db "\$DB"
EOF
    sudo chmod +x "$RUN_SCRIPT"
}

install_linux_service() {
    command -v systemctl >/dev/null 2>&1 || fail "systemctl not found on Linux target"

    echo "==> Creating service user if needed"
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        sudo useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    fi

    echo "==> Stopping existing service if present"
    sudo systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true

    echo "==> Installing application files"
    sudo mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/storage"
    sudo rsync -a --delete \
        --exclude='.venv' \
        --exclude='storage' \
        --exclude='log' \
        "$FETCH_DIR"/ "$INSTALL_DIR"/
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

    echo "==> Creating Python virtual environment"
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
    fi

    echo "==> Creating environment file"
    write_env_file "$LINUX_ENV_FILE"

    echo "==> Creating runtime launcher"
    write_run_script
    sudo chown "$SERVICE_USER:$SERVICE_USER" "$RUN_SCRIPT"

    echo "==> Creating systemd service"
    sudo tee "$LINUX_SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=licd local inference controller daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$LINUX_ENV_FILE
ExecStart=$RUN_SCRIPT
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    echo "==> Enabling and starting service"
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl restart "$SERVICE_NAME"

    echo "==> Service status"
    sudo systemctl --no-pager --full status "$SERVICE_NAME" || true
}

install_macos_service() {
    require_cmd launchctl

    echo "==> Stopping existing launchd service if present"
    sudo launchctl bootout "system/$MAC_LABEL" >/dev/null 2>&1 || \
        sudo launchctl bootout system "$MAC_PLIST_FILE" >/dev/null 2>&1 || true

    echo "==> Installing application files"
    sudo mkdir -p "$INSTALL_DIR" "$INSTALL_DIR/storage" "$INSTALL_DIR/log"
    sudo rsync -a --delete \
        --exclude='.venv' \
        --exclude='storage' \
        --exclude='log' \
        "$FETCH_DIR"/ "$INSTALL_DIR"/
    sudo chown -R "$DEPLOY_USER:$DEPLOY_GROUP" "$INSTALL_DIR"

    echo "==> Creating Python virtual environment"
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        sudo -u "$DEPLOY_USER" python3 -m venv "$VENV_DIR"
    fi

    echo "==> Creating environment file"
    write_env_file "$MAC_ENV_FILE"
    sudo chown root:wheel "$MAC_ENV_FILE"

    echo "==> Creating runtime launcher"
    write_run_script
    sudo chown "$DEPLOY_USER:$DEPLOY_GROUP" "$RUN_SCRIPT"

    echo "==> Creating LaunchDaemon plist"
    sudo tee "$MAC_PLIST_FILE" >/dev/null <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$MAC_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$RUN_SCRIPT</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>UserName</key>
    <string>$DEPLOY_USER</string>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/log/licd.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/log/licd.stderr.log</string>
</dict>
</plist>
EOF
    sudo chmod 644 "$MAC_PLIST_FILE"
    sudo chown root:wheel "$MAC_PLIST_FILE"

    echo "==> Enabling and starting LaunchDaemon"
    sudo launchctl bootstrap system "$MAC_PLIST_FILE"
    sudo launchctl enable "system/$MAC_LABEL" >/dev/null 2>&1 || true
    sudo launchctl kickstart -k "system/$MAC_LABEL"

    echo "==> Service status"
    sudo launchctl print "system/$MAC_LABEL" || true
}

echo "==> Remote host: $(hostname)"
echo "==> App: $APP_NAME"
echo "==> Install dir: $INSTALL_DIR"
echo "==> Venv dir: $VENV_DIR"
echo "==> Bind address: $HOST_BIND:$PORT"
echo "==> Remote OS: $REMOTE_OS"
echo "==> Deploy user: $DEPLOY_USER"
echo

echo "==> Checking sudo access"
sudo -v

case "$REMOTE_OS" in
    Linux)
        echo "==> Preparing Linux dependencies"
        if command -v apt-get >/dev/null 2>&1; then
            sudo apt-get update
            sudo apt-get install -y python3 python3-venv curl rsync
        else
            echo "WARNING: apt-get not found. Please ensure python3, python3-venv, curl, rsync, and systemd are installed."
        fi
        ;;
    Darwin)
        echo "==> Checking macOS dependencies"
        require_cmd python3
        require_cmd curl
        require_cmd rsync
        python3 -c 'import venv' >/dev/null 2>&1 || fail "python3 venv support is required on macOS"
        ;;
    *)
        fail "Unsupported remote OS: $REMOTE_OS"
        ;;
esac

echo "==> Unpacking deployment archive"
mkdir -p "$FETCH_DIR"
tar -xzf "$REMOTE_ARCHIVE" -C "$FETCH_DIR"

case "$REMOTE_OS" in
    Linux)
        install_linux_service
        ;;
    Darwin)
        install_macos_service
        ;;
esac

echo
echo "==> Testing health endpoints"
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null && curl -fsS "http://127.0.0.1:$PORT/diagnostics" >/dev/null; then
        HEALTH_OK=1
        break
    fi
    echo "Health check attempt $i failed; waiting..."
    sleep 1
done

if [ "$HEALTH_OK" -eq 1 ]; then
    echo
    echo "==> licd deployed successfully."
    echo "Health:"
    curl -fsS "http://127.0.0.1:$PORT/health"
    echo
else
    echo
    echo "WARNING: Service started, but /health or /diagnostics did not respond."
    if [ "$REMOTE_OS" = "Darwin" ]; then
        echo "Check logs with:"
        echo "  sudo launchctl print system/$MAC_LABEL"
        echo "  sudo tail -f $INSTALL_DIR/log/licd.stderr.log"
    else
        echo "Check logs with:"
        echo "  sudo journalctl -u $SERVICE_NAME -f"
    fi
    exit 1
fi

echo
echo "==> Final deployment paths"
echo "Source installed at: $INSTALL_DIR"
echo "Database path:       $INSTALL_DIR/storage/licd.db"
if [ "$REMOTE_OS" = "Darwin" ]; then
    echo "Environment file:    $MAC_ENV_FILE"
    echo "LaunchDaemon plist:  $MAC_PLIST_FILE"
else
    echo "Environment file:    $LINUX_ENV_FILE"
    echo "Systemd service:     $LINUX_SERVICE_FILE"
fi
REMOTE_SCRIPT

chmod +x /tmp/install-licd-remote.sh

echo "==> Copying remote installer"
scp /tmp/install-licd-remote.sh "${TARGET}:${REMOTE_INSTALLER}"
echo

echo "==> Running remote installer"
ssh -t "$TARGET" \
    "APP_NAME='$APP_NAME' SERVICE_NAME='$SERVICE_NAME' SERVICE_USER='$SERVICE_USER' INSTALL_DIR='$INSTALL_DIR' REMOTE_ARCHIVE='$REMOTE_ARCHIVE' PORT='$PORT' HOST_BIND='$HOST_BIND' MAC_LABEL='$MAC_LABEL' bash '$REMOTE_INSTALLER'"

echo
echo "==> Remote deployment complete."
echo
echo "Target:       $TARGET"
echo "Service:      $SERVICE_NAME"
echo "Bind address: $HOST_BIND:$PORT"
echo
echo "Test from target:"
echo "  ssh $TARGET 'curl -s http://127.0.0.1:$PORT/health'"
echo
echo "Optional SSH tunnel from this machine:"
echo "  ssh -N -L 8777:127.0.0.1:$PORT $TARGET"
