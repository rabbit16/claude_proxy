#!/usr/bin/env bash
# 重启 claude_code_proxy（默认监听 0.0.0.0:4000）
#
# 优先走 systemd（开机自启 + 崩溃自拉起 + 固定 TEAMO_HTTP_PROXY）。
# 未安装 unit 时回退到手工 nohup 启动，并同样注入代理环境变量。
set -euo pipefail

APP_DIR="${APP_DIR:-/mnt/data/claude_code_proxy}"
PYTHON="${PYTHON:-/root/anaconda3/envs/claude_code/bin/python3.12}"
PORT="${PORT:-4000}"
PID_FILE="${PID_FILE:-$APP_DIR/proxy.pid}"
LOG_FILE="${LOG_FILE:-$APP_DIR/nohup.out}"
WAIT_SECS="${WAIT_SECS:-15}"
SERVICE_NAME="${SERVICE_NAME:-claude-code-proxy.service}"

cd "$APP_DIR"

# 加载本地密钥与数据库配置。项目未引入 python-dotenv，只能由这里
# 或 systemd 的 EnvironmentFile 注入。已存在的环境变量优先，.env 仅补空缺，
# 不会覆盖 systemd / 调用方传入的值。
if [[ -f "$APP_DIR/.env" ]]; then
  while IFS='=' read -r env_key env_val || [[ -n "$env_key" ]]; do
    env_key="${env_key#"${env_key%%[![:space:]]*}"}"
    env_key="${env_key#export }"
    [[ -z "$env_key" || "$env_key" == \#* ]] && continue
    [[ -n "${!env_key:-}" ]] && continue
    export "$env_key=$env_val"
  done < "$APP_DIR/.env"
fi

# 主进程在 import settings.macro 时读取 TEAMO_API_KEY_FILE；不 source 密钥文件，
# 避免密钥出现在 shell 环境或命令行参数。旧上游可设 UPSTREAM_PROVIDER=legacy/yunxin。
export TEAMO_API_KEY_FILE="${TEAMO_API_KEY_FILE:-/etc/claude_code_proxy/teamo_api_key}"
# 远端无法直连 TeamoRouter：固定走本机 FlClash。
export TEAMO_HTTP_PROXY="${TEAMO_HTTP_PROXY:-http://127.0.0.1:7890}"
# 压缩会改写已缓存的历史。启动时固定关闭，避免继承到的环境变量把它打开。
export HEADROOM_ENABLED=0
export HEADROOM_KOMPRESS=0

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

pids_on_port() {
  ss -lntp 2>/dev/null | sed -n "s/.*:${PORT} .*pid=\\([0-9][0-9]*\\).*/\\1/p" | sort -u
}

main_pids() {
  pgrep -f "${PYTHON} main.py" 2>/dev/null || true
}

systemd_managed() {
  command -v systemctl >/dev/null 2>&1 || return 1
  # unit 文件存在即视为可由 systemd 管理（即使当前 inactive）
  systemctl cat "$SERVICE_NAME" >/dev/null 2>&1
}

stop_proxy() {
  local pids
  pids="$(pids_on_port)"
  if [[ -f "$PID_FILE" ]]; then
    local old
    old="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${old:-}" ]]; then
      pids="$(printf '%s\n%s\n' "$pids" "$old" | awk 'NF' | sort -u)"
    fi
  fi
  pids="$(printf '%s\n%s\n' "$pids" "$(main_pids)" | awk 'NF' | sort -u)"

  if [[ -z "${pids:-}" ]]; then
    log "no running proxy found on :${PORT}"
    return 0
  fi

  log "stopping pids: $(echo "$pids" | tr '\n' ' ')"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true

  local i
  for i in $(seq 1 20); do
    sleep 0.3
    if [[ -z "$(pids_on_port)" && -z "$(main_pids)" ]]; then
      break
    fi
  done

  if [[ -n "$(pids_on_port)$(main_pids)" ]]; then
    log "force kill remaining processes"
    # shellcheck disable=SC2086
    kill -9 $(pids_on_port) $(main_pids) 2>/dev/null || true
    sleep 0.5
  fi
  rm -f "$PID_FILE"
}

start_proxy() {
  if [[ ! -x "$PYTHON" ]]; then
    log "error: python not found: $PYTHON"
    exit 1
  fi
  if [[ ! -f "$APP_DIR/main.py" ]]; then
    log "error: main.py not found in $APP_DIR"
    exit 1
  fi

  log "starting: $PYTHON main.py (cwd=$APP_DIR, port=$PORT, proxy=${TEAMO_HTTP_PROXY})"
  # 脱离 SSH/终端；日志追加到 nohup.out
  setsid "$PYTHON" main.py >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  echo "$pid" >"$PID_FILE"
  disown "$pid" 2>/dev/null || true
  log "started pid=$pid"
}

wait_ready() {
  local i
  for i in $(seq 1 "$WAIT_SECS"); do
    if ss -lntp 2>/dev/null | grep -q ":${PORT} "; then
      local pid
      pid="$(pids_on_port | head -n1 || true)"
      log "ready: listening on :${PORT} pid=${pid:-unknown}"
      if command -v curl >/dev/null 2>&1; then
        if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
          log "health check ok"
        else
          log "port is up, health endpoint not ready yet (optional)"
        fi
      fi
      return 0
    fi
    sleep 1
  done
  log "error: port :${PORT} not listening after ${WAIT_SECS}s"
  log "tail $LOG_FILE:"
  tail -n 40 "$LOG_FILE" || true
  exit 1
}

if [[ "${UPSTREAM_PROVIDER:-teamo}" == "teamo" ]]; then
  # 在停掉旧进程前，让程序自身检查环境变量、macro.py 配置项或私有密钥文件。
  if ! "$PYTHON" -c 'from settings.macro import CLAUDE_GPT_KEY; raise SystemExit(0 if CLAUDE_GPT_KEY else 1)'; then
    log "error: TeamoRouter key missing; set TEAMO_API_KEY in settings/macro.py, environment, or $TEAMO_API_KEY_FILE"
    exit 1
  fi
fi

log "restart begin (dir=$APP_DIR)"

if systemd_managed; then
  log "using systemd: $SERVICE_NAME (TEAMO_HTTP_PROXY via unit Environment)"
  # 清掉可能残留的手工 nohup 进程，避免抢端口
  if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    stop_proxy || true
  fi
  systemctl restart "$SERVICE_NAME"
  wait_ready
  # 确认代理环境已注入
  pid="$(pids_on_port | head -n1 || true)"
  if [[ -n "${pid:-}" ]] && tr '\0' '\n' <"/proc/${pid}/environ" 2>/dev/null | grep -q '^TEAMO_HTTP_PROXY='; then
    log "TEAMO_HTTP_PROXY present in pid=$pid"
  else
    log "warn: TEAMO_HTTP_PROXY not visible in process environ"
  fi
  log "restart done (systemd)"
  exit 0
fi

log "systemd unit not found; fallback to nohup (proxy=${TEAMO_HTTP_PROXY})"
stop_proxy
start_proxy
wait_ready
log "restart done"
