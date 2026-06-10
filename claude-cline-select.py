#!/usr/bin/env python3
import json, os, sys, tty, termios, select, time

PROVIDERS_FILE = os.path.expanduser("~/.cline/data/settings/providers.json")
DEFAULT = "cline"

GLOBAL_STATE_FILE = os.path.expanduser("~/.cline/data/globalState.json")

try:
    p = json.load(open(PROVIDERS_FILE))
    providers = p.get("providers", {})
    # Active provider: globalState → lastUsedProvider → default
    active_id = p.get("lastUsedProvider", DEFAULT)
    if os.path.exists(GLOBAL_STATE_FILE):
        try:
            gs = json.load(open(GLOBAL_STATE_FILE))
            mode = gs.get("mode", "act").lower()
            gs_pid = gs.get(f"{mode}ModeApiProvider", "")
            if gs_pid and gs_pid in providers:
                active_id = gs_pid
        except Exception:
            pass
except Exception:
    print(DEFAULT, end="")
    sys.exit(0)

pids = list(providers.keys())
if len(pids) <= 1 or not sys.stdin.isatty():
    print(pids[0] if pids else DEFAULT, end="")
    sys.exit(0)

GLOBAL_STATE_FILE = os.path.expanduser("~/.cline/data/globalState.json")

# Read globalState to get per-mode model overrides (set by IDE plugin)
gs_overrides = {}
if os.path.exists(GLOBAL_STATE_FILE):
    try:
        gs = json.load(open(GLOBAL_STATE_FILE))
        mode = gs.get("mode", "act").lower()
        key_suffix_map = {"cline": "Cline", "openai": "OpenAi", "openai-compatible": "OpenAi", "openrouter": "OpenRouter", "fireworks": "Fireworks"}
        for pid, pv in providers.items():
            ptype = pv.get("settings", {}).get("provider", "")
            k = f"{mode}Mode{key_suffix_map.get(ptype, ptype.title())}ModelId"
            v = gs.get(k)
            if v:
                gs_overrides[pid] = v
    except Exception:
        pass

try:
    idx = pids.index(active_id)
except ValueError:
    idx = 0

def model_label(pid):
    s = providers[pid].get("settings", {})
    m = gs_overrides.get(pid, s.get("model", "?"))
    return f"{pid}: {s.get('provider', '?')} / {m}"

def model_list():
    return " ".join(
        gs_overrides.get(pid, providers[pid]["settings"]["model"])
        for pid in pids
        if "model" in providers[pid].get("settings", {})
    )

N = 1 + len(pids)
NL = "\r\n"
sys.stderr.write(f"Select provider (\u2191\u2193 to move, Enter to confirm, auto in 5s):{NL}")
for i, pid in enumerate(pids):
    sys.stderr.write(f"  {'\u2192' if i == idx else ' '} {model_label(pid)}{NL}")
sys.stderr.flush()

start = time.time()
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)

try:
    while True:
        elapsed = time.time() - start
        if elapsed >= 5:
            break
        r, _, _ = select.select([sys.stdin], [], [], max(0, 5 - elapsed))
        if not r:
            break
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            nxt = sys.stdin.read(2)
            if nxt in ("[A", "[B"):
                idx = (idx - 1) % len(pids) if nxt == "[A" else (idx + 1) % len(pids)
                start = time.time()
                sys.stderr.write(f"\033[{N}A\033[J")
                sys.stderr.write(f"Select provider (\u2191\u2193 to move, Enter to confirm, auto in 5s):{NL}")
                for i, pid in enumerate(pids):
                    sys.stderr.write(f"  {'\u2192' if i == idx else ' '} {model_label(pid)}{NL}")
                sys.stderr.flush()
        elif ch in ("\r", "\n"):
            break
        elif ch == "\x03":
            raise KeyboardInterrupt
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stderr.write(f"\033[{N}A\033[J")
    ml = model_list()
    if ml:
        sys.stderr.write(f"Models: {ml}{NL}")
    sys.stderr.flush()

print(pids[idx], end="")
