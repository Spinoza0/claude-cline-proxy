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
- **Context guard** — estimates input tokens and, if they exceed the model's context budget, automatically drops the oldest conversation turns (keeping `tool_use`/`tool_result` pairs intact and the system prompt) so the request fits the upstream model window, preventing `400` context-limit errors.
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

### Context guard

The upstream model has a fixed context window (e.g. `262144` tokens for many Cline models). When a conversation grows past it, the upstream returns a `400` error like *"Requested token count exceeds the model's maximum context length"*. The proxy guards against this automatically:

- It estimates input tokens (conservative ~3 chars per token) at each request.
- If the estimate exceeds the input budget, it drops the **oldest** conversation turns, keeping newer ones. Each `tool_use` is kept together with its matching `tool_result` (an assistant message and its following user result are dropped or kept as a unit), so pairs are never split. The system prompt is always preserved. At least the newest turn is kept so the request is never empty.
- The total `(input + requested max_tokens)` is also capped at `modelMaxTokens` — if you request a large `max_tokens`, the input budget shrinks accordingly.

**Configuration** (priority: env override → `providers.json` setting → built-in default):

| Setting | Env override | Default | Meaning |
|---------|--------------|---------|---------|
| `maxInputTokens` (in provider `settings`) | `CLINE_MAX_INPUT_TOKENS` | `250000` | Target input-token budget. `0` disables truncation entirely. |
| `modelMaxTokens` (in provider `settings`) | `CLINE_MODEL_MAX_TOKENS` | `262144` | Hard context limit of the upstream model; also caps `input + max_tokens`. |

Example `~/.cline/data/settings/providers.json` entry:

```json
"settings": {
  "provider": "cline",
  "model": "anthropic/claude-...",
  "maxInputTokens": 240000,
  "modelMaxTokens": 262144
}
```

Set `CLINE_MAX_INPUT_TOKENS=0` (or `maxInputTokens: 0`) to turn the guard off. When truncation happens, the proxy logs a `WARNING` with the number of dropped messages.

When a turn is dropped, Claude Code's own `/compact` is still the better long-term fix for very long sessions — the guard is a safety net that keeps requests from failing outright.

## Configuration

All configuration comes from Cline files — no secrets or models are hardcoded.

### Priority chain

1. **`CLINE_OVERRIDE_MODEL` / `CLINE_OVERRIDE_PROVIDER`** (env vars, set by `--model`/`--provider` flags)
2. **`globalState.json`** (per-mode provider and model selections from IDE plugin)
3. **`providers.json`** (API keys, base URLs, default models, lastUsedProvider)

| File | Role |
|------|------|
| `~/.cline/data/globalState.json` | **Primary**: active provider ID (`{mode}ModeApiProvider`) and per-mode model overrides (`{mode}Mode<Type>ModelId`) |
| `~/.cline/data/settings/providers.json` | **Secondary**: API keys, base URLs, provider types, default models, context-guard limits (`maxInputTokens`, `modelMaxTokens`) |
| `~/.cline/data/secrets.json` | OAuth idToken and refreshToken (under `cline:clineAccountId`) |
| `~/.cline/data/settings/cline_mcp_settings.json` | Cline MCP servers (merged automatically) |
| `claude-cline-mcp.json` | Local MCP overrides (starts empty; takes precedence over Cline MCP servers) |

### Token refresh

The proxy automatically refreshes expired tokens by calling `POST api.cline.bot/api/v1/auth/refresh` with the stored refresh token. If refresh fails, re-authenticate:

```bash
cline auth
```

## WebSearch Limitation

Claude Code's built-in `WebSearch` tool will **not work** through this proxy — it is an Anthropic-only feature that requires a direct connection to the Anthropic API. If you need internet search capabilities, add a web-search MCP server in your Cline MCP settings or in `claude-cline-mcp.json`.

## MCP server configuration

The launcher builds a single MCP config for Claude Code by merging two sources:

1. **`claude-cline-mcp.json`** (or the file pointed to by `CLAUDE_CLINE_MCP`) — local overrides.
2. **`~/.cline/data/settings/cline_mcp_settings.json`** — MCP servers configured in Cline.

### Merge rules

- **All** MCP servers from Cline settings are merged.
- **`claude-cline-mcp.json` takes precedence** — if a server is defined in both places, the local override wins.
- If neither file exists or neither defines any servers, the resulting config is empty and no MCP servers are loaded.

### Example

`claude-cline-mcp.json`:

```json
{
  "mcpServers": {
    "my-local-server": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"]
    }
  }
}
```

Cline `cline_mcp_settings.json`:

```json
{
  "mcpServers": {
    "brave-search": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "env": { "BRAVE_API_KEY": "..." }
    }
  }
}
```

Result passed to Claude Code: both `my-local-server` and `brave-search` are available. If both files defined `brave-search`, the version from `claude-cline-mcp.json` would be used.

## Files

| File | Purpose |
|------|---------|
| `claude-cline-proxy.py` | Local proxy: Anthropic ↔ OpenAI translation, token management, config resolution from globalState + providers.json |
| `claude-cline.sh` | Launcher: starts proxy, parses `--model`/`--provider`, auto-adds `--verbose` for stream-json, runs claude |
| `claude-cline-select.py` | Interactive TUI provider selection menu with 5s timeout and globalState-aware defaults |
| `claude-cline-mcp.json` | MCP server definitions (user-editable; Cline MCP servers merged automatically, local overrides take precedence) |
| `AGENTS.md` | Internal architecture notes, auth flow details |
