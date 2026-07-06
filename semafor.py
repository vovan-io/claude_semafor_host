#!/usr/bin/env python3
"""Claude Semafor — host bridge & CLI.

Связывает хуки Claude Code с устройством-семафором по WiFi HTTP.
Зависимостей нет — только стандартная библиотека Python 3.

Команды:
  register [--name N] [--host H]   спарить компьютер (устройство должно быть в PAIRING)
  install  [--host H]              скопировать себя в ~/.claude и вписать хуки в settings.json
  status   [--host H]              показать состояние всех хостов/агентов
  config   [--leds N --bright N --contrast N]   прочитать/задать настройки устройства
  test     [--host H]              прогнать агента 0 по цветам (визуальная проверка)
  unpair                           забыть локальную привязку

Событийный режим (из хуков; JSON события читается со stdin):
  working | attention | done | idle | end
Маппит session_id -> индекс агента 0..N (файл ~/.claude/semafor-agents.json).
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
except ImportError:  # Windows — без блокировок (обычно один хост)
    fcntl = None

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CFG_PATH = os.path.join(CLAUDE_DIR, "semafor.json")           # {addr, host_id, token}
MAP_PATH = os.path.join(CLAUDE_DIR, "semafor-agents.json")    # session_id -> index
SUBS_PATH = os.path.join(CLAUDE_DIR, "semafor-subs.json")     # session_id -> subagent count
SETTINGS_PATH = os.path.join(CLAUDE_DIR, "settings.json")
INSTALL_PATH = os.path.join(CLAUDE_DIR, "semafor.py")
TIMEOUT = 2.5

# Событие хука -> состояние, которое шлём устройству.
STATE_ALIASES = {
    "working": "working",
    "attention": "attention",
    "done": "done",
    "idle": "done",       # SessionStart: слот занят, агент свободен
    "offline": "offline",
    "end": "offline",     # SessionEnd
}

# Спец-события субагентов (не шлют state, обновляют счётчик -> /subs).
SUB_EVENTS = {"substart", "substop"}

# Хуки Claude Code -> слово-событие (для команды install). matcher: None или строка.
HOOK_EVENTS = [
    ("SessionStart", "idle", None),
    ("UserPromptSubmit", "working", None),
    ("PreToolUse", "working", ""),          # matcher "" = все инструменты
    ("PreToolUse", "substart", "Task"),     # запуск субагента (инструмент Task)
    ("SubagentStop", "substop", None),      # субагент завершился
    ("Notification", "attention", None),
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
    """Межпроцессная блокировка: параллельные хуки не портят карту сессий."""
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
    hdr.setdefault("Connection", "close")   # без keep-alive: Arduino WebServer иначе тормозит ответ
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.status, resp.read().decode(errors="replace")


def resolve_to_ip(addr):
    """host[:port] -> ip[:port] (один mDNS-резолв). При ошибке — как есть."""
    host = addr.split(":", 1)[0]
    port = addr[len(host):]        # ":port" или ""
    try:
        return socket.gethostbyname(host) + port
    except OSError:
        return addr


def ensure_device_ip(cfg):
    """Миграция старого конфига: addr=hostname -> IP (mDNS один раз), фолбэк в mdns."""
    addr = cfg.get("addr", "")
    host = addr.split(":", 1)[0]
    try:
        socket.inet_aton(host)      # уже IP -> ничего не делаем
        return cfg
    except OSError:
        pass
    cfg.setdefault("mdns", addr)
    cfg["addr"] = resolve_to_ip(addr)
    save_json(CFG_PATH, cfg)
    return cfg


def device_post(cfg, path, fields):
    """POST на устройство: сначала сохранённый IP, при ошибке — mDNS-имя."""
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
        print("pair failed: %s\n(устройство точно в режиме PAIRING? длинное нажатие кнопки)" % e,
              file=sys.stderr)
        return 1
    if status != 200:
        print("pair rejected (%s): %s" % (status, body), file=sys.stderr)
        return 1
    info = json.loads(body)
    # Резолвим mDNS-имя в IP один раз: события потом бьют по IP (без mDNS на
    # каждый запрос), а имя остаётся запасным адресом на случай смены IP.
    ip_addr = resolve_to_ip(addr)
    cfg = {"addr": ip_addr, "mdns": addr,
           "host_id": info["host_id"], "token": info["token"], "leds": 10}
    try:  # узнаём число диодов, чтобы не назначать агентов сверх ленты
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
def read_hook_stdin():
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    return data.get("session_id", "default")


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
        mp[session_id] = 0   # переполнение — переиспользуем 0
        save_json(MAP_PATH, mp)
        return 0


# ------------------------------------------------------------------ субагенты
def post_subs(cfg):
    with file_lock(SUBS_PATH):
        total = sum(load_json(SUBS_PATH, {}).values())
    device_post(cfg, "/subs", {"n": total})


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


def cmd_event(state_word):
    state = STATE_ALIASES.get(state_word)
    if state is None:
        print("unknown state: %s" % state_word, file=sys.stderr)
        return 2

    cfg = load_json(CFG_PATH, None)
    if not cfg:
        print("not paired yet (run: semafor.py register); skipping", file=sys.stderr)
        return 0  # хук не должен падать
    cfg = ensure_device_ip(cfg)

    session_id = read_hook_stdin()
    idx = resolve_index(session_id, release=(state == "offline"),
                        max_agents=int(cfg.get("leds", 10)))
    # конец хода/сессии — сбрасываем субагентов сессии (страховка от «утечки»)
    if state_word in ("done", "end"):
        update_subs(cfg, session_id, reset=True)
    if idx is None:
        return 0

    device_post(cfg, "/agent/%d" % idx, {"state": state})
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
        print("  (нет спаренных хостов)")
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
    for p in (CFG_PATH, MAP_PATH, SUBS_PATH):
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
    # Дедуп по точной команде: под одним событием может быть несколько наших
    # хуков (напр. PreToolUse: working + substart).
    for g in groups:
        for hk in g.get("hooks", []):
            if hk.get("command") == cmd:
                return True
    return False


def cmd_install(args):
    # 1) скопировать себя в ~/.claude/semafor.py
    src = os.path.abspath(__file__)
    if src != INSTALL_PATH:
        os.makedirs(CLAUDE_DIR, exist_ok=True)
        with open(src) as f:
            data = f.read()
        with open(INSTALL_PATH, "w") as f:
            f.write(data)
        os.chmod(INSTALL_PATH, 0o755)
        print("copied -> %s" % INSTALL_PATH)

    # 2) вписать хуки в settings.json (с бэкапом, без дублей)
    settings = load_json(SETTINGS_PATH, {})
    if os.path.exists(SETTINGS_PATH):
        save_json(SETTINGS_PATH + ".bak", settings)
    hooks = settings.setdefault("hooks", {})
    added = 0
    for event, state, matcher in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        entry = _hook_entry(state, matcher)
        if _has_command(groups, entry["hooks"][0]["command"]):
            continue
        groups.append(entry)
        added += 1
    save_json(SETTINGS_PATH, settings)
    print("hooks installed into %s (added %d, backup .bak)" % (SETTINGS_PATH, added))
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
    return p


def main(argv):
    # Событийный шорткат для хуков: `semafor.py working` / `semafor.py substart` и т.п.
    if len(argv) == 1 and argv[0] in STATE_ALIASES:
        return cmd_event(argv[0])
    if len(argv) == 1 and argv[0] in SUB_EVENTS:
        return cmd_subevent(argv[0])

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
