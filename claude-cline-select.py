#!/usr/bin/env python3
import json, os, sys, tty, termios, select, time

PROVIDERS_FILE = os.path.expanduser("~/.cline/data/settings/providers.json")
DEFAULT = "cline"

try:
    p = json.load(open(PROVIDERS_FILE))
    active_id = p.get("lastUsedProvider", DEFAULT)
    providers = p.get("providers", {})
except Exception:
    print(DEFAULT, end="")
    sys.exit(0)

pids = list(providers.keys())
if len(pids) <= 1 or not sys.stdin.isatty():
    print(pids[0] if pids else DEFAULT, end="")
    sys.exit(0)

try:
    idx = pids.index(active_id)
except ValueError:
    idx = 0

def fmt(pid):
    s = providers[pid].get("settings", {})
    return f"{pid}: {s.get('provider', '?')} / {s.get('model', '?')}"

N = 1 + len(pids)
sys.stderr.write(f"Select provider (\u2191\u2193 to move, Enter to confirm, auto in 5s):\n")
for i, pid in enumerate(pids):
    sys.stderr.write(f"  {'\u2192' if i == idx else ' '} {fmt(pid)}\n")
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
                sys.stderr.write(f"Select provider (\u2191\u2193 to move, Enter to confirm, auto in 5s):\n")
                for i, pid in enumerate(pids):
                    sys.stderr.write(f"  {'\u2192' if i == idx else ' '} {fmt(pid)}\n")
                sys.stderr.flush()
        elif ch in ("\r", "\n"):
            break
        elif ch == "\x03":
            raise KeyboardInterrupt
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    sys.stderr.write(f"\033[{N}A\033[J")
    sys.stderr.flush()

print(pids[idx], end="")
