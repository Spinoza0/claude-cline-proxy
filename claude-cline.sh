#!/bin/bash
set -e

VERSION="1.4.2"

SCRIPT="$0"
while [ -h "$SCRIPT" ]; do
    DIR="$(cd "$(dirname "$SCRIPT")" && pwd)"
    SCRIPT="$(readlink "$SCRIPT")"
    [[ "$SCRIPT" != /* ]] && SCRIPT="$DIR/$SCRIPT"
done
DIR="$(cd "$(dirname "$SCRIPT")" && pwd)"
INSTANCE_ID=$$
PORT_FILE="/tmp/claude-proxy-port-$INSTANCE_ID.txt"
PROXY_PID=""

# Parse --model and --provider from args before passing rest to claude
CLINE_OVERRIDE_MODEL=""
CLINE_OVERRIDE_PROVIDER="${CLINE_OVERRIDE_PROVIDER:-}"
PARSED_ARGS=()
skip_next=""
for arg in "$@"; do
    if [ -n "$skip_next" ]; then
        if [ "$skip_next" = "model" ]; then
            CLINE_OVERRIDE_MODEL="$arg"
        elif [ "$skip_next" = "provider" ]; then
            CLINE_OVERRIDE_PROVIDER="$arg"
        fi
        skip_next=""
        continue
    fi
    if [ "$arg" = "--model" ]; then
        skip_next="model"
        continue
    fi
    if [ "$arg" = "--provider" ]; then
        skip_next="provider"
        continue
    fi
    if [ "${arg#--provider=}" != "$arg" ]; then
        CLINE_OVERRIDE_PROVIDER="${arg#--provider=}"
        continue
    fi
    PARSED_ARGS+=("$arg")
done
set -- "${PARSED_ARGS[@]}"
[ -n "$CLINE_OVERRIDE_MODEL" ] && export CLINE_OVERRIDE_MODEL
[ -n "$CLINE_OVERRIDE_PROVIDER" ] && export CLINE_OVERRIDE_PROVIDER

# Check Cline is installed
if [ ! -d "$HOME/.cline" ]; then
    echo "Error: Cline is not installed." >&2
    echo "" >&2
    echo "Cline is required for this tool to work." >&2
    echo "Download it from: https://cline.bot" >&2
    exit 1
fi

# Check python3 is available
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is not installed." >&2
    echo "" >&2
    echo "Python 3 is required to run the proxy." >&2
    echo "Install it from: https://www.python.org/downloads/" >&2
    exit 1
fi

# Check claude CLI is available
if ! command -v claude &>/dev/null; then
    echo "Error: claude CLI is not installed." >&2
    echo "" >&2
    echo "Claude Code CLI is required." >&2
    echo "Install it from: https://docs.anthropic.com/en/docs/claude-code" >&2
    exit 1
fi

# Check Cline tokens are valid before starting proxy
python3 -c "
import json, os, time, base64

def decode_jwt_exp(token):
    try:
        payload_b64 = token.split('.')[1]
        payload_b64 += '=' * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get('exp', 0)
    except Exception:
        return 0

def token_valid(token):
    return bool(token) and time.time() < decode_jwt_exp(token)

def get_active_id(providers):
    """Same priority chain as proxy: env → globalState → lastUsedProvider"""
    env_id = os.environ.get('CLINE_OVERRIDE_PROVIDER')
    if env_id:
        return env_id
    gs_path = os.path.expanduser('$HOME/.cline/data/globalState.json')
    if os.path.exists(gs_path):
        try:
            gs = json.load(open(gs_path))
            mode = gs.get('mode', 'act').lower()
            gs_pid = gs.get(f'{mode}ModeApiProvider', '')
            if gs_pid and gs_pid in providers.get('providers', {}):
                return gs_pid
        except Exception:
            pass
    return providers.get('lastUsedProvider', 'cline')

secrets_file = os.path.expanduser('$HOME/.cline/data/secrets.json')
providers_file = os.path.expanduser('$HOME/.cline/data/settings/providers.json')

try:
    with open(secrets_file) as f:
        secrets = json.load(f)
    acc = secrets.get('cline:clineAccountId', '')
    if acc:
        acc_data = json.loads(acc) if isinstance(acc, str) else acc
        id_token = acc_data.get('idToken', '')
        if id_token and token_valid(id_token):
            exit(0)
except: pass

try:
    with open(providers_file) as f:
        providers = json.load(f)
    active_id = get_active_id(providers)
    active = providers.get('providers', {}).get(active_id, {})
    s = active.get('settings', {})
    if s.get('provider') == 'cline':
        raw = s.get('auth', {}).get('accessToken', '')
        if raw.startswith('workos:'):
            raw_token = raw[7:]
            if token_valid(raw_token):
                exit(0)
except: pass

# No valid tokens found — only fail if using cline provider
try:
    with open(providers_file) as f:
        p = json.load(f)
    active_id = get_active_id(p)
    active = p.get('providers', {}).get(active_id, {})
    if active.get('settings', {}).get('provider') == 'cline':
        exit(1)
except: pass
" 2>/dev/null || {
    echo "Cline session expired." >&2
    echo "To re-authenticate, run: cline auth" >&2
    exit 1
}

# Detect Homebrew Cellar: script is in .../Cellar/<name>/<version>/bin/
BREW_PREFIX=""
if [[ "$DIR" == */Cellar/*/bin ]]; then
    BREW_PREFIX="$(cd "$DIR/.." && pwd)"
fi

# Use venv python when installed via Homebrew, otherwise system python3
PYTHON="python3"
if [ -n "$BREW_PREFIX" ]; then
    if [ -x "$BREW_PREFIX/libexec/venv/bin/python3" ]; then
        PYTHON="$BREW_PREFIX/libexec/venv/bin/python3"
    elif [ -x "$BREW_PREFIX/libexec/bin/python3" ]; then
        PYTHON="$BREW_PREFIX/libexec/bin/python3"
    fi
fi

MCP_CONFIG=$(mktemp /tmp/claude-mcp-$INSTANCE_ID-XXXXXX.json)

cleanup() {
    local code=$?
    echo ""
    echo "Shutting down proxy..."
    if [ -n "$PROXY_PID" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
        kill "$PROXY_PID" 2>/dev/null
        wait "$PROXY_PID" 2>/dev/null || true
    fi
    rm -f "$PORT_FILE" "$MCP_CONFIG" "/tmp/claude-proxy-$$.log"
    exit $code
}

trap cleanup SIGINT SIGTERM EXIT

rm -f "$PORT_FILE"

# Locate proxy script: next to script (dev) or in Homebrew libexec (installed)
PROXY_SCRIPT="$DIR/claude-cline-proxy.py"
if [ -z "$BREW_PREFIX" ] && [ ! -f "$PROXY_SCRIPT" ]; then
    PROXY_SCRIPT="$DIR/../libexec/claude-cline-proxy.py"
    BREW_PREFIX="$DIR/.."
fi
if [ -n "$BREW_PREFIX" ] && [ ! -f "$PROXY_SCRIPT" ]; then
    PROXY_SCRIPT="$BREW_PREFIX/libexec/claude-cline-proxy.py"
fi

# Locate select script alongside proxy script
SELECT_SCRIPT="$(dirname "$PROXY_SCRIPT")/claude-cline-select.py"

# Provider selection menu (skip if --model or --provider was specified)
if [ -z "$CLINE_OVERRIDE_MODEL" ] && [ -z "$CLINE_OVERRIDE_PROVIDER" ] && [ -f "$SELECT_SCRIPT" ] && [ -t 0 ] && [ -c /dev/tty ]; then
    SELECTED=$($PYTHON "$SELECT_SCRIPT" </dev/tty || true)
    if [ -n "$SELECTED" ]; then
        CLINE_OVERRIDE_PROVIDER="$SELECTED"
        export CLINE_OVERRIDE_PROVIDER
    fi
fi

echo "claude-cline-proxy v$VERSION — https://github.com/Spinoza0/claude-cline-proxy"
if command -v brew &>/dev/null; then
    echo "To update: brew upgrade Spinoza0/tap/claude-cline-proxy"
fi
echo ""

echo "Starting Cline proxy (Python: $PYTHON)..."
export CLAUDE_PROXY_PORT_FILE="$PORT_FILE"
LOG_FILE="/tmp/claude-proxy-$$.log"
$PYTHON "$PROXY_SCRIPT" > "$LOG_FILE" 2>&1 &
PROXY_PID=$!

PORT=""
for i in $(seq 1 15); do
    if [ -f "$PORT_FILE" ]; then
        PORT=$(cat "$PORT_FILE")
        break
    fi
    sleep 0.5
done

if [ -z "$PORT" ]; then
    echo "Proxy failed to start (port file not found)" >&2
    echo "Python: $PYTHON" >&2
    echo "Script: $PROXY_SCRIPT" >&2
    echo "Log: $LOG_FILE" >&2
    if [ -s "$LOG_FILE" ]; then
        echo "--- proxy output ---" >&2
        cat "$LOG_FILE" >&2
        echo "---" >&2
    fi
    kill "$PROXY_PID" 2>/dev/null || true
    exit 1
fi

if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "Proxy process died" >&2
    exit 1
fi

echo "Proxy running on port $PORT (pid $PROXY_PID)"

# read model from Cline config (or use --model override)
CLINE_MODEL="${CLINE_OVERRIDE_MODEL:-$($PYTHON -c "
import json, os
try:
    p = json.load(open(os.path.expanduser('$HOME/.cline/data/settings/providers.json')))
    gs_path = os.path.expanduser('$HOME/.cline/data/globalState.json')
    # Priority: env override → globalState → lastUsedProvider
    active_id = os.environ.get('CLINE_OVERRIDE_PROVIDER') or ''
    if not active_id and os.path.exists(gs_path):
        gs = json.load(open(gs_path))
        mode = gs.get('mode', 'act').lower()
        gs_pid = gs.get(f'{mode}ModeApiProvider', '')
        if gs_pid and gs_pid in p.get('providers', {}):
            active_id = gs_pid
    if not active_id:
        active_id = p.get('lastUsedProvider', 'cline')
    active = p.get('providers', {}).get(active_id, {})
    model = active.get('settings', {}).get('model', '')

    # Check globalState model override
    if os.path.exists(gs_path):
        gs = json.load(open(gs_path))
        mode = gs.get('mode', 'act').lower()
        ptype = active.get('settings', {}).get('provider', '')
        suffix_map = {'cline': 'Cline', 'openrouter': 'OpenRouter', 'openai': 'OpenAi', 'openai-compatible': 'OpenAi', 'fireworks': 'Fireworks'}
        gs_key = f'{mode}Mode{suffix_map.get(ptype, ptype.title())}ModelId'
        gs_model = gs.get(gs_key, '')
        if gs_model:
            model = gs_model

    print(model)
except: pass
" 2>/dev/null)}"

# Resolve MCP config path: next to script (dev) or Homebrew etc (installed)
MCP_SOURCE="$DIR/claude-cline-mcp.json"
if [ -z "$BREW_PREFIX" ] && [ ! -f "$MCP_SOURCE" ]; then
    MCP_SOURCE="$DIR/../etc/claude-cline-mcp.json"
fi
if [ -n "$BREW_PREFIX" ] && [ ! -f "$MCP_SOURCE" ]; then
    MCP_SOURCE="$BREW_PREFIX/etc/claude-cline-mcp.json"
fi

$PYTHON -c "
import json, os

mcp_source = os.environ.get('CLAUDE_CLINE_MCP', '$MCP_SOURCE')

try:
    with open(mcp_source) as f:
        mcp = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    mcp = {'mcpServers': {}}

# overlay tavily from Cline config if present
cline_path = os.path.expanduser('$HOME/.cline/data/settings/cline_mcp_settings.json')
try:
    with open(cline_path) as f:
        cline_mcp = json.load(f)
    tavily = cline_mcp.get('mcpServers', {}).get('tavily')
    if tavily:
        mcp.setdefault('mcpServers', {})['tavily'] = {
            'command': tavily['command'],
            'env': tavily.get('env', {}),
        }
except (FileNotFoundError, json.JSONDecodeError):
    pass

with open('$MCP_CONFIG', 'w') as f:
    json.dump(mcp, f, indent=2)
"

echo "Starting Claude Code (model: ${CLINE_MODEL:-from Cline})..."

export ANTHROPIC_BASE_URL="http://127.0.0.1:$PORT"
export ANTHROPIC_API_KEY="sk-ant-dummy"

if [ -n "$CLINE_MODEL" ]; then
    export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLINE_MODEL"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLINE_MODEL"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLINE_MODEL"
fi

# Auto-add --verbose when --output-format stream-json is used
EXTRA=""
skip=0
for arg in "$@"; do
    if [ $skip -eq 1 ]; then
        skip=0
        if [ "$arg" = "stream-json" ]; then
            EXTRA="--verbose"
            break
        fi
        continue
    fi
    if [ "$arg" = "--output-format" ]; then
        skip=1
    elif [ "$arg" = "--output-format=stream-json" ]; then
        EXTRA="--verbose"
        break
    fi
done

claude --tools default "$@" $EXTRA --mcp-config "$MCP_CONFIG"
