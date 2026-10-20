#!/usr/bin/env bash
# 从本机同步 claude_proxy 到远端并重启。
#
# 用法：
#   ./deploy.sh              # 同步 + 重启
#   ./deploy.sh --restart    # 仅远端重启
#   ./deploy.sh --dry-run    # 只看将同步哪些文件
#
# 凭据：优先读同目录 .deploy.env；也可用环境变量覆盖。
# 传输：有 sshpass 时用 rsync；否则自动走 Python/paramiko。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -f "$SCRIPT_DIR/.deploy.env" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/.deploy.env"
fi

export REMOTE_HOST="${REMOTE_HOST:-60.204.149.187}"
export REMOTE_USER="${REMOTE_USER:-root}"
export REMOTE_DIR="${REMOTE_DIR:-/mnt/data/claude_code_proxy}"
export DEPLOY_SSH_PASS="${DEPLOY_SSH_PASS:-${SSH_PASS:-}}"
export PYTHON_REMOTE="${PYTHON_REMOTE:-/root/anaconda3/envs/claude_code/bin/python3.12}"
export LOCAL_DIR="$SCRIPT_DIR"

MODE=deploy
for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE=dry-run ;;
    --restart|--restart-only) MODE=restart ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done
export DEPLOY_MODE="$MODE"

if [[ -z "${DEPLOY_SSH_PASS}" ]]; then
  if ! ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
    "${REMOTE_USER}@${REMOTE_HOST}" 'true' 2>/dev/null; then
    echo "error: 需要 SSH 密码。请创建 .deploy.env（参考 .deploy.env.example）或 export DEPLOY_SSH_PASS=..." >&2
    exit 1
  fi
fi

exec python3 - <<'PY'
import os, sys, time, hashlib, stat
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("error: need paramiko (pip install paramiko)", file=sys.stderr)
    sys.exit(1)

host = os.environ["REMOTE_HOST"]
user = os.environ["REMOTE_USER"]
remote_dir = os.environ["REMOTE_DIR"].rstrip("/")
password = os.environ.get("DEPLOY_SSH_PASS") or None
python_remote = os.environ["PYTHON_REMOTE"]
local_dir = Path(os.environ["LOCAL_DIR"]).resolve()
mode = os.environ.get("DEPLOY_MODE", "deploy")

EXCLUDE_DIR_NAMES = {
    ".git", ".idea", ".pytest_cache", "__pycache__", "logs",
}
EXCLUDE_FILE_NAMES = {
    "nohup.out", "proxy.pid", ".deploy.env",
}
EXCLUDE_PREFIXES = (".backup_", ".deploy_backup_")
EXCLUDE_SUFFIXES = (".pyc",)


def log(msg: str) -> None:
    print(f"[{time.strftime('%F %T')}] {msg}", flush=True)


def ignored(path: Path) -> bool:
    rel = path.relative_to(local_dir)
    parts = rel.parts
    if any(p in EXCLUDE_DIR_NAMES for p in parts[:-1]):
        return True
    if path.is_dir() and path.name in EXCLUDE_DIR_NAMES:
        return True
    if path.name in EXCLUDE_FILE_NAMES:
        return True
    if path.name.startswith(EXCLUDE_PREFIXES):
        return True
    if any(path.name.endswith(suf) for suf in EXCLUDE_SUFFIXES):
        return True
    if ".bak" in path.name or path.name.startswith("main.py.before-"):
        return True
    return False


def connect():
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=host, username=user, timeout=20, allow_agent=True, look_for_keys=True)
    if password:
        kwargs["password"] = password
        kwargs["allow_agent"] = False
        kwargs["look_for_keys"] = False
    client.connect(**kwargs)
    return client


def run(client, cmd: str, check=True) -> str:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=180)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        raise RuntimeError(f"remote exit {code}: {cmd}\n{err or out}")
    return out


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_remote_dir(sftp, path: str) -> None:
    parts = path.strip("/").split("/")
    cur = ""
    for part in parts:
        cur += "/" + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def sync_tree(client) -> tuple[int, int]:
    sftp = client.open_sftp()
    uploaded = skipped = 0
    for root, dirs, files in os.walk(local_dir):
        root_path = Path(root)
        # prune excluded dirs in-place
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIR_NAMES and not d.startswith(EXCLUDE_PREFIXES)]
        rel_root = root_path.relative_to(local_dir)
        remote_root = remote_dir if str(rel_root) == "." else f"{remote_dir}/{rel_root.as_posix()}"
        ensure_remote_dir(sftp, remote_root)
        for name in files:
            local_file = root_path / name
            if ignored(local_file):
                continue
            remote_file = f"{remote_root}/{name}"
            data = local_file.read_bytes()
            local_hash = sha256_bytes(data)
            same = False
            try:
                with sftp.open(remote_file, "rb") as rf:
                    remote_hash = sha256_bytes(rf.read())
                same = remote_hash == local_hash
            except FileNotFoundError:
                same = False
            rel = local_file.relative_to(local_dir).as_posix()
            if same:
                skipped += 1
                if mode == "dry-run":
                    log(f"skip {rel}")
                continue
            log(f"{'would upload' if mode == 'dry-run' else 'upload'} {rel}")
            if mode != "dry-run":
                tmp = remote_file + ".deploy_tmp"
                with sftp.open(tmp, "wb") as wf:
                    wf.write(data)
                try:
                    sftp.chmod(tmp, local_file.stat().st_mode & 0o777)
                except Exception:
                    pass
                try:
                    sftp.remove(remote_file)
                except FileNotFoundError:
                    pass
                sftp.rename(tmp, remote_file)
            uploaded += 1
    sftp.close()
    return uploaded, skipped


client = connect()
try:
    if mode != "restart":
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = f"{remote_dir}/.deploy_backup_{stamp}"
        log(f"backup remote -> {backup}")
        if mode != "dry-run":
            run(client, f"mkdir -p '{backup}' && cp -a '{remote_dir}/main.py' '{remote_dir}/utils' "
                f"'{remote_dir}/settings' '{remote_dir}/restart.sh' '{remote_dir}/gunicorn.conf.py' "
                f"'{backup}/' 2>/dev/null || true", check=False)
        uploaded, skipped = sync_tree(client)
        log(f"sync summary uploaded={uploaded} unchanged={skipped}")
        if mode == "dry-run":
            log("dry-run done (未重启)")
            sys.exit(0)

    log("remote restart")
    out = run(client, f"cd '{remote_dir}' && chmod +x restart.sh deploy.sh 2>/dev/null || true; "
              f"APP_DIR='{remote_dir}' PYTHON='{python_remote}' ./restart.sh")
    print(out, end="" if out.endswith("\n") else "\n")

    log("smoke: health")
    print(run(client, "curl -fsS --max-time 5 http://127.0.0.1:4000/health || true").strip())

    log("smoke: import billing helpers")
    smoke = r'''
cd {remote_dir}
{python} - <<'P'
from utils.image_billing import lookup_image_price
from utils.common_utils import extract_usage, is_gemini_native_path
p = lookup_image_price("gpt-image-2", teamo=True)
assert p and p.get("billing") == "per_image", p
assert is_gemini_native_path("/v1beta/models/gemini-3.1-flash-image:generateContent")
u = extract_usage({{
  "object": "chat.completion",
  "usage": {{"prompt_tokens": 6, "completion_tokens": 87, "input_tokens": 0, "output_tokens": 0}},
}})
assert u.get("input_tokens") == 6 and u.get("output_tokens") == 87, u
print("smoke ok", p["per_image_tiers"])
P
'''.format(remote_dir=remote_dir, python=python_remote)
    print(run(client, smoke).strip())
    log("deploy done")
finally:
    client.close()
PY
