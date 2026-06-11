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

- Reads your active provider and model from Cline's config files at each request (no hardcoded models)
- **Source of truth** for active provider: `CLINE_OVERRIDE_PROVIDER` env → `globalState.json` (set by IDE plugin) → `providers.json` → fallback to `lastUsedProvider`
- **Source of truth** for model: `CLINE_OVERRIDE_MODEL` env → `globalState.json` per-mode override → provider's default model in `providers.json`
- Ignores the model name sent by `claude` in the request body — always uses its own resolved model
- **Context window enforcement** — reads `contextWindow` from `globalState.json` model info per-provider. If the estimated token count (input + requested output) exceeds the window, the request is rejected with a clear `400` error before being sent to the upstream API, avoiding wasteful round-trips.
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
# interactive — shows provider selection menu (5s timeout, defaults to globalState)
claude-cline

# quick prompt via the globalState-selected provider
claude-cline -p "explain how streams work in Python"

# select provider explicitly (skips menu)
claude-cline --provider openrouter -p "design a database schema"

# use a specific model (skips menu, overrides all other model sources)
claude-cline --model deepseek/deepseek-v4-flash -p "hi"

# combine provider and model override
claude-cline --provider openrouter --model qwen/qwen3-coder:free -p "hi"

# equals-style flags also work
claude-cline --provider=openrouter --model=deepseek/deepseek-v4-flash

# pipe prompts
echo "refactor this code" | claude-cline --model claude-sonnet-4-20250514
```

### Provider selection menu

When run interactively without `--model` or `--provider`, a menu shows all configured providers.
The default selection comes from `globalState.json` first, then `lastUsedProvider`:

```
Select provider (↑↓ to move, Enter to confirm, auto in 5s):
  → cline: cline / kwaipilot/kat-coder-pro
    openrouter: openrouter / qwen/qwen3-coder:free
    openai-compatible: openai-compatible / qwen3.5:9b
    sapaicore: sapaicore / gpt-5.4
Models: kwaipilot/kat-coder-pro qwen/qwen3-coder:free qwen3.5:9b
```

- Models shown reflect the actual active model (reads both `providers.json` and `globalState.json`)
- The `Models:` line at the bottom is copyable for use with `--model`

### IDE plugin integration

Cline IDE plugins (VS Code, JetBrains) store selections in `globalState.json`:

| Key | Value |
|-----|-------|
| `planModeApiProvider` / `actModeApiProvider` | Active provider ID (e.g. `cline`) |
| `planModeClineModelId` / `actModeClineModelId` | Model override for the cline provider |

The proxy reads these automatically — no manual config needed.
Changes made in the plugin apply immediately on the next `claude-cline` run.

### Proxy logs

Logs are silenced by default. To enable debug logging:

```bash
CLAUDE_PROXY_LOG=1 claude-cline <your prompt>
# logs written to /tmp/claude-proxy-<pid>.log
```

## Advanced flags

| Flag | Description |
|------|-------------|
| `--model <name>` | Override the model name. Skips provider menu. Takes highest priority. |
| `--provider <id>` | Use a specific provider config. Skips provider menu. Use with `--model` for full override. |
| `--output-format stream-json` | Enables JSON streaming output. `--verbose` is auto-added (required for `stream-json` output). |

### Context window

The proxy reads `contextWindow` from `globalState.json` (`{mode}Mode<Type>ModelInfo`) for each provider. Before forwarding a request to the upstream API, it estimates input tokens (~4 chars per token) and checks if the total (input + requested `max_tokens`) fits within the limit. If not, it returns a `400` error instructing you to use `/compact` — no wasted API round-trip.

Messages that fit within the window pass through normally.

## Configuration

All configuration comes from Cline files — no secrets or models are hardcoded.

### Priority chain

1. **`CLINE_OVERRIDE_MODEL` / `CLINE_OVERRIDE_PROVIDER`** (env vars, set by `--model`/`--provider` flags)
2. **`globalState.json`** (per-mode provider and model selections from IDE plugin)
3. **`providers.json`** (API keys, base URLs, default models, lastUsedProvider)

| File | Role |
|------|------|
| `~/.cline/data/globalState.json` | **Primary**: active provider ID (`{mode}ModeApiProvider`) and per-mode model overrides (`{mode}Mode<Type>ModelId`) |
| `~/.cline/data/settings/providers.json` | **Secondary**: API keys, base URLs, provider types, default models |
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
| `claude-cline-proxy.py` | Local proxy: Anthropic ↔ OpenAI translation, token management, config resolution from globalState + providers.json |
| `claude-cline.sh` | Launcher: starts proxy, parses `--model`/`--provider`, auto-adds `--verbose` for stream-json, runs claude |
| `claude-cline-select.py` | Interactive TUI provider selection menu with 5s timeout and globalState-aware defaults |
| `claude-cline-mcp.json` | MCP server definitions (user-editable; Tavily merged from Cline automatically) |
| `AGENTS.md` | Internal architecture notes, auth flow details |
