#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
DESKTOP_DIR="${REPO_ROOT}/zero_agent/frontends/desktop"
APP_NAME="${ZA_APP_NAME:-ZeroAgent}"
APP_PATH="${ZA_APP_PATH:-/Applications/${APP_NAME}.app}"
BRIDGE_PORT="${BRIDGE_PORT:-14168}"
HEALTH_URL="${ZA_HEALTH_URL:-http://127.0.0.1:${BRIDGE_PORT}/status}"
LOG_DIR="${REPO_ROOT}/temp/restart_logs"
SETTINGS_PATH="${HOME}/.zero_agent_desktop_settings.json"

DRY_RUN=0
STATUS_ONLY=0
STOP_ONLY=0
SKIP_PYTHON_BUILD=0
SKIP_DESKTOP_BUILD=0
SKIP_INSTALL=0
NO_START=0
HEALTH_TIMEOUT="${ZA_HEALTH_TIMEOUT:-90}"
PYTHON_BIN="${PYTHON:-}"
MOUNT_POINT=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Restart ZeroAgent desktop end to end:
  1. stop running ZeroAgent desktop/bridge processes
  2. rebuild the editable Python package with UI extras
  3. build the Tauri desktop app
  4. install the generated app to /Applications/ZeroAgent.app
  5. start the app and verify the bridge health endpoint

Options:
  --status              Show current matching processes and bridge status only
  --stop-only           Stop matching ZeroAgent processes and exit
  --skip-python-build   Skip pip install -e '.[ui]'
  --skip-desktop-build  Skip npm run tauri -- build
  --skip-install        Skip installing the built app bundle
  --no-start            Do not start the app after build/install
  --health-timeout SEC  Seconds to wait for /status readiness (default: ${HEALTH_TIMEOUT})
  --dry-run             Print actions without changing processes or files
  -h, --help            Show this help
EOF
}

log() {
  printf '[restart] %s\n' "$*"
}

die() {
  printf '[restart] ERROR: %s\n' "$*" >&2
  exit 1
}

run() {
  log "+ $*"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    return 0
  fi
  "$@"
}

cleanup() {
  if [[ -n "${MOUNT_POINT}" ]]; then
    hdiutil detach "${MOUNT_POINT}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

resolve_executable() {
  local candidate="$1"
  local resolved=""

  if [[ "${candidate}" == */* ]]; then
    local dir
    local base
    dir="$(cd "$(dirname "${candidate}")" && pwd -P)"
    base="$(basename "${candidate}")"
    resolved="${dir}/${base}"
  else
    resolved="$(command -v "${candidate}" 2>/dev/null || true)"
  fi

  [[ -n "${resolved}" && -x "${resolved}" ]] || return 1
  printf '%s\n' "${resolved}"
}

select_python() {
  local candidate=""

  if [[ -n "${PYTHON_BIN}" ]]; then
    candidate="${PYTHON_BIN}"
  elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    candidate="${REPO_ROOT}/.venv/bin/python"
  else
    candidate="python3"
  fi

  PYTHON_BIN="$(resolve_executable "${candidate}")" || die "Python executable not found: ${candidate}"
}

json_string() {
  "${PYTHON_BIN}" -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

write_desktop_settings() {
  local python_json
  local root_json
  python_json="$(printf '%s' "${PYTHON_BIN}" | json_string)"
  root_json="$(printf '%s' "${REPO_ROOT}" | json_string)"

  log "writing desktop settings: ${SETTINGS_PATH}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    return 0
  fi

  mkdir -p "$(dirname "${SETTINGS_PATH}")"
  {
    printf '{\n'
    printf '  "python_path": %s,\n' "${python_json}"
    printf '  "project_dir": %s\n' "${root_json}"
    printf '}\n'
  } > "${SETTINGS_PATH}"
}

match_pids() {
  ps -axo pid=,command= | awk \
    -v self="$$" \
    -v app_path="${APP_PATH}/Contents/MacOS/zero-agent-desktop" \
    -v root="${REPO_ROOT}" '
      {
        pid = $1
        $1 = ""
        command = substr($0, 2)
        if (pid == self) next
        if (index(command, "scripts/restart.sh") > 0) next
        if (command ~ /(^|\/)awk[[:space:]]/) next
        if (index(command, "awk -v app_path=") > 0 ||
            index(command, "awk -v app_path ") > 0) next
        if (index(command, app_path) > 0 ||
            index(command, root "/zero_agent/frontends/desktop_bridge.py") > 0 ||
            index(command, root "/zero_agent/frontends/launcher.py") > 0 ||
            index(command, "zero_agent.frontends.desktop_bridge") > 0 ||
            index(command, "zero_agent.frontends.launcher") > 0 ||
            index(command, "zero-agent-launcher") > 0) {
          print pid
        }
      }
    '
}

list_matching_processes() {
  local rows
  rows="$(ps -axo pid=,ppid=,command= | awk \
    -v app_path="${APP_PATH}/Contents/MacOS/zero-agent-desktop" \
    -v root="${REPO_ROOT}" '
      {
        pid = $1
        ppid = $2
        $1 = ""
        $2 = ""
        command = substr($0, 3)
        if (index(command, "scripts/restart.sh") > 0) next
        if (command ~ /(^|\/)awk[[:space:]]/) next
        if (index(command, "awk -v app_path=") > 0 ||
            index(command, "awk -v app_path ") > 0) next
        if (index(command, app_path) > 0 ||
            index(command, root "/zero_agent/frontends/desktop_bridge.py") > 0 ||
            index(command, root "/zero_agent/frontends/launcher.py") > 0 ||
            index(command, "zero_agent.frontends.desktop_bridge") > 0 ||
            index(command, "zero_agent.frontends.launcher") > 0 ||
            index(command, "zero-agent-launcher") > 0) {
          print pid " " ppid " " command
        }
      }
    ')"

  if [[ -z "${rows}" ]]; then
    log "no matching ZeroAgent processes"
  else
    log "matching ZeroAgent processes:"
    printf '%s\n' "${rows}"
  fi
}

wait_for_exit() {
  local timeout="$1"
  shift
  local deadline=$((SECONDS + timeout))
  local pid=""
  local alive=0

  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    alive=0
    for pid in "$@"; do
      if kill -0 "${pid}" >/dev/null 2>&1; then
        alive=1
        break
      fi
    done
    [[ "${alive}" -eq 0 ]] && return 0
    sleep 0.2
  done

  return 1
}

stop_processes() {
  local pids
  pids="$(match_pids | tr '\n' ' ' | sed 's/[[:space:]]*$//')"

  if [[ -z "${pids}" ]]; then
    log "no running ZeroAgent desktop/bridge processes to stop"
    return 0
  fi

  log "stopping pids: ${pids}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    return 0
  fi

  kill -TERM ${pids} >/dev/null 2>&1 || true
  if wait_for_exit 8 ${pids}; then
    return 0
  fi

  local remaining
  remaining=""
  local pid
  for pid in ${pids}; do
    if kill -0 "${pid}" >/dev/null 2>&1; then
      remaining="${remaining} ${pid}"
    fi
  done

  if [[ -n "${remaining}" ]]; then
    log "force killing pids:${remaining}"
    kill -KILL ${remaining} >/dev/null 2>&1 || true
  fi
}

ensure_bridge_port_free() {
  [[ "${DRY_RUN}" -eq 1 ]] && { log "dry-run: skipping bridge port-free check"; return 0; }

  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi

  local listeners
  listeners="$(lsof -nP -tiTCP:"${BRIDGE_PORT}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${listeners}" ]]; then
    die "port ${BRIDGE_PORT} is still listening after stop: ${listeners}"
  fi
}

build_python_package() {
  [[ "${SKIP_PYTHON_BUILD}" -eq 1 ]] && { log "skipping Python package build"; return 0; }
  run "${PYTHON_BIN}" -m pip install -e ".[ui]"
}

build_desktop_app() {
  [[ "${SKIP_DESKTOP_BUILD}" -eq 1 ]] && { log "skipping Tauri desktop build"; return 0; }
  command -v npm >/dev/null 2>&1 || die "npm not found"

  if [[ ! -d "${DESKTOP_DIR}/node_modules" ]]; then
    log "desktop node_modules missing; installing from lockfile"
    (cd "${DESKTOP_DIR}" && run npm ci)
  fi

  (cd "${DESKTOP_DIR}" && run npm run tauri -- build)
}

latest_dmg() {
  local dmg_dir="${DESKTOP_DIR}/src-tauri/target/release/bundle/dmg"
  local latest=""
  local file

  for file in "${dmg_dir}/${APP_NAME}"_*.dmg "${dmg_dir}"/*.dmg; do
    [[ -f "${file}" ]] || continue
    if [[ -z "${latest}" || "${file}" -nt "${latest}" ]]; then
      latest="${file}"
    fi
  done

  [[ -n "${latest}" ]] || return 1
  printf '%s\n' "${latest}"
}

attach_dmg() {
  local dmg="$1"
  local output

  log "mounting DMG: ${dmg}"
  output="$(hdiutil attach "${dmg}" -nobrowse -readonly)"
  MOUNT_POINT="$(printf '%s\n' "${output}" | awk -F '\t' '/\/Volumes\// {print $NF; exit}')"

  if [[ -z "${MOUNT_POINT}" ]]; then
    MOUNT_POINT="$(printf '%s\n' "${output}" | sed -n 's#.*\(/Volumes/.*\)$#\1#p' | head -n 1)"
  fi

  [[ -n "${MOUNT_POINT}" && -d "${MOUNT_POINT}" ]] || die "failed to mount ${dmg}"
}

install_app() {
  [[ "${SKIP_INSTALL}" -eq 1 ]] && { log "skipping app install"; return 0; }
  [[ "$(uname -s)" == "Darwin" ]] || die "app installation currently supports macOS only"

  local dmg
  dmg="$(latest_dmg)" || die "built DMG not found under ${DESKTOP_DIR}/src-tauri/target/release/bundle/dmg"

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    log "would install latest DMG: ${dmg} -> ${APP_PATH}"
    return 0
  fi

  attach_dmg "${dmg}"

  local source_app
  source_app="$(find "${MOUNT_POINT}" -maxdepth 2 -type d -name "${APP_NAME}.app" -print -quit)"
  if [[ -z "${source_app}" ]]; then
    source_app="$(find "${MOUNT_POINT}" -maxdepth 2 -type d -name "*.app" -print -quit)"
  fi
  [[ -n "${source_app}" ]] || die "no .app bundle found in ${dmg}"

  log "installing ${source_app} -> ${APP_PATH}"
  rm -rf "${APP_PATH}"
  ditto "${source_app}" "${APP_PATH}"
  xattr -dr com.apple.quarantine "${APP_PATH}" >/dev/null 2>&1 || true
}

start_app() {
  [[ "${NO_START}" -eq 1 ]] && { log "not starting app because --no-start was set"; return 0; }
  [[ -d "${APP_PATH}" ]] || die "app not found: ${APP_PATH}"
  run open -n "${APP_PATH}"
}

wait_for_health() {
  [[ "${NO_START}" -eq 1 ]] && return 0

  log "waiting for bridge health: ${HEALTH_URL}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    return 0
  fi

  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  local body=""
  while [[ "${SECONDS}" -lt "${deadline}" ]]; do
    body="$(curl -fsS "${HEALTH_URL}" 2>/dev/null || true)"
    if [[ "${body}" == *'"ok": true'* || "${body}" == *'"ok":true'* ]]; then
      if [[ "${body}" == *'"ready": true'* || "${body}" == *'"ready":true'* ]]; then
        log "bridge ready: ${body}"
        return 0
      fi
    fi
    sleep 1
  done

  [[ -n "${body}" ]] && log "last health response: ${body}"
  die "bridge did not become ready within ${HEALTH_TIMEOUT}s"
}

print_status() {
  list_matching_processes
  if command -v lsof >/dev/null 2>&1; then
    local listeners
    listeners="$(lsof -nP -iTCP:"${BRIDGE_PORT}" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${listeners}" ]]; then
      log "bridge port listener:"
      printf '%s\n' "${listeners}"
    else
      log "bridge port ${BRIDGE_PORT} is not listening"
    fi
  fi

  local body
  body="$(curl -fsS "${HEALTH_URL}" 2>/dev/null || true)"
  if [[ -n "${body}" ]]; then
    log "health: ${body}"
  else
    log "health endpoint unavailable: ${HEALTH_URL}"
  fi
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --status)
      STATUS_ONLY=1
      shift
      ;;
    --stop-only)
      STOP_ONLY=1
      shift
      ;;
    --skip-python-build)
      SKIP_PYTHON_BUILD=1
      shift
      ;;
    --skip-desktop-build)
      SKIP_DESKTOP_BUILD=1
      shift
      ;;
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --no-start)
      NO_START=1
      shift
      ;;
    --health-timeout)
      [[ "$#" -ge 2 ]] || die "--health-timeout requires a value"
      HEALTH_TIMEOUT="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ -d "${REPO_ROOT}/zero_agent" ]] || die "ZeroAgent package not found under ${REPO_ROOT}"
[[ -d "${DESKTOP_DIR}" ]] || die "desktop project not found: ${DESKTOP_DIR}"

select_python
log "repo root: ${REPO_ROOT}"
log "python: ${PYTHON_BIN}"
log "app path: ${APP_PATH}"

if [[ "${STATUS_ONLY}" -eq 1 ]]; then
  print_status
  exit 0
fi

write_desktop_settings
stop_processes
ensure_bridge_port_free

if [[ "${STOP_ONLY}" -eq 1 ]]; then
  log "stopped; exiting because --stop-only was set"
  exit 0
fi

build_python_package
build_desktop_app
install_app
start_app
wait_for_health
print_status
log "done"
