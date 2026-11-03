# Claude Semafor — host side

The bridge between the Claude Code hooks and the semaphore device. Hooks are
short-lived processes, so the link is WiFi HTTP: on each event `semafor.py` POSTs
to the device (`semafor.local`). No dependencies — the Python 3 standard library
only (`curl` is not required).

The device firmware, OTA updates and battery calibration tooling live in the
separate firmware repository (`claude_semafor`).

## How it works

```
Claude Code session
  ├─ UserPromptSubmit / PreToolUse → semafor.py working   → 🟡
  ├─ Notification                  → semafor.py attention → 🔴
  ├─ Stop                          → semafor.py done      → 🟢
  ├─ SessionStart                  → semafor.py idle
  └─ SessionEnd                    → semafor.py end (frees the LED)
             │ POST http://semafor.local/agent/{idx}  (X-Semafor-Token)
             ▼
        ESP32-C3  →  LED[idx] of the active host
```

The `session_id` from the hook is mapped to an LED index `0..N-1` (stored in
`~/.claude/semafor-agents.json`, accessed under a file lock so concurrent
sessions do not clash). One computer is one "host" on the device; its sessions
occupy the LEDs of the strip while that host is active.

## Install in three steps

```sh
# 1) copy the script into ~/.claude and add the hooks to settings.json (idempotent;
#    existing settings are preserved and a settings.json.bak is created)
python3 semafor.py install

# 2) long-press the button on the device to enter PAIRING (a timer appears on the OLED), then:
python3 ~/.claude/semafor.py register            # optionally --name "macbook" --host 192.168.1.108

# 3) start a Claude Code session — the LED will run through 🟡→🔴→🟢
```

`register` also asks the device for its LED count and stores it, so sessions are
never assigned beyond the length of the strip. Everything lives in
`~/.claude/semafor.json`.

## CLI commands

```sh
semafor.py status                    # state of every host and agent
semafor.py config                    # show the device settings
semafor.py config --leds 8           # set the LED count (persisted to NVS)
semafor.py config --bright 128 --contrast 100
semafor.py test                      # cycle agent 0 through the colours (strip check)
semafor.py unpair                    # forget the local pairing
semafor.py gauge [--once]            # report tokens left + CPU temperature (HW4)
```

Any command can be aimed at a specific address with `--host 192.168.1.108`.

## Gauges (HW4)

On a board with the two needles the bridge reports, once a second:

- **tokens** — what is left of your usage limit, `100 - used %` of the tighter of
  the 5-hour and 7-day windows (a window past its `resets_at` counts as 100 % left);
- **temp** — the hottest CPU core in °C: on Apple Silicon the SMC core sensors
  `Tp0*`/`Tpx*` (P cores) and `Te0*` (E cores), the ones Stats shows, read through the AppleSMC
  user client (IOKit via `ctypes`, no root); they bottom out at 40 °C when idle. On
  Linux `coretemp`/`k10temp` hwmon or the thermal zones. Intel Macs and Windows report
  no temperature.

Claude Code exposes the limits only to the status line, so `install` sets
`statusLine` to `semafor.py statusline` (it stores them in
`~/.claude/semafor-usage.json` and prints `tokens 62% left · cpu 41°C`). The limits
are there for claude.ai Pro/Max subscribers only, after the session's first reply;
until then only the temperature is sent. If you already have a status line,
`install` leaves it alone; pipe its stdin into `semafor.py statusline` as well.

The report loop, `semafor.py gauge`, is started by the `SessionStart` /
`UserPromptSubmit` / `PreToolUse` hooks (one instance, guarded by
`~/.claude/semafor-gauge.lock`; log in `~/.claude/semafor-gauge.log`). It exits
60 s after the last session has ended and drops both values, so the needles fall back
to rest. A board without the gauges answers `present:false` and ignores the values.

```sh
semafor.py gauge --once              # one report, prints what the device stored
semafor.py gauge                     # run in the foreground, never exits on idle
```

## Several computers

Run `install` + `register` from each computer (each takes its own host slot, up
to `MAX_HOSTS`). A short press on the device switches which host's agents the
strip shows; if an inactive host has a 🔴 agent, a `!` appears on the OLED.

## First-time WiFi setup

If the device cannot see a stored network for more than 30s it raises the access
point `Semafor-XXXX` with a captive portal (`192.168.4.1`) — connect to it and
enter the SSID and password. The credentials are stored in NVS.

## Manual check without hooks

```sh
TOKEN=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/semafor.json')))['token'])")
curl -H "X-Semafor-Token: $TOKEN" -X POST http://semafor.local/agent/0 -d state=working
curl http://semafor.local/status
```

## License

MIT — see [LICENSE](LICENSE).
