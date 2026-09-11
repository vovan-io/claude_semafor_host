#!/usr/bin/env python3
"""Claude Semafor — host bridge & CLI.

Bridges the Claude Code hooks to the semaphore device over WiFi HTTP.
No dependencies — the Python 3 standard library only.

Commands:
  register [--name N] [--host H]   pair this computer (the device must be in PAIRING)
  install  [--host H]              copy itself to ~/.claude and add the hooks to settings.json
  status   [--host H]              show the state of every host and agent
  config   [--leds N --bright N --contrast N]   read or set the device settings
  test     [--host H]              cycle agent 0 through the colours (visual check)
  ota      [--fs] [--file F]       flash firmware (or the web UI) over WiFi, no USB
  calib    [--save F] [--restore F]  battery ADC calibration table
  unpair                           forget the local pairing

Event mode (invoked from the hooks; the event JSON is read from stdin):
  working | attention | done | idle | end
Maps session_id -> agent index 0..N (in ~/.claude/semafor-agents.json).
"""

import sys
import os
import json
import time
import socket
import argparse
import urllib.request
import urllib.parse
from contextlib import contextmanager

try:
    import fcntl  # POSIX file locking (macOS/Linux)
except ImportError:  # Windows — no locking (usually a single host anyway)
    fcntl = None

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CFG_PATH = os.path.join(CLAUDE_DIR, "semafor.json")           # {addr, host_id, token}
MAP_PATH = os.path.join(CLAUDE_DIR, "semafor-agents.json")    # session_id -> index
SUBS_PATH = os.path.join(CLAUDE_DIR, "semafor-subs.json")     # session_id -> subagent count
STATE_PATH = os.path.join(CLAUDE_DIR, "semafor-state.json")   # session_id -> last state
SETTINGS_PATH = os.path.join(CLAUDE_DIR, "settings.json")
INSTALL_PATH = os.path.join(CLAUDE_DIR, "semafor.py")
TIMEOUT = 2.5

# Hook event -> the state we send to the device.
STATE_ALIASES = {
    "working": "working",
    "attention": "attention",
    "done": "done",
    "idle": "done",       # SessionStart: the slot is taken, the agent is free
    "offline": "offline",
    "end": "offline",     # SessionEnd
}

# Special subagent events (they send no state, they update the counter -> /subs).
SUB_EVENTS = {"substart", "substop"}

# Manual limit events (for testing): `semafor.py limit` / `semafor.py limit-clear`.
LIMIT_EVENTS = {"limit", "limit-clear"}

# Special Notification event: attention, but we do NOT raise red after a turn has
# finished (otherwise a green "done" would start blinking red after a minute or so of
# idling). See cmd_notify.
NOTIFY_EVENTS = {"notify"}

# Substrings in the Notification hook text that mean "usage limit reached".
# (Claude Code has no dedicated limit hook — the signal arrives as a Notification.)
LIMIT_MARKERS = ("usage limit", "limit reached", "rate limit")

# Claude Code hooks -> event word (used by the install command). matcher: None or a string.
HOOK_EVENTS = [
    ("SessionStart", "idle", None),
    ("UserPromptSubmit", "working", None),
    ("PreToolUse", "working", ""),          # matcher "" = every tool
    ("PreToolUse", "substart", "Task"),     # a subagent starts (the Task tool)
    ("PreToolUse", "attention", "AskUserQuestion"),  # the agent is asking the user
    ("SubagentStop", "substop", None),      # a subagent finished
    ("Notification", "notify", None),       # attention/limit, but not after done (see cmd_notify)
    ("Stop", "done", None),
    ("SessionEnd", "end", None),
]


# ------------------------------------------------------------------ io helpers
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


@contextmanager
def file_lock(path):
    """Cross-process lock: concurrent hooks must not corrupt the session map."""
    if fcntl is None:
        yield
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lf = open(path + ".lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


# ------------------------------------------------------------------ http
def http_request(addr, path, fields=None, headers=None, method=None):
    url = "http://%s%s" % (addr, path)
    data = urllib.parse.urlencode(fields).encode() if fields is not None else None
    hdr = dict(headers or {})
    hdr.setdefault("Connection", "close")   # no keep-alive: the Arduino WebServer stalls otherwise
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode(errors="replace")


def resolve_to_ip(addr):
    """host[:port] -> ip[:port] (a single mDNS lookup). Returned unchanged on failure."""
    host = addr.split(":", 1)[0]
    port = addr[len(host):]        # ":port" or ""
    try:
        return socket.gethostbyname(host) + port
    except OSError:
        return addr


def ensure_device_ip(cfg):
    """Migrates an old config: addr=hostname -> IP (one mDNS lookup), falling back to mdns."""
    addr = cfg.get("addr", "")
    host = addr.split(":", 1)[0]
    try:
        socket.inet_aton(host)      # already an IP -> nothing to do
        return cfg
    except OSError:
        pass
    cfg.setdefault("mdns", addr)
    cfg["addr"] = resolve_to_ip(addr)
    save_json(CFG_PATH, cfg)
    return cfg


def device_post(cfg, path, fields):
    """POST to the device: the stored IP first, then the mDNS name on failure."""
    headers = {"X-Semafor-Token": str(cfg["token"]),
               "Content-Type": "application/x-www-form-urlencoded"}
    targets = []
    if cfg.get("addr"):
        targets.append(cfg["addr"])
    if cfg.get("mdns") and cfg["mdns"] not in targets:
        targets.append(cfg["mdns"])
    last = None
    for t in targets:
        try:
            return http_request(t, path, fields, headers)
        except Exception as e:  # noqa: BLE001
            last = e
    if last:
        print("post failed: %s" % last, file=sys.stderr)
    return None


def device_addr(args):
    cfg = load_json(CFG_PATH, {})
    return getattr(args, "host", None) or cfg.get("addr") or "semafor.local"


def device_token():
    return str(load_json(CFG_PATH, {}).get("token", ""))


# ------------------------------------------------------------------ register
def cmd_register(args):
    addr = getattr(args, "host", None) or "semafor.local"
    name = args.name or socket.gethostname().split(".")[0]
    try:
        status, body = http_request(addr, "/pair", {"name": name})
    except Exception as e:  # noqa: BLE001
        print("pair failed: %s\n(is the device really in PAIRING mode? long-press the button)" % e,
              file=sys.stderr)
        return 1
    if status != 200:
        print("pair rejected (%s): %s" % (status, body), file=sys.stderr)
        return 1
    info = json.loads(body)
    # Resolve the mDNS name to an IP once: events then go straight to the IP (no mDNS
    # per request), while the name stays as a fallback in case the IP changes.
    ip_addr = resolve_to_ip(addr)
    cfg = {"addr": ip_addr, "mdns": addr,
           "host_id": info["host_id"], "token": info["token"], "leds": 10}
    try:  # learn the LED count so we never assign agents beyond the strip
        _, cbody = http_request(ip_addr, "/config")
        cfg["leds"] = int(json.loads(cbody).get("leds", 10))
    except Exception:  # noqa: BLE001
        pass
    save_json(CFG_PATH, cfg)
    print("paired as host_id=%s (name=%s) via %s (mdns %s), leds=%d"
          % (info["host_id"], name, ip_addr, addr, cfg["leds"]))
    print("saved -> %s" % CFG_PATH)
    return 0


# ------------------------------------------------------------------ session -> index
_hook_payload = None


def _read_hook_payload():
    # stdin can only be read once — cache the whole hook JSON payload.
    global _hook_payload
    if _hook_payload is None:
        try:
            raw = sys.stdin.read()
            _hook_payload = json.loads(raw) if raw.strip() else {}
        except (ValueError, OSError):
            _hook_payload = {}
    return _hook_payload


def read_hook_stdin():
    return _read_hook_payload().get("session_id", "default")


def read_hook_message():
    return str(_read_hook_payload().get("message", ""))


def set_last_state(session_id, state):
    """Remember the last state of a session — needed so a Notification arriving after
    done/offline does not raise red (see cmd_notify)."""
    with file_lock(STATE_PATH):
        st = load_json(STATE_PATH, {})
        if state == "offline":
            st.pop(session_id, None)
        else:
            st[session_id] = state
        save_json(STATE_PATH, st)


def get_last_state(session_id):
    with file_lock(STATE_PATH):
        return load_json(STATE_PATH, {}).get(session_id)


def resolve_index(session_id, release=False, max_agents=10):
    with file_lock(MAP_PATH):
        mp = load_json(MAP_PATH, {})
        if release:
            idx = mp.pop(session_id, None)
            if idx is not None:
                save_json(MAP_PATH, mp)
            return idx
        if session_id in mp:
            return mp[session_id]
        used = set(mp.values())
        for i in range(max_agents):
            if i not in used:
                mp[session_id] = i
                save_json(MAP_PATH, mp)
                return i
        mp[session_id] = 0   # overflow — reuse slot 0
        save_json(MAP_PATH, mp)
        return 0


# ------------------------------------------------------------------ subagents
def post_subs(cfg):
    # A mask of WHICH agents (LEDs) currently have an active subagent, not just a
    # count: the device cross-fades that specific agent's LED yellow<->white.
    with file_lock(SUBS_PATH):
        subs = load_json(SUBS_PATH, {})
    with file_lock(MAP_PATH):
        agent_map = load_json(MAP_PATH, {})
    mask = 0
    for session_id, count in subs.items():
        if count <= 0:
            continue
        idx = agent_map.get(session_id)
        if idx is not None:
            mask |= (1 << idx)
    device_post(cfg, "/subs", {"mask": "%x" % mask})


def update_subs(cfg, session_id, delta=0, reset=False):
    with file_lock(SUBS_PATH):
        subs = load_json(SUBS_PATH, {})
        if reset:
            subs.pop(session_id, None)
        else:
            v = subs.get(session_id, 0) + delta
            if v > 0:
                subs[session_id] = v
            else:
                subs.pop(session_id, None)
        save_json(SUBS_PATH, subs)
    post_subs(cfg)


def cmd_subevent(word):
    cfg = load_json(CFG_PATH, None)
    if not cfg:
        return 0
    cfg = ensure_device_ip(cfg)
    session_id = read_hook_stdin()
    update_subs(cfg, session_id, delta=(+1 if word == "substart" else -1))
    return 0


def _post_limit(cfg, on):
    # best-effort: a hook must not fail just because the device is unreachable.
    try:
        device_post(cfg, "/limit", {"on": "1" if on else "0"})
    except Exception as e:
        print("limit post failed: %s" % e, file=sys.stderr)


def cmd_limit(on):
    cfg = load_json(CFG_PATH, None)
    if not cfg:
        return 0
    cfg = ensure_device_ip(cfg)
    _post_limit(cfg, on)
    return 0


def cmd_event(state_word):
    state = STATE_ALIASES.get(state_word)
    if state is None:
        print("unknown state: %s" % state_word, file=sys.stderr)
        return 2

    cfg = load_json(CFG_PATH, None)
    if not cfg:
        print("not paired yet (run: semafor.py register); skipping", file=sys.stderr)
        return 0  # a hook must never fail
    cfg = ensure_device_ip(cfg)

    # A new session -> clear any pending limit alert.
    if state_word == "idle":
        _post_limit(cfg, False)

    session_id = read_hook_stdin()
    idx = resolve_index(session_id, release=(state == "offline"),
                        max_agents=int(cfg.get("leds", 10)))
    set_last_state(session_id, state)
    # end of turn/session — clear the session's subagents (guards against leaks)
    if state_word in ("done", "end"):
        update_subs(cfg, session_id, reset=True)
    if idx is None:
        return 0

    device_post(cfg, "/agent/%d" % idx, {"state": state})
    return 0


def cmd_notify():
    """The Notification hook. Two cases:
    1) the text contains a limit marker -> put the device into "limit" mode;
    2) otherwise it is attention ("waiting for input" / a permission prompt). BUT after
       a turn has finished (last state == done/offline/None) Claude Code sends
       "waiting for input" anyway — red is not wanted there, so green stays. Red is
       only raised for a request made while working (last state == working)."""
    cfg = load_json(CFG_PATH, None)
    if not cfg:
        return 0
    cfg = ensure_device_ip(cfg)

    # A Notification mentioning the limit -> "limit" mode (outranks attention).
    msg = read_hook_message().lower()
    if any(m in msg for m in LIMIT_MARKERS):
        _post_limit(cfg, True)
        return 0

    session_id = read_hook_stdin()
    if get_last_state(session_id) in ("done", "offline", None):
        return 0
    idx = resolve_index(session_id, max_agents=int(cfg.get("leds", 10)))
    if idx is None:
        return 0
    set_last_state(session_id, "attention")
    device_post(cfg, "/agent/%d" % idx, {"state": "attention"})
    return 0


# ------------------------------------------------------------------ status
def cmd_status(args):
    addr = device_addr(args)
    try:
        _, body = http_request(addr, "/status")
    except Exception as e:  # noqa: BLE001
        print("status failed: %s" % e, file=sys.stderr)
        return 1
    data = json.loads(body)
    glyph = {"working": "W", "attention": "!", "done": "o", "offline": "."}
    print("device %s  (active host: %s)" % (addr, data.get("active")))
    for h in data.get("hosts", []):
        row = "".join(glyph.get(a["s"], "?") for a in h["agents"])
        print("  [%d] %-12s %s" % (h["id"], h["name"], row))
    if not data.get("hosts"):
        print("  (no paired hosts)")
    return 0


# ------------------------------------------------------------------ ota
DEFAULT_FW = ".pio/build/esp32-c3-devkitm-1/firmware.bin"
DEFAULT_FS = ".pio/build/esp32-c3-devkitm-1/littlefs.bin"


def cmd_ota(args):
    """Push a firmware (or filesystem) image to the device over HTTP.

    Built by hand rather than with a library so the script keeps its
    no-dependencies promise. The device streams the body straight into the
    inactive OTA slot, so even a 1.3MB image never has to fit in its RAM.
    """
    cfg = load_json(CFG_PATH, {})
    if not cfg.get("token"):
        print("not paired: run `semafor.py register` first", file=sys.stderr)
        return 1
    addr = device_addr(args)

    path = args.file or (DEFAULT_FS if args.fs else DEFAULT_FW)

    # PlatformIO's custom upload_command is shared by the `upload` and `uploadfs`
    # targets and only varies $SOURCE, so infer the image kind from its name.
    # An explicit --fs still wins.
    is_fs = args.fs or os.path.basename(path).startswith(("littlefs", "spiffs"))

    if not os.path.isfile(path):
        print("image not found: %s\n(build it first: pio run%s)"
              % (path, " -t buildfs" if is_fs else ""), file=sys.stderr)
        return 1
    with open(path, "rb") as f:
        image = f.read()

    boundary = "----semafor%d" % int(time.time())
    head = ("--%s\r\n"
            'Content-Disposition: form-data; name="image"; filename="%s"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            % (boundary, os.path.basename(path))).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    body = head + image + tail

    url = "http://%s/update%s" % (addr, "?type=fs" if is_fs else "")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "X-Semafor-Token": str(cfg["token"]),
        "Content-Type": "multipart/form-data; boundary=%s" % boundary,
        "Content-Length": str(len(body)),
        "Connection": "close",
    })

    print("uploading %s %s (%.1f KB) -> %s"
          % ("filesystem" if is_fs else "firmware", os.path.basename(path),
             len(image) / 1024.0, addr))
    try:
        # Generous timeout: erasing the target partition alone takes several seconds.
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(resp.read().decode(errors="replace"))
    except Exception as e:  # noqa: BLE001
        print("ota failed: %s" % e, file=sys.stderr)
        return 1
    print("device is rebooting into the new image; it will be back in a few seconds")
    return 0


# ------------------------------------------------------------------ config
def cmd_config(args):
    addr = device_addr(args)
    fields = {}
    if args.leds is not None:
        fields["leds"] = args.leds
    if args.bright is not None:
        fields["bright"] = args.bright
    if args.contrast is not None:
        fields["contrast"] = args.contrast
    try:
        if fields:
            _, body = http_request(addr, "/config", fields, method="POST")
        else:
            _, body = http_request(addr, "/config")
    except Exception as e:  # noqa: BLE001
        print("config failed: %s" % e, file=sys.stderr)
        return 1
    print(body)
    return 0


# ------------------------------------------------------------------ calibration
# The calibration file lives in fs/, which is what `pio run -t uploadfs` ships to
# the device. Keeping one copy means --save updates the file that gets flashed.
DEFAULT_CAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "fs", "calibration.json")


CAL_NOTE = ("Per-unit battery ADC calibration. Shipped by `pio run -t uploadfs`, loaded at "
            "boot, rewritten by the device when a row is edited in calib.html. "
            "Do NOT copy to another board. See README, 'Calibrating the battery gauge'.")


def cmd_calib(args):
    """Read, save or restore the battery ADC calibration table.

    The table lives in the device's NVS and survives reflashing, but not an
    erase_flash or a swap to another unit -- hence --save/--restore. Measuring it
    takes a bench supply and half an hour, so it is worth keeping in a file.
    """
    addr = device_addr(args)

    if args.restore:
        data = load_json(args.restore, None)
        if not data:
            print("cannot read %s" % args.restore, file=sys.stderr)
            return 1
        points = data.get("points") or []
        if not points:
            print("no points in %s" % args.restore, file=sys.stderr)
            return 1
        try:
            # Wipe first: restoring onto a non-empty table would merge the two.
            http_request(addr, "/calib", {"clear": 1}, method="POST")
            for raw, real in points:
                http_request(addr, "/calib", {"raw": raw, "real": real}, method="POST")
            cutoff = data.get("protection_cutoff_mv")
            if cutoff:
                http_request(addr, "/calib", {"cutoff": cutoff}, method="POST")
        except Exception as e:  # noqa: BLE001
            print("restore failed: %s" % e, file=sys.stderr)
            return 1
        print("restored %d points%s -> %s"
              % (len(points), " + cutoff %dmV" % cutoff if cutoff else "", addr))

    try:
        _, body = http_request(addr, "/calib")
    except Exception as e:  # noqa: BLE001
        print("calib failed: %s" % e, file=sys.stderr)
        return 1
    data = json.loads(body)

    if args.save:
        # Written in the same shape and order the firmware itself writes, so a
        # --save right after a device-side edit produces no spurious diff.
        with open(args.save, "w") as f:
            f.write("{\n")
            f.write('  "_note": %s,\n' % json.dumps(CAL_NOTE))
            f.write('  "protection_cutoff_mv": %d,\n' % data.get("cutoff", 0))
            f.write('  "points": [\n')
            pts = data.get("points", [])
            for i, pt in enumerate(pts):
                f.write("    [%d, %d]%s\n"
                        % (pt["raw"], pt["real"], "," if i + 1 < len(pts) else ""))
            f.write("  ]\n}\n")
        print("saved %d points -> %s" % (len(data.get("points", [])), args.save))

    print("now: raw %dmV -> %dmV (%d%%), charger %s"
          % (data.get("raw", 0), data.get("mv", 0), data.get("pct", 0), data.get("chg", "?")))
    print("protection cutoff %dmV, shutdown at %dmV"
          % (data.get("cutoff", 0), data.get("critical", 0)))
    print("  %-8s %-8s %s" % ("real", "raw", "correction"))
    for pt in data.get("points", []):
        print("  %-8d %-8d %+d" % (pt["real"], pt["raw"], pt["real"] - pt["raw"]))
    if not data.get("points"):
        print("  (table empty -- the gauge falls back to the DONE self-calibration)")
    return 0


# ------------------------------------------------------------------ test
def cmd_test(args):
    addr = device_addr(args)
    token = device_token()
    if not token:
        print("not paired (run register)", file=sys.stderr)
        return 1
    headers = {"X-Semafor-Token": token,
               "Content-Type": "application/x-www-form-urlencoded"}
    seq = ["working", "attention", "done", "offline"]
    for s in seq:
        try:
            http_request(addr, "/agent/0", {"state": s, "label": "test"}, headers)
            print("agent 0 -> %s" % s)
        except Exception as e:  # noqa: BLE001
            print("test failed: %s" % e, file=sys.stderr)
            return 1
        time.sleep(1.2)
    return 0


# ------------------------------------------------------------------ unpair
def cmd_unpair(_args):
    for p in (CFG_PATH, MAP_PATH, SUBS_PATH, STATE_PATH):
        try:
            os.remove(p)
            print("removed %s" % p)
        except OSError:
            pass
    return 0


# ------------------------------------------------------------------ install
def _hook_entry(state, matcher):
    cmd = 'python3 "$HOME/.claude/semafor.py" %s' % state
    entry = {"hooks": [{"type": "command", "command": cmd}]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _has_command(groups, cmd):
    # Dedup by exact command: one event can carry several of our hooks
    # (e.g. PreToolUse: working + substart).
    for g in groups:
        for hk in g.get("hooks", []):
            if hk.get("command") == cmd:
                return True
    return False


def cmd_install(args):
    # 1) copy ourselves to ~/.claude/semafor.py
    src = os.path.abspath(__file__)
    if src != INSTALL_PATH:
        os.makedirs(CLAUDE_DIR, exist_ok=True)
        with open(src) as f:
            data = f.read()
        with open(INSTALL_PATH, "w") as f:
            f.write(data)
        os.chmod(INSTALL_PATH, 0o755)
        print("copied -> %s" % INSTALL_PATH)

    # 2) add the hooks to settings.json (with a backup, without duplicates)
    settings = load_json(SETTINGS_PATH, {})
    if os.path.exists(SETTINGS_PATH):
        save_json(SETTINGS_PATH + ".bak", settings)
    hooks = settings.setdefault("hooks", {})

    # 2a) migration: drop OUR obsolete hooks that are no longer in HOOK_EVENTS
    # (e.g. the old Notification -> attention). Other people's hooks are left alone.
    desired = {}
    for event, state, matcher in HOOK_EVENTS:
        cmd = _hook_entry(state, matcher)["hooks"][0]["command"]
        desired.setdefault(event, set()).add(cmd)
    removed = 0
    for event, groups in list(hooks.items()):
        kept = []
        for g in groups:
            hks = g.get("hooks", [])
            ours = [hk for hk in hks if "semafor.py" in (hk.get("command") or "")]
            if len(hks) == 1 and ours and hks[0].get("command") not in desired.get(event, set()):
                removed += 1
                continue
            kept.append(g)
        hooks[event] = kept

    added = 0
    for event, state, matcher in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        entry = _hook_entry(state, matcher)
        if _has_command(groups, entry["hooks"][0]["command"]):
            continue
        groups.append(entry)
        added += 1
    save_json(SETTINGS_PATH, settings)
    print("hooks installed into %s (added %d, removed %d stale, backup .bak)"
          % (SETTINGS_PATH, added, removed))
    if not load_json(CFG_PATH, None):
        print("next: put device in PAIRING (long press) and run: semafor.py register")
    return 0


# ------------------------------------------------------------------ main
def build_parser():
    p = argparse.ArgumentParser(prog="semafor.py", description="Claude Semafor host bridge & CLI")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("register", help="pair this host (device must be in PAIRING)")
    r.add_argument("--name")
    r.add_argument("--host", default="semafor.local")
    r.set_defaults(func=cmd_register)

    i = sub.add_parser("install", help="copy script + add hooks to settings.json")
    i.set_defaults(func=cmd_install)

    s = sub.add_parser("status", help="show hosts/agents")
    s.add_argument("--host")
    s.set_defaults(func=cmd_status)

    c = sub.add_parser("config", help="get/set device settings")
    c.add_argument("--host")
    c.add_argument("--leds", type=int)
    c.add_argument("--bright", type=int)
    c.add_argument("--contrast", type=int)
    c.set_defaults(func=cmd_config)

    cal = sub.add_parser("calib", help="read/save/restore the battery calibration table")
    cal.add_argument("--host")
    cal.add_argument("--save", nargs="?", const=DEFAULT_CAL, metavar="FILE",
                     help="write the device's table to FILE (default: fs/calibration.json)")
    cal.add_argument("--restore", nargs="?", const=DEFAULT_CAL, metavar="FILE",
                     help="upload a table from FILE, replacing what is on the device")
    cal.set_defaults(func=cmd_calib)

    t = sub.add_parser("test", help="cycle agent 0 through colors")
    t.add_argument("--host")
    t.set_defaults(func=cmd_test)

    o = sub.add_parser("ota", help="flash firmware over WiFi (no USB cable)")
    o.add_argument("--host")
    o.add_argument("--file", help="image to send (default: the PlatformIO build output)")
    o.add_argument("--fs", action="store_true",
                   help="send the LittleFS image (web UI) instead of the firmware")
    o.set_defaults(func=cmd_ota)

    u = sub.add_parser("unpair", help="forget local pairing")
    u.set_defaults(func=cmd_unpair)
    return p


def main(argv):
    # Event shortcut used by the hooks: `semafor.py working` / `semafor.py substart` etc.
    if len(argv) == 1 and argv[0] in STATE_ALIASES:
        return cmd_event(argv[0])
    if len(argv) == 1 and argv[0] in SUB_EVENTS:
        return cmd_subevent(argv[0])
    if len(argv) == 1 and argv[0] in NOTIFY_EVENTS:
        return cmd_notify()
    if len(argv) == 1 and argv[0] in LIMIT_EVENTS:
        return cmd_limit(argv[0] == "limit")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
