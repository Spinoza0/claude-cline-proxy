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

## Releasing a New Version

Releasing is a single end-to-end flow. Do **all** of the following in order — never stop at just a tag, otherwise the GitHub Releases page and Homebrew users won't see the new version:

1. Update `VERSION` string in `claude-cline.sh` to match the new tag.
2. Commit, tag, and push the tag + branch:
   `git add README.md claude-cline-proxy.py claude-cline.sh && git commit -m "v1.x.x" && git tag v1.x.x && git push origin v1.x.x && git push`
3. Compute the tarball SHA256 (after the tag is pushed):
   `curl -sL "https://github.com/Spinoza0/claude-cline-proxy/archive/refs/tags/v1.x.x.tar.gz" | shasum -a 256`
4. Create the **GitHub Release** for the tag (a tag alone does NOT appear on the Releases page):
   Use a heredoc and `--notes-file` so GitHub renders line breaks correctly. Do **not** pass `--notes` with literal `\n` sequences — they will appear as `\n` on the release page.

   ```bash
   cat > /tmp/release-notes-v1.x.x.md <<'EOF'
   Describe user-facing changes here.

   - Change one
   - Change two
   EOF
   gh release create v1.x.x \
     --repo Spinoza0/claude-cline-proxy \
     --title "v1.x.x" \
     --notes-file /tmp/release-notes-v1.x.x.md
   rm -f /tmp/release-notes-v1.x.x.md
   ```
5. Update the `homebrew-tap` formula: bump the `url` tag to `v1.x.x.tar.gz` and the `sha256` to the value from step 3.
6. Push the formula tap. Stage the formula file explicitly (avoid `git add -A`, which may be blocked by git hooks):
   `cd /opt/homebrew/Library/Taps/spinoza0/homebrew-tap && git diff && git add Formula/claude-cline-proxy.rb && git commit -m "claude-cline-proxy v1.x.x" && git push`
7. Users update via: `brew upgrade Spinoza0/tap/claude-cline-proxy`

Note: if retagging (deleting and recreating the same tag), the SHA256 changes because the tarball content changes. Always compute SHA256 from the final tag, and recreate the GitHub Release (delete + re-create) since a release is bound to the tag.

## If Tokens Expire
1. `cline auth` — opens browser for OAuth (Google/GitHub)
2. Or re-authenticate in Cline IDE extension
3. After that, `claude-cline.sh` picks up fresh tokens automatically
