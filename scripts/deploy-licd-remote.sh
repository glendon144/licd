#!/usr/bin/env bash
set -euo pipefail

# deploy-licd-remote.sh
#
# Conservative remote deployment script for licd.
#
# Usage:
#   ./scripts/deploy-licd-remote.sh blues.clonesvr.com
#   ./scripts/deploy-licd-remote.sh jazz.clonesvr.com
#
# Run this from the licd source directory on your workstation.
#
# What it does:
#   - creates a deployment archive from the local licd source tree
#   - copies the archive to the target host
#   - installs licd under /opt/licd
#   - creates/uses a dedicated system user: licd
#   - creates a Python virtualenv under /opt/licd/.venv
#   - creates /etc/default/licd
#   - creates a systemd service
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
TARGET="${1:-}"

if [ -z "$TARGET" ]; then
    echo "Usage: $0 <target-host>"
    echo
    echo "Examples:"
    echo "  $0 blues.clonesvr.com"
    echo "  $0 jazz.clonesvr.com"
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

TMP_DIR="$(mktemp -d)"
FETCH_DIR="$TMP_DIR/licd"

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "==> Remote host: $(hostname)"
echo "==> App: $APP_NAME"
echo "==> Install dir: $INSTALL_DIR"
echo "==> Venv dir: $VENV_DIR"
echo "==> Bind address: $HOST_BIND:$PORT"
echo

echo "==> Checking sudo access"
sudo -v

echo "==> Installing system packages"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv curl rsync
else
    echo "WARNING: apt-get not found. Please ensure python3, python3-venv, curl, and rsync are installed."
fi

echo "==> Unpacking deployment archive"
mkdir -p "$FETCH_DIR"
tar -xzf "$REMOTE_ARCHIVE" -C "$FETCH_DIR"

echo "==> Creating service user if needed"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    sudo useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Stopping existing service if present"
if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
fi

echo "==> Installing application files"
sudo mkdir -p "$INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR/storage"
sudo rsync -a --delete \
    --exclude='.venv' \
    --exclude='storage' \
    "$FETCH_DIR"/ "$INSTALL_DIR"/
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

echo "==> Creating Python virtual environment"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
fi

echo "==> Creating environment file"
sudo tee "/etc/default/$SERVICE_NAME" >/dev/null <<EOF
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

echo "==> Creating runtime launcher"
sudo tee "$INSTALL_DIR/run-licd.sh" >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/licd"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"

cd "$APP_DIR"
mkdir -p "$APP_DIR/storage"

HOST="${LICD_BIND_HOST:-127.0.0.1}"
PORT="${LICD_BIND_PORT:-8777}"
DB="${LICD_DB:-$APP_DIR/storage/licd.db}"

exec "$VENV_DIR/bin/python" "$APP_DIR/licd.py" --host "$HOST" --port "$PORT" --db "$DB"
EOF

sudo chmod +x "$INSTALL_DIR/run-licd.sh"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/run-licd.sh"

echo "==> Creating systemd service"
sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null <<EOF
[Unit]
Description=licd local inference controller daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=/etc/default/$SERVICE_NAME
ExecStart=$INSTALL_DIR/run-licd.sh
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
    echo "Check logs with:"
    echo "  sudo journalctl -u $SERVICE_NAME -f"
    exit 1
fi

echo
echo "==> Final deployment paths"
echo "Source installed at: $INSTALL_DIR"
echo "Database path:       $INSTALL_DIR/storage/licd.db"
echo "Environment file:    /etc/default/$SERVICE_NAME"
echo "Systemd service:     /etc/systemd/system/$SERVICE_NAME.service"
REMOTE_SCRIPT

chmod +x /tmp/install-licd-remote.sh

echo "==> Copying remote installer"
scp /tmp/install-licd-remote.sh "${TARGET}:${REMOTE_INSTALLER}"
echo

echo "==> Running remote installer"
ssh -t "$TARGET" \
    "APP_NAME='$APP_NAME' SERVICE_NAME='$SERVICE_NAME' SERVICE_USER='$SERVICE_USER' INSTALL_DIR='$INSTALL_DIR' REMOTE_ARCHIVE='$REMOTE_ARCHIVE' PORT='$PORT' HOST_BIND='$HOST_BIND' bash '$REMOTE_INSTALLER'"

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
