#!/bin/bash
set -e

SCRIPT="$0"
while [ -h "$SCRIPT" ]; do
    DIR="$(cd "$(dirname "$SCRIPT")" && pwd)"
    SCRIPT="$(readlink "$SCRIPT")"
    [[ "$SCRIPT" != /* ]] && SCRIPT="$DIR/$SCRIPT"
done
DIR="$(cd "$(dirname "$SCRIPT")" && pwd)"
PORT_FILE="/tmp/claude-proxy-port.txt"
PROXY_PID=""

MCP_CONFIG=$(mktemp /tmp/claude-mcp-XXXXXX.json)

cleanup() {
    local code=$?
    echo ""
    echo "Shutting down proxy..."
    if [ -n "$PROXY_PID" ] && kill -0 "$PROXY_PID" 2>/dev/null; then
        kill "$PROXY_PID" 2>/dev/null
        wait "$PROXY_PID" 2>/dev/null || true
    fi
    rm -f "$PORT_FILE" "$MCP_CONFIG"
    exit $code
}

trap cleanup SIGINT SIGTERM EXIT

rm -f "$PORT_FILE"

# Locate proxy script: next to script (dev) or in Homebrew libexec (installed)
PROXY_SCRIPT="$DIR/claude-cline-proxy.py"
BREW_PREFIX=""
if [ ! -f "$PROXY_SCRIPT" ]; then
    PROXY_SCRIPT="$DIR/../libexec/claude-cline-proxy.py"
    BREW_PREFIX="$DIR/.."
fi

# Use Homebrew venv python if available, otherwise system python3
PYTHON="python3"
if [ -n "$BREW_PREFIX" ] && [ -f "$BREW_PREFIX/libexec/venv/bin/python3" ]; then
    PYTHON="$BREW_PREFIX/libexec/venv/bin/python3"
fi

echo "Starting Cline proxy..."
if [ -n "$CLAUDE_PROXY_LOG" ]; then
    LOG_FILE="/tmp/claude-proxy.log"
    $PYTHON "$PROXY_SCRIPT" > "$LOG_FILE" 2>&1 &
else
    $PYTHON "$PROXY_SCRIPT" > /dev/null 2>&1 &
fi
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
    kill "$PROXY_PID" 2>/dev/null || true
    exit 1
fi

if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "Proxy process died" >&2
    exit 1
fi

echo "Proxy running on port $PORT (pid $PROXY_PID)"

# read model from Cline config
CLINE_MODEL=$($PYTHON -c "
import json, os
try:
    p = json.load(open(os.path.expanduser('$HOME/.cline/data/settings/providers.json')))
    active = p.get('providers', {}).get(p.get('lastUsedProvider', 'cline'), {})
    print(active.get('settings', {}).get('model', ''))
except: pass
" 2>/dev/null)

# Resolve MCP config path: next to script (dev) or Homebrew etc (installed)
MCP_SOURCE="$DIR/claude-cline-mcp.json"
if [ ! -f "$MCP_SOURCE" ]; then
    MCP_SOURCE="$DIR/../etc/claude-cline-mcp.json"
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

claude --tools default "$@" --mcp-config "$MCP_CONFIG"
