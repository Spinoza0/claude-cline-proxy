# claude-cline-proxy

Run [Claude Code](https://docs.anthropic.com/en/docs/claude-code) using any model from your [Cline](https://cline.bot) provider configuration via a local proxy that translates Anthropic Messages API ↔ OpenAI Chat Completions API.

**macOS only** — this project relies on Cline's desktop configuration and token files, which are only available on macOS.

## How it works

```
claude CLI → claude-cline-proxy.py → api.cline.bot / OpenRouter / Ollama / etc.
     ↑
  reads config from ~/.cline/data/
     ↓
claude-cline-select.py  (interactive provider menu, optional)
```

The proxy:

- Reads your active provider and model from Cline's `providers.json` at each request (no hardcoded models)
- Also reads per-mode model overrides from `globalState.json` (set by Cline IDE plugins)
- Translates Anthropic streaming API calls (including tool calls, multi-turn, reasoning blocks) to OpenAI format
- Handles Cline OAuth token refresh automatically via `api.cline.bot/api/v1/auth/refresh`
- Picks a random available port in the 8000–9000 range

## Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` in PATH)
- [Python 3](https://www.python.org/) with `aiohttp`
- [Cline](https://cline.bot) installed with an active provider configured (any model)

## Setup

### Homebrew (recommended)

```bash
brew install Spinoza0/tap/claude-cline-proxy
```

### Manual

```bash
pip install aiohttp
chmod +x claude-cline.sh
./claude-cline.sh <your prompt>
```

## Usage

```bash
# interactive — shows provider selection menu (5s timeout, defaults to last used)
claude-cline

# quick prompt via the last-used provider
claude-cline -p "explain how streams work in Python"

# select provider explicitly (skips menu)
claude-cline --provider openrouter -p "design a database schema"

# use a specific model (skips menu, overrides provider's default model)
claude-cline --model deepseek/deepseek-v4-flash -p "hi"

# combine provider and model override
claude-cline --provider openrouter --model qwen/qwen3-coder:free -p "hi"

# equals-style flags also work
claude-cline --provider=openrouter --model=deepseek/deepseek-v4-flash

# pipe prompts
echo "refactor this code" | claude-cline --model claude-sonnet-4-20250514
```

### Provider selection menu

When run interactively without `--model` or `--provider`, a menu shows all configured providers:

```
Select provider (↑↓ to move, Enter to confirm, auto in 5s):
  → cline: cline / deepseek/deepseek-v4-flash
    openrouter: openrouter / qwen/qwen3-coder:free
    openai-compatible: openai-compatible / qwen3.5:9b
    sapaicore: sapaicore / gpt-5.4
Models: deepseek/deepseek-v4-flash qwen/qwen3-coder:free qwen3.5:9b
```

- Models shown reflect the actual active model (reads both `providers.json` and `globalState.json`)
- The `Models:` line at the bottom is copyable for use with `--model`

### Model override from IDE plugin

Cline IDE plugins (VS Code, JetBrains) store model selections in `globalState.json`.
The proxy reads this file and uses the per-mode model override automatically —
no need to manually update `providers.json`.

### Proxy logs

Logs are silenced by default. To enable debug logging:

```bash
CLAUDE_PROXY_LOG=1 claude-cline <your prompt>
# logs written to /tmp/claude-proxy-<pid>.log
```

## Advanced flags

| Flag | Description |
|------|-------------|
| `--model <name>` | Override the model name. Skips provider menu. |
| `--provider <id>` | Use a specific provider config. Skips provider menu. Use with `--model` for full override. |
| `--output-format stream-json` | Enables JSON streaming output. `--verbose` is auto-added (required by `claude --print`). |

## Configuration

All configuration comes from Cline files — no secrets or models are hardcoded:

| File | Role |
|------|------|
| `~/.cline/data/settings/providers.json` | Provider configs (API keys, base URLs, default models) |
| `~/.cline/data/globalState.json` | Per-mode model overrides (set by Cline IDE plugins) |
| `~/.cline/data/secrets.json` | OAuth idToken and refreshToken (under `cline:clineAccountId`) |
| `~/.cline/data/settings/cline_mcp_settings.json` | Tavily MCP key (optional, merged if present) |
| `claude-cline-mcp.json` | Local MCP overrides (starts empty, add your custom MCPs here) |

### Token refresh

The proxy automatically refreshes expired tokens by calling `POST api.cline.bot/api/v1/auth/refresh` with the stored refresh token. If refresh fails, re-authenticate:

```bash
cline auth
```

## WebSearch Limitation

Claude Code's built-in `WebSearch` tool will **not work** through this proxy — it is an Anthropic-only feature that requires a direct connection to the Anthropic API. If you need internet search capabilities:

1. **Tavily via Cline (automatic)** — configure Tavily as an MCP server in your Cline settings. The launcher (`claude-cline.sh`) merges it automatically.
2. **Any other search MCP** — add your preferred web search tool (e.g. Brave Search, Exa) as an MCP server in `claude-cline-mcp.json`.

## Files

| File | Purpose |
|------|---------|
| `claude-cline-proxy.py` | Local proxy: Anthropic ↔ OpenAI translation, token management, globalState model override |
| `claude-cline.sh` | Launcher: starts proxy, parses `--model`/`--provider`, auto-adds `--verbose` for stream-json, runs claude |
| `claude-cline-select.py` | Interactive TUI provider selection menu with 5s timeout |
| `claude-cline-mcp.json` | MCP server definitions (user-editable; Tavily merged from Cline automatically) |
| `AGENTS.md` | Internal architecture notes, auth flow details |
