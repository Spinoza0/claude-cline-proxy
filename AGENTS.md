## Project Context
- **Goal**: Run Claude Code CLI through a Cline provider (api.cline.bot) with any model from Cline config
- **Proxy**: claude-cline-proxy.py — translates Anthropic Messages API ↔ OpenAI Chat Completions API
- **Launcher**: claude-cline.sh — starts proxy, launches claude CLI

## How Cline Authentication Works

### Token Files
- `~/.cline/data/secrets.json` — key `cline:clineAccountId`:
  - `idToken` — raw JWT (no workos: prefix)
  - `refreshToken` — Firebase refresh token
  - `expiresAt` — Unix timestamp (seconds)
- `~/.cline/data/settings/providers.json` — `auth` section:
  - `accessToken` — `workos:<raw JWT>` (with prefix)
  - `refreshToken` — copy of refresh token
  - `expiresAt` — Unix timestamp (ms)

### Token Refresh (implemented in claude-cline-proxy.py)
- Endpoint: `POST https://api.cline.bot/api/v1/auth/refresh`
- Body: `{"refreshToken": "<refreshToken>", "grantType": "refresh_token"}`
- Headers: Content-Type/Accept application/json, User-Agent
- Response: `{"success":true,"data":{"accessToken":"...","refreshToken":"...","expiresAt":"ISO8601"}}`
- After refresh, both files are updated (providers.json + secrets.json)

### How Cline CLI Gets the Token
1. `AuthService.getAuthToken()` → calls `ClineAuthProvider.retrieveClineAuthInfo(controller)`
2. Reads `cline:clineAccountId` from storage (secrets.json)
3. If token expired — calls `refreshToken()` → `POST api.cline.bot/api/v1/auth/refresh`
4. Returns `workos:<accessToken>`

### Token Selection Order in claude-cline-proxy.py
1. idToken from secrets.json (if valid) → `workos:` + idToken
2. accessToken from providers.json (if valid) → as-is
3. Refresh via Cline API → update both files → `workos:` + newAccessToken

## Key Architecture Decisions
- Model is read dynamically from providers.json on each request
- Tavily MCP — only from Cline config (conditional), no hardcoded keys
- `--bare` not needed — `ANTHROPIC_API_KEY` (dummy) suffices for all tools
- Proxy port — random from 8000-9000, up to 5 attempts
- Streaming (SSE → Anthropic format) supported
- Tool calls and multi-turn work

## If Tokens Expire
1. `cline auth` — opens browser for OAuth (Google/GitHub)
2. Or re-authenticate in Cline IDE extension
3. After that, `claude-cline.sh` picks up fresh tokens automatically
