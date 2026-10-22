#!/usr/bin/env bash
# 安装/更新 systemd unit，路径按当前机器实际情况填充。
#
# 用法：
#   ./install-service.sh                    # 用当前目录、当前 python、端口 4000
#   APP_DIR=/opt/proxy ./install-service.sh # 显式指定
#   APP_PORT=4100 PYTHON=/usr/bin/python3.12 ./install-service.sh
#
# 换机器后执行一次，再 ./restart.sh 即可；之后部署仍走 ./deploy.sh。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
PYTHON="${PYTHON:-$(command -v python3 || true)}"
APP_PORT="${APP_PORT:-4000}"
TEAMO_API_KEY_FILE="${TEAMO_API_KEY_FILE:-/etc/claude_code_proxy/teamo_api_key}"
SERVICE_NAME="${SERVICE_NAME:-claude-code-proxy.service}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"

if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
  echo "error: python not found; set PYTHON=/path/to/python3" >&2
  exit 1
fi
if [[ ! -f "$APP_DIR/main.py" ]]; then
  echo "error: main.py not found in APP_DIR=$APP_DIR" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "error: systemctl not found; use ./restart.sh (nohup fallback) instead" >&2
  exit 1
fi
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# 用 sed 做占位符替换，避免 envsubst 误伤 unit 里的 $ 和 %。
sed -e "s|@APP_DIR@|${APP_DIR}|g" \
    -e "s|@PYTHON@|${PYTHON}|g" \
    -e "s|@APP_PORT@|${APP_PORT}|g" \
    -e "s|@TEAMO_API_KEY_FILE@|${TEAMO_API_KEY_FILE}|g" \
    "$SCRIPT_DIR/claude-code-proxy.service" >"$TMP"

install -m 644 "$TMP" "$UNIT_PATH"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

echo "installed: $UNIT_PATH"
echo "  APP_DIR=$APP_DIR"
echo "  PYTHON=$PYTHON"
echo "  APP_PORT=$APP_PORT"
echo "  TEAMO_API_KEY_FILE=$TEAMO_API_KEY_FILE"
echo "下一步：确认 $APP_DIR/.env 存在（参考 .env.example），然后 ./restart.sh"
