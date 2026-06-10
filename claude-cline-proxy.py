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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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


def extract_valid_id_token(acc_data_str: str) -> str:
    try:
        acc_data = json.loads(acc_data_str)
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
    acc_data_str = secrets.get("cline:clineAccountId", "")
    if acc_data_str:
        try:
            acc_data = json.loads(acc_data_str)
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

        providers["providers"][active_id]["settings"]["auth"]["accessToken"] = "workos:" + new_access
        providers["providers"][active_id]["settings"]["auth"]["refreshToken"] = new_refresh
        providers["providers"][active_id]["settings"]["auth"]["expiresAt"] = int((time.time() + 3600) * 1000)
        PROVIDERS_FILE.write_text(json.dumps(providers, indent=2))

        if acc_data_str:
            try:
                secrets_data = json.loads(acc_data_str)
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

    logger.info("Config: provider=%s model=%s api=%s", provider, model, api_url.split("//")[1] if "//" in api_url else api_url)
    return {"api_url": api_url, "api_key": api_key, "model": model, "provider": provider}


def make_msg_id():
    return "msg_" + uuid.uuid4().hex[:24]


ANTHROPIC_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "content_filter",
}


def translate_request(body: dict, config: dict) -> dict:
    messages = []
    if body.get("system"):
        messages.append({"role": "system", "content": body["system"]})

    for m in body.get("messages", []):
        role = m["role"]
        content = m.get("content", "")
        if role == "assistant" and isinstance(content, list):
            text_parts = [b for b in content if b.get("type") == "text"]
            tool_parts = [b for b in content if b.get("type") == "tool_use"]
            msg = {"role": "assistant"}
            if text_parts:
                msg["content"] = text_parts[0].get("text", "")
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
                            "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"},
                        })
                elif block.get("type") == "tool_result":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": block.get("content", ""),
                    })
            if oai_parts:
                has_user = any(
                    r["role"] == "user" and r.get("content") is oai_parts
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

    async with aiohttp.ClientSession() as sess:
        async with sess.post(config["api_url"], json=oai_body, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                err = await resp.text()
                raise RuntimeError(f"API error {resp.status}: {err}")
            result = await resp.json()
            if isinstance(result, dict) and "data" in result:
                result = result["data"]
            return result


async def stream_openai(config: dict, oai_body: dict) -> aiohttp.ClientResponse:
    headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

    conn = aiohttp.TCPConnector()
    sess = aiohttp.ClientSession(connector=conn)
    try:
        resp = await sess.post(config["api_url"], json=oai_body, headers=headers, timeout=aiohttp.ClientTimeout(total=300))
        if resp.status != 200:
            err = await resp.text()
            await sess.close()
            raise RuntimeError(f"API error {resp.status}: {err}")
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

    try:
        async for line in upstream_resp.content:
            line = line.decode().strip()
            if not line or line == "data: [DONE]":
                continue
            if not line.startswith("data: "):
                continue

            raw = line[6:]
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue

            choices = chunk.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta") or {}
            finish = choices[0].get("finish_reason")

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

                usage = chunk.get("usage", {})
                await emit(send_anthropic_event("message_delta", {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": ANTHROPIC_STOP_REASONS.get(finish, "end_turn"),
                        "stop_sequence": None,
                    },
                    "usage": {
                        "output_tokens": usage.get("completion_tokens", 0),
                        "input_tokens": usage.get("prompt_tokens", 0),
                    },
                }))
                await emit(send_anthropic_event("message_stop", {"type": "message_stop"}))

    except (ConnectionResetError, asyncio.CancelledError):
        logger.info("Client disconnected")
    except Exception as e:
        logger.error("Stream error: %s", e)
    finally:
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


def find_port() -> int:
    for attempt in range(1, MAX_PORT_ATTEMPTS + 1):
        port = random.randint(*PORT_RANGE)
        if not any(p.info[1] == port for p in asyncio.run(find_connections())):
            return port
    raise RuntimeError(f"Could not find free port after {MAX_PORT_ATTEMPTS} attempts")


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
    app = web.Application()
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    port_file = Path(os.environ.get("CLAUDE_PROXY_PORT_FILE", "/tmp/claude-proxy-port.txt"))
    port_file.write_text(str(port))
    port_file.touch()

    print(f"CLINE_PROXY_PORT={port}", flush=True)

    stop_event = asyncio.Event()

    def shutdown():
        if not stop_event.is_set():
            stop_event.set()

    loop = asyncio.get_event_loop()
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
