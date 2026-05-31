# claude-cline-proxy

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) using any model from your [Cline](https://cline.bot) provider configuration via a local proxy that translates Anthropic Messages API ↔ OpenAI Chat Completions API.

## How it works

```
claude CLI → proxy.py → api.cline.bot (DeepSeek, Anthropic, etc.)
     ↑
  reads model/tokens from ~/.cline/data/
```

The proxy:

- Reads your active provider and model from Cline's `providers.json` at each request (no hardcoded models)
- Translates Anthropic streaming API calls (including tool calls, multi-turn, reasoning blocks) to OpenAI format
- Handles Cline OAuth token refresh automatically via `api.cline.bot/api/v1/auth/refresh`
- Picks a random available port in the 8000–9000 range

## Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` in PATH)
- [Python 3](https://www.python.org/) with `aiohttp`
- Cline account with an active provider configured (any model)

## Setup

```bash
# install python dependency
pip install aiohttp

# make the launcher executable
chmod +x run_claude.sh

# (optional) symlink to PATH
ln -s "$PWD/run_claude.sh" /opt/homebrew/bin/run_claude
```

## Usage

```bash
./run_claude.sh <your prompt>
# or
run_claude <your prompt>

# all claude arguments are passed through transparently:
run_claude --print "explain how streams work in Python"
run_claude -p "design a database schema for a blog"   # plan mode
```

Proxy logs are silenced by default. To enable debug logging:

```bash
CLAUDE_PROXY_LOG=1 run_claude <your prompt>
# logs written to /tmp/claude-proxy.log
```

The script:

1. Starts `proxy.py` in the background
2. Reads the active model from `~/.cline/data/settings/providers.json`
3. Optionally merges Tavily MCP from Cline's MCP config
4. Launches `claude` with `ANTHROPIC_BASE_URL` pointed at the proxy
5. Cleans up the proxy on exit

## Configuration

All configuration comes from Cline's files — no secrets or models are hardcoded in this project:

| File | Role |
|------|------|
| `~/.cline/data/settings/providers.json` | Provider, model, auth tokens |
| `~/.cline/data/secrets.json` | OAuth idToken and refreshToken (under `cline:clineAccountId`) |
| `~/.cline/data/settings/cline_mcp_settings.json` | Tavily MCP key (optional, merged if present) |
| `mcp.json` | Local MCP overrides (starts empty, add your custom MCPs here) |

### Token refresh

The proxy automatically refreshes expired tokens by calling `POST api.cline.bot/api/v1/auth/refresh` with the stored refresh token. If refresh fails, you can re-authenticate:

```bash
cline auth
```

## WebSearch Limitation

Claude Code's built-in `WebSearch` tool will **not work** through this proxy — it is an Anthropic-only feature that requires a direct connection to the Anthropic API. If you need internet search capabilities, you have two options:

1. **Tavily via Cline (automatic)** — configure Tavily as an MCP server in your Cline settings. The launcher (`run_claude.sh`) will automatically detect it and merge the configuration.
2. **Any other search MCP** — add your preferred web search tool (e.g. Brave Search, Exa) as an MCP server in `mcp.json`.

## Files

| File | Purpose |
|------|---------|
| `proxy.py` | Local proxy: Anthropic ↔ OpenAI translation, token management |
| `run_claude.sh` | Launcher: starts proxy, reads Cline config, runs claude |
| `mcp.json` | MCP server definitions (user-editable, Tavily is merged from Cline) |
| `AGENTS.md` | Architecture notes, auth flow details |
