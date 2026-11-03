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
  unpair                           forget the local pairing
  gauge    [--once]                report tokens left + hottest CPU core every second
  statusline                       statusLine command: stores the session's rate limits

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
import subprocess
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
USAGE_PATH = os.path.join(CLAUDE_DIR, "semafor-usage.json")   # rate limits from the status line
GAUGE_LOCK = os.path.join(CLAUDE_DIR, "semafor-gauge.lock")   # held by the running gauge loop
GAUGE_LOG = os.path.join(CLAUDE_DIR, "semafor-gauge.log")
TIMEOUT = 2.5

# Gauge loop (HW4 needles): one report per GAUGE_PERIOD seconds. The first hook of
# the first Claude session starts it; from then on it runs until logout or reboot,
# whether or not any session is open. While the device does not answer it retries
# every GAUGE_RETRY seconds and logs only the changes between up and down.
GAUGE_PERIOD = 1.0
GAUGE_RETRY = 10.0
STATUSLINE_CMD = 'python3 "$HOME/.claude/semafor.py" statusline'

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


def device_post(cfg, path, fields, quiet=False):
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
    if last and not quiet:
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
    if state_word in ("idle", "working"):
        ensure_gauge_running()

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


# ------------------------------------------------------------------ gauges (HW4)
# Token gauge: what is left of the tighter of the 5-hour / 7-day rate-limit windows.
# Claude Code hands those to the statusLine command only (no hook carries them), so
# `semafor.py statusline` stores them in USAGE_PATH and the gauge loop reads that file.
# Temperature gauge: the hottest CPU core sensor (SMC), read without root.

def cmd_statusline():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    rl = data.get("rate_limits") or {}
    windows = {}
    for name in ("five_hour", "seven_day"):
        w = rl.get(name) or {}
        if isinstance(w.get("used_percentage"), (int, float)):
            windows[name] = {"used": float(w["used_percentage"]),
                             "resets_at": w.get("resets_at")}
    if windows:
        try:
            save_json(USAGE_PATH, {"ts": time.time(), "windows": windows})
        except OSError:
            pass
    left = tokens_left()
    parts = []
    if left is not None:
        parts.append("tokens %d%% left" % round(left))
    t = cpu_temp_max()
    if t is not None:
        parts.append("cpu %d°C" % round(t))
    print(" · ".join(parts) if parts else "semafor")
    return 0


def tokens_left():
    """Percent left of the tighter window, or None when no window is known.
    A window whose resets_at has passed counts as fully available again."""
    usage = load_json(USAGE_PATH, {})
    now = time.time()
    used = []
    for w in (usage.get("windows") or {}).values():
        try:
            u = float(w["used"])
        except (KeyError, TypeError, ValueError):
            continue
        r = w.get("resets_at")
        if isinstance(r, (int, float)) and r <= now:
            u = 0.0
        used.append(u)
    if not used:
        return None
    return max(0.0, min(100.0, 100.0 - max(used)))


class _SmcTemps:
    """Apple Silicon: the CPU core sensors in the SMC — keys "Tp0*"/"Tpx*" (P cores)
    and "Te0*" (E cores), the family Stats shows. Read through the AppleSMC user client
    (IOKit via ctypes): no root, no dependencies. The core keys bottom out at 40.0 °C
    when idle. Measured on an M4 Max: 40 °C idle, ~64 °C after 20 s on all cores."""

    def __init__(self):
        import ctypes
        import ctypes.util
        import struct
        self.ct, self.struct = ctypes, struct
        V, U8, U16, U32 = ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32
        io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        io.IOServiceMatching.restype = V
        io.IOServiceMatching.argtypes = [ctypes.c_char_p]
        io.IOServiceGetMatchingService.restype = U32
        io.IOServiceGetMatchingService.argtypes = [U32, V]
        io.IOServiceOpen.argtypes = [U32, U32, U32, ctypes.POINTER(U32)]
        io.IOConnectCallStructMethod.argtypes = [U32, U32, V, ctypes.c_size_t,
                                                 V, ctypes.POINTER(ctypes.c_size_t)]
        self.io = io

        class Vers(ctypes.Structure):
            _fields_ = [("b", U8 * 4), ("release", U16)]

        class PLimit(ctypes.Structure):
            _fields_ = [("version", U16), ("length", U16),
                        ("cpu", U32), ("gpu", U32), ("mem", U32)]

        class KeyInfo(ctypes.Structure):
            _fields_ = [("size", U32), ("type", U32), ("attr", U8)]

        class KeyData(ctypes.Structure):          # SMCKeyData_t, 80 bytes
            _fields_ = [("key", U32), ("vers", Vers), ("plimit", PLimit),
                        ("info", KeyInfo), ("result", U8), ("status", U8),
                        ("cmd", U8), ("data32", U32), ("bytes", U8 * 32)]

        if ctypes.sizeof(KeyData) != 80:
            raise OSError("SMCKeyData_t layout")
        self.KeyData = KeyData
        svc = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"AppleSMC"))
        if not svc:
            raise OSError("no AppleSMC")
        self.conn = U32()
        task = U32.in_dll(ctypes.CDLL(None), "mach_task_self_")
        if io.IOServiceOpen(svc, task, 0, ctypes.byref(self.conn)) != 0:
            raise OSError("AppleSMC open failed")

        flt = struct.unpack(">I", b"flt ")[0]
        count = self._read_info_and_value("#KEY")
        self.keys = []                            # (key code, size) of the core sensors
        for idx in range(int(count or 0)):
            d = KeyData()
            d.cmd, d.data32 = 8, idx              # kSMCGetKeyFromIndex
            out = self._call(d)
            if not out:
                continue
            name = struct.pack(">I", out.key)
            # per-core keys only: Tp0*/Tpx* (P cores), Te0* (E cores). Tp1*/Tp2* sit at
            # 45..53 °C when idle and look like cluster/substrate sensors.
            if name[:3] not in (b"Tp0", b"Tpx", b"Te0"):
                continue
            info = self._info(out.key)
            if info and info.type == flt and info.size == 4:
                self.keys.append(out.key)
        if not self.keys:
            raise OSError("no CPU core sensors in the SMC")

    def _call(self, d):
        ct = self.ct
        out = self.KeyData()
        n = ct.c_size_t(ct.sizeof(out))
        r = self.io.IOConnectCallStructMethod(self.conn.value, 2, ct.byref(d), ct.sizeof(d),
                                              ct.byref(out), ct.byref(n))
        return out if r == 0 and out.result == 0 else None

    def _info(self, code):
        d = self.KeyData()
        d.key, d.cmd = code, 9                    # kSMCGetKeyInfo
        out = self._call(d)
        return out.info if out else None

    def _read(self, code, size):
        d = self.KeyData()
        d.key, d.cmd = code, 5                    # kSMCReadKey
        d.info.size = size
        out = self._call(d)
        return bytes(out.bytes[:size]) if out else None

    def _read_info_and_value(self, name):         # "#KEY": ui32, big-endian
        code = self.struct.unpack(">I", name.encode())[0]
        info = self._info(code)
        raw = self._read(code, info.size) if info else None
        return self.struct.unpack(">I", raw)[0] if raw and len(raw) == 4 else None

    def read(self):
        hottest = None
        for code in self.keys:
            raw = self._read(code, 4)
            if not raw:
                continue
            t = self.struct.unpack("<f", raw)[0]
            if 0 < t < 150 and (hottest is None or t > hottest):
                hottest = t
        return hottest


class _MacTemps:
    """Fallback when the SMC gives no core sensors: the IOHID temperature sensors
    (usage page 0xff00, usage 5), "PMU tdie*". NOTE: on an M4 Max these are the PMU
    die, not the CPU cores — they barely move under load (+2 °C)."""

    def __init__(self):
        import ctypes
        import ctypes.util
        self.ct = ctypes
        V = ctypes.c_void_p
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
        cf.CFStringCreateWithCString.restype = V
        cf.CFStringCreateWithCString.argtypes = [V, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFNumberCreate.restype = V
        cf.CFNumberCreate.argtypes = [V, ctypes.c_int, V]
        cf.CFDictionaryCreate.restype = V
        cf.CFDictionaryCreate.argtypes = [V, V, V, ctypes.c_long, V, V]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetCount.argtypes = [V]
        cf.CFArrayGetValueAtIndex.restype = V
        cf.CFArrayGetValueAtIndex.argtypes = [V, ctypes.c_long]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [V, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        cf.CFRelease.argtypes = [V]
        io.IOHIDEventSystemClientCreate.restype = V
        io.IOHIDEventSystemClientCreate.argtypes = [V]
        io.IOHIDEventSystemClientSetMatching.argtypes = [V, V]
        io.IOHIDEventSystemClientCopyServices.restype = V
        io.IOHIDEventSystemClientCopyServices.argtypes = [V]
        io.IOHIDServiceClientCopyProperty.restype = V
        io.IOHIDServiceClientCopyProperty.argtypes = [V, V]
        io.IOHIDServiceClientCopyEvent.restype = V
        io.IOHIDServiceClientCopyEvent.argtypes = [V, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]
        io.IOHIDEventGetFloatValue.restype = ctypes.c_double
        io.IOHIDEventGetFloatValue.argtypes = [V, ctypes.c_int32]
        self.cf, self.io = cf, io
        utf8 = 0x08000100

        def cfstr(text):
            return cf.CFStringCreateWithCString(None, text.encode(), utf8)

        def cfnum(n):
            v = ctypes.c_int32(n)
            return cf.CFNumberCreate(None, 3, ctypes.byref(v))   # kCFNumberSInt32Type

        keys = (V * 2)(cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage"))
        vals = (V * 2)(cfnum(0xff00), cfnum(5))
        kcb = V.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        vcb = V.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        match = cf.CFDictionaryCreate(None, keys, vals, 2,
                                      ctypes.addressof(kcb), ctypes.addressof(vcb))
        self.client = io.IOHIDEventSystemClientCreate(None)
        io.IOHIDEventSystemClientSetMatching(self.client, match)
        self.services = io.IOHIDEventSystemClientCopyServices(self.client)
        if not self.services:
            raise OSError("no HID temperature services")
        prop = cfstr("Product")
        buf = ctypes.create_string_buffer(128)
        named = []
        for i in range(cf.CFArrayGetCount(self.services)):
            sc = cf.CFArrayGetValueAtIndex(self.services, i)
            name = io.IOHIDServiceClientCopyProperty(sc, prop)
            if not name:
                continue
            ok = cf.CFStringGetCString(name, buf, len(buf), utf8)
            cf.CFRelease(name)
            if ok:
                named.append((buf.value.decode(errors="replace"), sc))
        # The die sensors; on chips that name them otherwise, the CPU cluster sensors.
        self.cpu = [sc for n, sc in named if n.startswith("PMU tdie")] or \
                   [sc for n, sc in named if "ACC" in n or "CPU" in n.upper()]
        if not self.cpu:
            raise OSError("no CPU temperature sensors")

    def read(self):
        temp_event = 15                       # kIOHIDEventTypeTemperature
        hottest = None
        for sc in self.cpu:
            ev = self.io.IOHIDServiceClientCopyEvent(sc, temp_event, 0, 0)
            if not ev:
                continue
            t = self.io.IOHIDEventGetFloatValue(ev, temp_event << 16)
            self.cf.CFRelease(ev)
            if 0 < t < 150 and (hottest is None or t > hottest):
                hottest = t
        return hottest


def _linux_temp():
    """Linux: hwmon coretemp/k10temp/... (per core), else the thermal zones."""
    import glob
    best = None
    cpu_drivers = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal")
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            with open(os.path.join(hw, "name")) as f:
                if f.read().strip() not in cpu_drivers:
                    continue
        except OSError:
            continue
        for p in glob.glob(os.path.join(hw, "temp*_input")):
            try:
                with open(p) as f:
                    t = int(f.read()) / 1000.0
            except (OSError, ValueError):
                continue
            best = t if best is None else max(best, t)
    if best is None:
        for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            try:
                with open(p) as f:
                    t = int(f.read()) / 1000.0
            except (OSError, ValueError):
                continue
            best = t if best is None else max(best, t)
    return best


_temp_reader = None


def cpu_temp_max():
    """Hottest CPU core / die in °C, or None when this platform cannot tell."""
    global _temp_reader
    if _temp_reader is None:
        _temp_reader = False
        if sys.platform == "darwin":
            for reader in (_SmcTemps, _MacTemps):
                try:
                    _temp_reader = reader().read
                    break
                except Exception:  # noqa: BLE001  (Intel Mac, sandbox, ...)
                    _temp_reader = False
        elif sys.platform.startswith("linux"):
            _temp_reader = _linux_temp
    if not _temp_reader:
        return None
    try:
        return _temp_reader()
    except Exception:  # noqa: BLE001
        return None


def _gauge_fields():
    fields = {}
    left = tokens_left()
    if left is not None:
        fields["pct"] = "%.1f" % left
    t = cpu_temp_max()
    if t is not None:
        fields["temp"] = "%.1f" % max(-50.0, min(250.0, t))
    return fields


def _open_gauge_lock():
    """The lock file held for the loop's lifetime, or None if a loop already runs."""
    if fcntl is None:
        return open(GAUGE_LOCK, "w")
    os.makedirs(CLAUDE_DIR, exist_ok=True)
    lf = open(GAUGE_LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lf.close()
        return None
    return lf


def ensure_gauge_running():
    """From a hook: start the gauge loop in the background unless one already runs."""
    if fcntl is None:
        return                       # no single-instance guard -> run `gauge` by hand
    lf = _open_gauge_lock()
    if lf is None:
        return                       # already running
    lf.close()                       # free it for the child (a race just loses its lock)
    try:
        log = open(GAUGE_LOG, "a")
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "gauge"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    except OSError as e:
        print("gauge start failed: %s" % e, file=sys.stderr)


def cmd_gauge(args):
    cfg = load_json(CFG_PATH, None)
    if not cfg and args.once:
        print("not paired (run register)", file=sys.stderr)
        return 1
    cfg = ensure_device_ip(cfg) if cfg else {}      # the loop waits for `register`
    if args.once:
        fields = _gauge_fields()
        if not fields:
            print("nothing to report (no rate limits yet, no temperature sensor)")
            return 1
        r = device_post(cfg, "/gauge", fields)
        print("sent %s -> %s" % (fields, r[1] if r else "no answer"))
        return 0 if r else 1

    lock = _open_gauge_lock()
    if lock is None:
        print("a gauge loop is already running", file=sys.stderr)
        return 0

    def log(msg):
        print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)

    log("gauge loop started (pid %d)" % os.getpid())
    up = None                            # device answering? None = not tried yet
    next_tick = time.monotonic()
    try:
        while True:
            period = GAUGE_PERIOD
            cfg = load_json(CFG_PATH, cfg)   # follows a re-pairing or a new IP
            fields = _gauge_fields() if cfg.get("token") else {}
            if fields:
                ok = device_post(cfg, "/gauge", fields, quiet=True) is not None
                if ok != up:
                    log("device %s at %s" % ("up" if ok else "not answering",
                                             cfg.get("addr") or cfg.get("mdns")))
                    up = ok
                if not ok:
                    period = GAUGE_RETRY
            elif not cfg.get("token"):
                period = GAUGE_RETRY         # unpaired: wait for `register`
            next_tick += period
            delay = next_tick - time.monotonic()
            if delay < 0:                    # slow device or the Mac slept: no burst
                next_tick = time.monotonic()
                delay = 0
            time.sleep(delay)
    except KeyboardInterrupt:
        return 0
    finally:
        lock.close()


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
    for p in (CFG_PATH, MAP_PATH, SUBS_PATH, STATE_PATH, USAGE_PATH):
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

    # 3) the status line: the only place Claude Code exposes the rate limits
    #    (for the token gauge). Someone else's status line is left alone.
    sl = settings.get("statusLine")
    if not sl:
        settings["statusLine"] = {"type": "command", "command": STATUSLINE_CMD}
        print("statusLine set -> semafor.py statusline")
    elif "semafor.py" not in str(sl.get("command", "")):
        print("statusLine already set to %r — left alone. For the token gauge, pipe its "
              "stdin through: %s" % (sl.get("command"), STATUSLINE_CMD))
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

    t = sub.add_parser("test", help="cycle agent 0 through colors")
    t.add_argument("--host")
    t.set_defaults(func=cmd_test)

    u = sub.add_parser("unpair", help="forget local pairing")
    u.set_defaults(func=cmd_unpair)

    g = sub.add_parser("gauge", help="report tokens left + hottest CPU core every second")
    g.add_argument("--once", action="store_true", help="send one report and exit")
    g.set_defaults(func=cmd_gauge)
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
    if len(argv) == 1 and argv[0] == "statusline":
        return cmd_statusline()

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
