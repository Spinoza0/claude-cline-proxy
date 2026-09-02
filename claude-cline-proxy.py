#!/usr/bin/env python3
import json, os, sys, signal, random, asyncio, logging, uuid, time, base64
from pathlib import Path
import aiohttp
from aiohttp import web

CLINE_DATA = Path.home() / ".cline" / "data"
PROVIDERS_FILE = CLINE_DATA / "settings" / "providers.json"
SECRETS_FILE = CLINE_DATA / "secrets.json"
PORT_RANGE = (8000, 9000)
MAX_PORT_ATTEMPTS = 5
CLINE_API = "https://api.cline.bot/api/v1/chat/completions"
CLINE_REFRESH_URL = "https://api.cline.bot/api/v1/auth/refresh"
OPENROUTER_API = "https://openrouter.ai/api/v1/chat/completions"

# Input-context guard: if the estimated input token count exceeds this budget
# the proxy drops the oldest conversation units to fit. Set
# CLINE_MAX_INPUT_TOKENS=0 to disable truncation entirely.
MAX_INPUT_TOKENS = int(os.environ.get("CLINE_MAX_INPUT_TOKENS", "250000"))
# Hard context limit of the upstream model. Used to also cap
# (input + completion) so the request never exceeds the model window even
# when the caller requests a large max_tokens.
MODEL_MAX_TOKENS = int(os.environ.get("CLINE_MODEL_MAX_TOKENS", "262144"))
# Safety margin (tokens) kept free below the hard limit.
TRUNC_MARGIN = 1024
# Rough chars->tokens divisor. Conservative (3) so we never *under*estimate
# and risk exceeding the real limit.
TOKEN_DIVISOR = 3

LOG_LEVEL = logging.DEBUG if os.environ.get("CLAUDE_PROXY_LOG") else logging.INFO
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logger = logging.getLogger("claude-proxy")


def decode_jwt_exp(token: str) -> int:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("exp", 0)
    except Exception:
        return 0


def token_valid(token: str) -> bool:
    return bool(token) and time.time() < decode_jwt_exp(token)


def extract_valid_id_token(acc_data: str | dict) -> str:
    try:
        if isinstance(acc_data, str):
            acc_data = json.loads(acc_data)
        id_token = acc_data.get("idToken", "")
        if id_token and token_valid(id_token):
            # check if it's a WorkOS idToken (has client_id in claims)
            return "workos:" + id_token
    except (json.JSONDecodeError, KeyError, Exception):
        pass
    return ""


def extract_valid_access_token(s: dict) -> str:
    raw = s.get("auth", {}).get("accessToken", "")
    if raw.startswith("workos:"):
        raw_token = raw[7:]
        if token_valid(raw_token):
            return raw
    return ""


async def refresh_and_save_tokens(providers: dict, active_id: str, s: dict) -> str:
    refresh_token = s.get("auth", {}).get("refreshToken", "")

    secrets = json.loads(SECRETS_FILE.read_text()) if SECRETS_FILE.exists() else {}
    acc_val = secrets.get("cline:clineAccountId", "")
    if acc_val:
        try:
            acc_data = json.loads(acc_val) if isinstance(acc_val, str) else acc_val
            rt2 = acc_data.get("refreshToken", "")
            if rt2:
                refresh_token = rt2
                logger.info("Using refresh token from secrets")
        except Exception:
            pass

    if not refresh_token:
        raise RuntimeError("No refresh token available. Run 'cline auth' to re-authenticate.")

    try:
        result = await do_token_refresh(refresh_token)
        new_access = result["access_token"]
        new_refresh = result["refresh_token"]

        expires_at = result.get("expires_at")
        if isinstance(expires_at, str):
            from datetime import datetime
            expires_ms = int(datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp() * 1000)
        else:
            expires_ms = int(expires_at) if expires_at else int((time.time() + 3600) * 1000)

        providers["providers"][active_id]["settings"]["auth"]["accessToken"] = "workos:" + new_access
        providers["providers"][active_id]["settings"]["auth"]["refreshToken"] = new_refresh
        providers["providers"][active_id]["settings"]["auth"]["expiresAt"] = expires_ms
        PROVIDERS_FILE.write_text(json.dumps(providers, indent=2))

        if acc_val:
            try:
                secrets_data = json.loads(acc_val) if isinstance(acc_val, str) else acc_val
                secrets_data["idToken"] = new_access
                secrets_data["refreshToken"] = new_refresh
                secrets["cline:clineAccountId"] = json.dumps(secrets_data)
                SECRETS_FILE.write_text(json.dumps(secrets, indent=2))
                logger.info("Updated secrets.json with new tokens")
            except Exception:
                pass

        logger.info("Token refresh successful")
        return "workos:" + new_access
    except Exception as e:
        logger.warning("Token refresh attempt failed: %s", e)

    raw = s.get("auth", {}).get("accessToken", "")
    if raw.startswith("workos:"):
        raw_token = raw[7:]
        if token_valid(raw_token):
            logger.info("Falling back to existing accessToken")
            return raw

    raise RuntimeError("All tokens expired and refresh failed. Run 'cline auth' to re-authenticate.")


async def do_token_refresh(refresh_token: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Cline/3.0.15",
        "X-Client-Version": "0.0.0",
        "X-Core-Version": "0.0.0",
    }
    body = {"refreshToken": refresh_token, "grantType": "refresh_token"}
    async with aiohttp.ClientSession() as sess:
        async with sess.post(CLINE_REFRESH_URL, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"Cline API refresh failed ({resp.status}): {err[:300]}")
            data = await resp.json()
            if not data.get("success"):
                raise RuntimeError(f"Cline API refresh returned error: {data}")
            ad = data["data"]
            return {"access_token": ad["accessToken"], "refresh_token": ad["refreshToken"], "expires_at": ad["expiresAt"]}


def get_gs_active_id(providers: dict) -> str | None:
    """Read the active provider ID from globalState.json (if available)."""
    GS_PATH = Path.home() / ".cline" / "data" / "globalState.json"
    if not GS_PATH.exists():
        return None
    try:
        gs = json.loads(GS_PATH.read_text())
        mode = gs.get("mode", "act").lower()
        gs_provider = gs.get(f"{mode}ModeApiProvider", "")
        # Validate it's a known provider before we switch to it
        if gs_provider and gs_provider in providers.get("providers", {}):
            return gs_provider
    except Exception:
        pass
    return None


async def load_cline_config():
    providers = json.loads(PROVIDERS_FILE.read_text())
    secrets = json.loads(SECRETS_FILE.read_text()) if SECRETS_FILE.exists() else {}

    # Priority: explicit user override → globalState (IDE plugin) → lastUsedProvider
    active_id = (
        os.environ.get("CLINE_OVERRIDE_PROVIDER")
        or get_gs_active_id(providers)
        or providers.get("lastUsedProvider", "cline")
    )
    active = providers["providers"].get(active_id)
    if not active:
        raise RuntimeError(f"Provider '{active_id}' not found")

    s = active["settings"]
    provider = s["provider"]
    model = os.environ.get("CLINE_OVERRIDE_MODEL") or s["model"]
    api_key = ""
    api_url = ""

    # Check globalState.json for per-mode model override
    GLOBAL_STATE_FILE = Path.home() / ".cline" / "data" / "globalState.json"
    if GLOBAL_STATE_FILE.exists():
        try:
            gs = json.loads(GLOBAL_STATE_FILE.read_text())
            key_suffix_map = {
                "cline": "Cline",
                "openrouter": "OpenRouter",
                "openai": "OpenAi",
                "openai-compatible": "OpenAi",
                "fireworks": "Fireworks",
            }
            mode = gs.get("mode", "act").lower()
            gs_model_key = f"{mode}Mode{key_suffix_map.get(provider, provider.title())}ModelId"
            gs_model = gs.get(gs_model_key, "")
            if gs_model and not os.environ.get("CLINE_OVERRIDE_MODEL"):
                model = gs_model
        except Exception as e:
            logger.warning("Failed to read globalState.json: %s", e)

    if provider == "cline":
        workos_token = ""
        acc_data_str = secrets.get("cline:clineAccountId", "")
        if acc_data_str:
            workos_token = extract_valid_id_token(acc_data_str)
        if not workos_token:
            workos_token = extract_valid_access_token(s)
        if not workos_token:
            workos_token = await refresh_and_save_tokens(providers, active_id, s)
        api_key = workos_token
        api_url = CLINE_API
    elif provider == "openrouter":
        api_key = s.get("apiKey", secrets.get("openRouterApiKey", ""))
        api_url = OPENROUTER_API
    elif provider in ("openai", "openai-compatible"):
        api_key = s.get("apiKey", "")
        base = s.get("baseUrl", "").rstrip("/")
        api_url = base + "/chat/completions"
    elif provider == "anthropic":
        api_key = s.get("apiKey", "")
        api_url = s.get("baseUrl", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
    else:
        raise RuntimeError(f"Unsupported provider: {provider}")

    if not api_key and provider not in ("openai", "openai-compatible"):
        raise RuntimeError(f"No API key for provider '{provider}'")

    # Context-guard limits. Env override (CLINE_MAX_INPUT_TOKENS /
    # CLINE_MODEL_MAX_TOKENS) takes priority, otherwise read from the active
    # provider settings (Cline config), falling back to built-in defaults.
    max_input_raw = os.environ.get("CLINE_MAX_INPUT_TOKENS")
    max_input_tokens = int(max_input_raw) if max_input_raw else int(s.get("maxInputTokens", MAX_INPUT_TOKENS))
    model_max_raw = os.environ.get("CLINE_MODEL_MAX_TOKENS")
    model_max_tokens = int(model_max_raw) if model_max_raw else int(s.get("modelMaxTokens", MODEL_MAX_TOKENS))

    logger.info("Config: provider=%s model=%s api=%s",
                provider, model,
                api_url.split("//")[1] if "//" in api_url else api_url)
    return {
        "api_url": api_url,
        "api_key": api_key,
        "model": model,
        "provider": provider,
        "max_input_tokens": max_input_tokens,
        "model_max_tokens": model_max_tokens,
    }


def make_msg_id():
    return "msg_" + uuid.uuid4().hex[:24]


ANTHROPIC_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "content_filter",
}


def translate_tool_result_content(content) -> str | list:
    """Convert Anthropic tool_result content into OpenAI tool message content.

    Anthropic tool results can be a plain string or a list of content blocks
    (text, image, document, ...). OpenAI tool messages only accept text /
    image_url content parts, so blocks are converted or replaced with a text
    marker — otherwise the upstream Cline API fails to deserialize the body.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        oai_parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                oai_parts.append({"type": "text", "text": block.get("text", "")})
            elif btype == "image":
                src = block.get("source", {})
                if isinstance(src, dict) and src.get("type") == "base64":
                    oai_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{src.get('media_type', '')};base64,{src.get('data', '')}"},
                    })
            elif btype == "document":
                src = block.get("source", {})
                if isinstance(src, dict):
                    media = src.get("media_type", "application/pdf")
                    oai_parts.append({"type": "text", "text": f"[document: {media}, {len(src.get('data', ''))} bytes]"})
                else:
                    oai_parts.append({"type": "text", "text": "[document]"})
            else:
                oai_parts.append({"type": "text", "text": f"[block: {btype}]"})
        return oai_parts if oai_parts else ""
    return ""


def _estimate_tokens(text_or_obj) -> int:
    """Rough token estimate. Conservative divisor so we never undercount."""
    if text_or_obj is None:
        return 0
    if isinstance(text_or_obj, str):
        s = text_or_obj
    else:
        try:
            s = json.dumps(text_or_obj, ensure_ascii=False)
        except Exception:
            s = str(text_or_obj)
    return max(1, len(s) // TOKEN_DIVISOR)


def _tool_use_ids(msg: dict) -> list:
    ids = []
    if msg.get("role") == "assistant":
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    ids.append(b.get("id"))
    return ids


def _tool_result_ids(msg: dict) -> list:
    ids = []
    if msg.get("role") == "user":
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    ids.append(b.get("tool_use_id"))
    return ids


def _build_units(messages: list) -> list:
    """Group messages into units so tool_use/tool_result pairs stay together.

    A unit is an assistant message containing tool_use plus the immediately
    following user message(s) that carry the matching tool_result blocks.
    Keeping/dropping a unit as a whole guarantees we never split a pair.
    """
    units = []
    i = 0
    n = len(messages)
    while i < n:
        msg = messages[i]
        tu = _tool_use_ids(msg)
        if tu:
            unit = [i]
            used = set(tu)
            j = i + 1
            while j < n:
                tr = _tool_result_ids(messages[j])
                if tr and (set(tr) & used):
                    unit.append(j)
                    used |= set(tr)
                    j += 1
                else:
                    break
            units.append(unit)
            i = j
        else:
            units.append([i])
            i += 1
    return units


def _truncate_messages(body: dict, max_input_tokens: int, model_max_tokens: int):
    """Drop oldest conversation units so the input fits the token budget.

    Returns (dropped_message_count, estimated_total_tokens) for logging.
    """
    system = body.get("system")
    sys_tok = _estimate_tokens(system)
    messages = body.get("messages", [])
    if not messages:
        return 0, sys_tok

    max_tokens_field = body.get("max_tokens", 4096)
    avail = min(max_input_tokens, model_max_tokens - max_tokens_field - TRUNC_MARGIN)
    if avail <= 0:
        avail = max_input_tokens

    units = _build_units(messages)
    unit_tok = [sum(_estimate_tokens(messages[idx]) for idx in u) for u in units]

    keep_units = []
    total = 0
    for u, tok in reversed(list(zip(units, unit_tok))):
        if total + tok <= avail:
            keep_units.append(u)
            total += tok
        else:
            break

    if not keep_units:
        # Never emit an empty request: keep at least the newest unit.
        keep_units = [units[-1]]

    keep_units.reverse()
    kept_set = {idx for u in keep_units for idx in u}
    new_messages = [messages[i] for i in range(len(messages)) if i in kept_set]

    dropped = len(messages) - len(new_messages)
    body["messages"] = new_messages
    final_tok = total + sys_tok
    if dropped:
        logger.info("Context guard: kept %d messages (est. %d tokens), dropped %d", len(new_messages), final_tok, dropped)
    return dropped, final_tok


def translate_request(body: dict, config: dict) -> dict:
    max_input_tokens = config.get("max_input_tokens", MAX_INPUT_TOKENS)
    model_max_tokens = config.get("model_max_tokens", MODEL_MAX_TOKENS)
    if max_input_tokens and max_input_tokens > 0:
        dropped, est_total = _truncate_messages(body, max_input_tokens, model_max_tokens)
        if dropped:
            logger.warning(
                "Context guard: dropped %d oldest message(s) (est. %d input tokens) to fit model limit %d",
                dropped, est_total, model_max_tokens,
            )

    messages = []
    if body.get("system"):
        system = body["system"]
        if isinstance(system, list):
            system = [
                {"type": "text", "text": b.get("text", "")}
                for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            ]
        messages.append({"role": "system", "content": system})

    for m in body.get("messages", []):
        role = m["role"]
        content = m.get("content", "")
        if role == "assistant" and isinstance(content, list):
            text_parts = [b for b in content if b.get("type") == "text"]
            tool_parts = [b for b in content if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text_parts[0].get("text", "") if text_parts else ""}
            if tool_parts:
                msg["tool_calls"] = []
                for tc in tool_parts:
                    msg["tool_calls"].append({
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": json.dumps(tc.get("input", {})),
                        },
                    })
            messages.append(msg)
        elif role == "user" and isinstance(content, list):
            oai_parts = []
            for block in content:
                if block.get("type") == "text":
                    oai_parts.append({"type": "text", "text": block.get("text", "")})
                elif block.get("type") == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        oai_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{src.get('media_type', '')};base64,{src.get('data', '')}"},
                        })
                elif block.get("type") == "tool_result":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": translate_tool_result_content(block.get("content", "")),
                    })
            if oai_parts:
                has_user = any(
                    r["role"] == "user" and r.get("content") == oai_parts
                    for r in messages
                )
                if not has_user:
                    messages.append({"role": "user", "content": oai_parts})
        elif role == "user":
            messages.append({"role": "user", "content": content})
        elif role == "assistant":
            messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            messages.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m.get("content", "")})

    oai_body: dict = {
        "model": config["model"],
        "messages": messages,
        "stream": body.get("stream", False),
        "max_tokens": body.get("max_tokens", 4096),
    }
    # Ask the upstream for a final usage chunk so we can report real
    # input_tokens back to Claude Code (required for autocompact).
    if body.get("stream"):
        oai_body["stream_options"] = {"include_usage": True}
    if "temperature" in body:
        oai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        oai_body["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        oai_body["stop"] = body["stop_sequences"]

    tools = body.get("tools")
    if tools:
        oai_body["tools"] = []
        for t in tools:
            oai_body["tools"].append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            })
    return oai_body


def build_anthropic_response(openai_body: dict, config: dict, model_name: str = "") -> dict:
    choice = openai_body.get("choices", [{}])[0]
    msg = choice.get("message", {})
    content_blocks = []
    if msg.get("content"):
        content_blocks.append({"type": "text", "text": msg["content"]})
    for tc in (msg.get("tool_calls") or []):
        try:
            inp = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            inp = tc["function"]["arguments"]
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc["function"]["name"],
            "input": inp,
        })

    finish = choice.get("finish_reason", "stop")
    stop_type = choice.get("stop_reason") or ANTHROPIC_STOP_REASONS.get(finish, "end_turn")
    usage = openai_body.get("usage", {})
    return {
        "id": make_msg_id(),
        "type": "message",
        "role": "assistant",
        "content": content_blocks,
        "model": model_name or config["model"],
        "stop_reason": stop_type,
        "stop_sequence": choice.get("stop_sequence"),
        "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
    }


async def call_openai(config: dict, oai_body: dict) -> dict:
    headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=30,
        sock_connect=30,
        sock_read=120,
    )
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.post(config["api_url"], json=oai_body, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"API error {resp.status}: {err[:500]}")
            result = await resp.json()
            if isinstance(result, dict) and "data" in result:
                result = result["data"]
            return result


async def stream_openai(config: dict, oai_body: dict) -> tuple[aiohttp.ClientResponse, aiohttp.ClientSession]:
    headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

    # SSE streams should use short-lived connections. Some openai-compatible
    # providers reset idle keep-alive sockets aggressively, which leads to
    # "Tool use interrupted" errors in Claude Code. Disable connection reuse
    # for streaming and use per-phase timeouts instead of a single total limit.
    connector = aiohttp.TCPConnector(
        force_close=True,
        enable_cleanup_closed=True,
    )
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=30,
        sock_connect=30,
        sock_read=600,
    )
    sess = aiohttp.ClientSession(connector=connector, timeout=timeout)
    try:
        resp = await sess.post(config["api_url"], json=oai_body, headers=headers, timeout=timeout)
        if resp.status != 200:
            err = await resp.text()
            await sess.close()
            raise RuntimeError(f"API error {resp.status}: {err[:500]}")
        return resp, sess
    except Exception:
        await sess.close()
        raise


async def handle_messages(request: web.Request) -> web.Response:
    if request.content_type not in ("application/json",):
        return web.json_response({"error": {"type": "invalid_request_error", "message": "Expected application/json"}}, status=400)

    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        return web.json_response({"error": {"type": "invalid_request_error", "message": f"Invalid JSON: {e}"}}, status=400)

    try:
        config = await load_cline_config()
    except Exception as e:
        logger.error("Config error: %s", e)
        return web.json_response({"error": {"type": "api_error", "message": str(e)}}, status=500)

    is_stream = body.get("stream", False)
    model_name = config["model"]

    try:
        oai_body = translate_request(body, config)
    except Exception as e:
        logger.error("Translate error: %s", e)
        return web.json_response({"error": {"type": "invalid_request_error", "message": str(e)}}, status=400)

    if is_stream:
        return await handle_stream(request, config, oai_body, model_name)
    else:
        return await handle_non_stream(config, oai_body, model_name)


async def handle_non_stream(config: dict, oai_body: dict, model_name: str) -> web.Response:
    try:
        oai_resp = await call_openai(config, oai_body)
    except Exception as e:
        logger.error("API call error: %s", e)
        return web.json_response({"error": {"type": "api_error", "message": str(e)}}, status=502)

    try:
        anth_response = build_anthropic_response(oai_resp, config, model_name)
    except Exception as e:
        logger.error("Response build error: %s", e)
        return web.json_response({"error": {"type": "api_error", "message": str(e)}}, status=500)

    return web.json_response(anth_response, headers={"x-request-id": anth_response["id"]})


async def _emit_stream_error(resp: web.StreamResponse, message: str):
    """Send an Anthropic error event so Claude Code handles the failure gracefully."""
    try:
        event = f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': message}})}\n\n"
        await resp.write(event.encode())
    except Exception:
        pass


async def _keepalive_writer(resp: web.StreamResponse, stop: asyncio.Event):
    """Send SSE comment pings so Claude Code's connection stays alive while the upstream model thinks."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            try:
                await resp.write(b": keepalive\n\n")
            except Exception:
                break


async def handle_stream(request: web.Request, config: dict, oai_body: dict, model_name: str) -> web.StreamResponse:
    try:
        upstream_resp, sess = await stream_openai(config, oai_body)
    except Exception as e:
        logger.error("Stream init error: %s", e)
        return web.json_response({"error": {"type": "api_error", "message": str(e)}}, status=502)

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "x-vercel-ai-data-stream": "v1",
        },
    )
    await resp.prepare(request)

    msg_id = make_msg_id()
    cb_index = [0]

    stop_keepalive = asyncio.Event()
    keepalive_task = asyncio.create_task(_keepalive_writer(resp, stop_keepalive))

    def send_anthropic_event(event_type: str, data: dict):
        return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"

    async def emit(text: str):
        await resp.write(text.encode())

    await emit(send_anthropic_event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model_name,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }))

    current_tool_calls: dict[int, dict] = {}
    text_started = False
    thinking_started = False
    final_finish = None
    last_usage: dict = {}
    stream_errored = False

    try:
        stream_chunk_count = 0
        last_chunk_time = time.time()
        async for line in upstream_resp.content:
            now = time.time()
            line = line.decode().strip()
            if not line:
                continue
            if line == "data: [DONE]":
                logger.debug("SSE: received [DONE] after %d chunks, %.1fs since last", stream_chunk_count, now - last_chunk_time)
                break
            if not line.startswith("data: "):
                continue

            raw = line[6:]
            stream_chunk_count += 1
            elapsed = now - last_chunk_time
            if elapsed > 10:
                logger.warning("SSE: gap %.1fs between chunks (chunk #%d)", elapsed, stream_chunk_count)
            last_chunk_time = now
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Ignoring malformed SSE chunk: %s", raw[:200])
                continue

            # Some providers send token usage in a dedicated choices:[] chunk
            # (when stream_options.include_usage is enabled). Capture it even
            # when there are no choices so Claude Code sees real input_tokens
            # and can trigger autocompact on its own.
            if "usage" in chunk:
                last_usage = chunk.get("usage", {})

            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta") or {}
            finish = choices[0].get("finish_reason")

            tc_in_chunk = delta.get("tool_calls")
            if finish or tc_in_chunk:
                logger.debug("SSE chunk: finish=%s tool_calls=%s content=%s reasoning=%s",
                             finish,
                             [tc.get("function", {}).get("name") for tc in tc_in_chunk] if tc_in_chunk else None,
                             bool(delta.get("content")),
                             bool(delta.get("reasoning")))

            reasoning = delta.get("reasoning", "")
            if reasoning:
                if not thinking_started:
                    await emit(send_anthropic_event("content_block_start", {
                        "type": "content_block_start",
                        "index": cb_index[0],
                        "content_block": {"type": "thinking", "thinking": ""},
                    }))
                    cb_index[0] += 1
                    thinking_started = True
                await emit(send_anthropic_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": cb_index[0] - 1,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                }))

            content = delta.get("content", "")
            if content:
                if thinking_started and not text_started:
                    await emit(send_anthropic_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": cb_index[0] - 1,
                    }))
                    thinking_started = False
                if not text_started:
                    await emit(send_anthropic_event("content_block_start", {
                        "type": "content_block_start",
                        "index": cb_index[0],
                        "content_block": {"type": "text", "text": ""},
                    }))
                    cb_index[0] += 1
                    text_started = True
                await emit(send_anthropic_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": cb_index[0] - 1,
                    "delta": {"type": "text_delta", "text": content},
                }))

            tool_calls = delta.get("tool_calls") or []
            for tc in tool_calls:
                tc_idx = tc.get("index", 0)
                if tc_idx not in current_tool_calls:
                    if text_started:
                        await emit(send_anthropic_event("content_block_stop", {
                            "type": "content_block_stop",
                            "index": cb_index[0] - 1,
                        }))
                        text_started = False
                    if thinking_started:
                        await emit(send_anthropic_event("content_block_stop", {
                            "type": "content_block_stop",
                            "index": cb_index[0] - 1,
                        }))
                        thinking_started = False
                    blk_idx = cb_index[0]
                    cb_index[0] += 1
                    current_tool_calls[tc_idx] = {"id": "", "name": "", "arguments": "", "block_idx": blk_idx}
                    await emit(send_anthropic_event("content_block_start", {
                        "type": "content_block_start",
                        "index": blk_idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": tc.get("function", {}).get("name", ""),
                            "input": {},
                        },
                    }))
                tcc = current_tool_calls[tc_idx]
                if tc.get("id"):
                    tcc["id"] = tc["id"]
                if tc.get("function", {}).get("name"):
                    tcc["name"] = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    tcc["arguments"] += tc["function"]["arguments"]
                if tc.get("function", {}).get("arguments"):
                    await emit(send_anthropic_event("content_block_delta", {
                        "type": "content_block_delta",
                        "index": tcc["block_idx"],
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": tc["function"]["arguments"],
                        },
                    }))

            if finish:
                if thinking_started:
                    await emit(send_anthropic_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": cb_index[0] - 1,
                    }))
                    thinking_started = False
                if text_started:
                    await emit(send_anthropic_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": cb_index[0] - 1,
                    }))
                    text_started = False

                for idx in sorted(current_tool_calls.keys()):
                    tcc = current_tool_calls[idx]
                    await emit(send_anthropic_event("content_block_stop", {
                        "type": "content_block_stop",
                        "index": tcc["block_idx"],
                    }))
                current_tool_calls.clear()

                final_finish = finish

        logger.debug("SSE stream loop done: final_finish=%s chunks=%s thinking=%s text=%s tool_calls=%s",
                     final_finish, stream_chunk_count, thinking_started, text_started,
                     list(current_tool_calls.keys()) if current_tool_calls else None)

    except (ConnectionResetError, asyncio.CancelledError) as e:
        logger.info("Connection reset: %s", e)
        stream_errored = True
        await _emit_stream_error(resp, f"Connection reset: {e}")
    except aiohttp.ClientPayloadError as e:
        logger.error("Upstream SSE payload error: %s", e)
        stream_errored = True
        await _emit_stream_error(resp, f"Upstream interrupted: {e}")
    except aiohttp.ClientConnectionError as e:
        logger.error("Upstream connection error: %s", e)
        stream_errored = True
        await _emit_stream_error(resp, f"Connection lost: {e}")
    except aiohttp.ServerTimeoutError as e:
        logger.error("Upstream SSE timeout: %s", e)
        stream_errored = True
        await _emit_stream_error(resp, f"Upstream timeout: {e}")
    except Exception as e:
        logger.error("Stream error: %s", e)
        logger.exception(e)
        stream_errored = True
        await _emit_stream_error(resp, f"Stream error: {e}")
    finally:
        stop_keepalive.set()
        try:
            await keepalive_task
        except Exception:
            pass

        if final_finish is None and not stream_errored:
            logger.warning("SSE stream ended without finish_reason (%d chunks, last chunk %.1fs ago)",
                           stream_chunk_count, time.time() - last_chunk_time)

        # Emit the final message_delta with the real (accumulated) usage so
        # Claude Code tracks context usage and triggers autocompact.
        # Skip when the stream errored — an error event was already sent and
        # sending message_stop would confuse Claude Code.
        if final_finish is not None and not stream_errored:
            await emit(send_anthropic_event("message_delta", {
                "type": "message_delta",
                "delta": {
                    "stop_reason": ANTHROPIC_STOP_REASONS.get(final_finish, "end_turn"),
                    "stop_sequence": None,
                },
                "usage": {
                    "output_tokens": last_usage.get("completion_tokens", 0),
                    "input_tokens": last_usage.get("prompt_tokens", 0),
                },
            }))
            await emit(send_anthropic_event("message_stop", {"type": "message_stop"}))

        try:
            await resp.write_eof()
        except (ConnectionResetError, ConnectionError):
            pass
        upstream_resp.close()
        if not sess.closed:
            await sess.close()

    return resp


async def handle_health(request: web.Request) -> web.Response:
    try:
        config = await load_cline_config()
        return web.json_response({"status": "ok", "provider": config["provider"], "model": config["model"]})
    except Exception as e:
        return web.json_response({"status": "error", "message": str(e)}, status=503)


async def find_connections() -> list:
    try:
        proc = await asyncio.create_subprocess_exec("lsof", "-i", "-P", "-n", "-sTCP:LISTEN", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        conns = []
        for line in stdout.decode().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 9:
                addr = parts[8]
                if ":" in addr:
                    host, port = addr.rsplit(":", 1)
                    try:
                        conns.append((host, int(port)))
                    except ValueError:
                        pass
        return conns
    except Exception:
        return []


async def find_random_free_port(start_port: int = 8000, end_port: int = 9000, max_attempts: int = 5) -> int:
    conns = await find_connections()
    used_ports = {p for _, p in conns}

    for _ in range(max_attempts):
        port = random.randint(start_port, end_port)
        if port not in used_ports:
            return port
    raise RuntimeError(f"No free port in range {start_port}-{end_port} after {max_attempts} attempts")


async def main():
    port = await find_random_free_port(*PORT_RANGE, MAX_PORT_ATTEMPTS)
    logger.info("Cline proxy starting on port %d", port)
    logger.info("Config file: %s", PROVIDERS_FILE)

    proxy_pid = os.getpid()
    app = web.Application(client_max_size=100 * 1024 * 1024)
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    port_file = Path(os.environ.get("CLAUDE_PROXY_PORT_FILE", "/tmp/claude-proxy-port.txt"))
    port_file.write_text(str(port))

    print(f"CLINE_PROXY_PORT={port}", flush=True)

    stop_event = asyncio.Event()

    def shutdown():
        if not stop_event.is_set():
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        await runner.cleanup()
        port_file = Path(os.environ.get("CLAUDE_PROXY_PORT_FILE", "/tmp/claude-proxy-port.txt"))
        if port_file.exists():
            port_file.unlink()
        logger.info("Proxy stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted")
    except RuntimeError as e:
        logger.error("Fatal: %s", e)
        sys.exit(1)
