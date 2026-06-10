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

## Language
- English is the official language of this project. All code, comments, documentation, commit messages, and communication must be in English.

## Key Architecture Decisions
- Model is read from globalState.json first, then providers.json
- Tavily MCP — only from Cline config (conditional), no hardcoded keys
- `--bare` not needed — `ANTHROPIC_API_KEY` (dummy) suffices for all tools
- Proxy port — random from 8000-9000, up to 5 attempts
- Streaming (SSE → Anthropic format) supported
- Tool calls and multi-turn work

## Updating the Homebrew Formula

When a new version of claude-cline-proxy is released:

1. Update `VERSION` string in `claude-cline.sh` to match the new tag
2. Commit, tag, and push: `git add -A && git commit -m "v1.x.x" && git tag v1.x.x && git push origin v1.x.x && git push`
3. Update `homebrew-tap` formula: change the `url` tag and `sha256` checksum
4. Compute SHA256: `curl -sL "https://github.com/Spinoza0/claude-cline-proxy/archive/refs/tags/v1.x.x.tar.gz" | shasum -a 256`
5. Push the formula tap: `cd /opt/homebrew/Library/Taps/spinoza0/homebrew-tap && git add -A && git commit -m "..." && git push`
6. Users update via: `brew upgrade Spinoza0/tap/claude-cline-proxy`

Note: if retagging (deleting and recreating the same tag), the SHA256 changes because the tarball content changes. Always compute SHA256 from the final tag.

## If Tokens Expire
1. `cline auth` — opens browser for OAuth (Google/GitHub)
2. Or re-authenticate in Cline IDE extension
3. After that, `claude-cline.sh` picks up fresh tokens automatically
