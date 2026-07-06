# Claude Semafor — host side

Мост между хуками Claude Code и устройством-семафором. Хуки — короткоживущие
процессы, поэтому связь по WiFi HTTP: `semafor.py` при событии делает POST на
устройство (`semafor.local`). Зависимостей нет — только стандартная библиотека
Python 3 (`curl` не нужен).

## Как это работает

```
Claude Code session
  ├─ UserPromptSubmit / PreToolUse → semafor.py working   → 🟡
  ├─ Notification                  → semafor.py attention → 🔴
  ├─ Stop                          → semafor.py done      → 🟢
  ├─ SessionStart                  → semafor.py idle
  └─ SessionEnd                    → semafor.py end (освобождает диод)
             │ POST http://semafor.local/agent/{idx}  (X-Semafor-Token)
             ▼
        ESP32-C3  →  LED[idx] активного хоста
```

`session_id` из хука маппится на индекс диода `0..N-1` (файл
`~/.claude/semafor-agents.json`, доступ под файловой блокировкой — параллельные
сессии не мешают друг другу). Один компьютер = один «хост» на устройстве; его
сессии занимают диоды ленты активного хоста.

## Установка за 3 шага

```sh
# 1) скопировать скрипт в ~/.claude и вписать хуки в settings.json (идемпотентно,
#    существующие настройки сохраняются, создаётся settings.json.bak)
python3 host/semafor.py install

# 2) на устройстве длинным нажатием кнопки войти в PAIRING (на OLED — таймер), затем:
python3 ~/.claude/semafor.py register            # --name "macbook" --host 192.168.1.108 при желании

# 3) запустить сессию Claude Code — диод пройдёт цикл 🟡→🔴→🟢
```

`register` заодно узнаёт у устройства число диодов и сохраняет его, чтобы не
назначать сессии сверх длины ленты. Всё лежит в `~/.claude/semafor.json`.

## Команды CLI

```sh
semafor.py status                    # состояние всех хостов/агентов
semafor.py config                    # показать настройки устройства
semafor.py config --leds 8           # задать число диодов (сохранится в NVS)
semafor.py config --bright 128 --contrast 100
semafor.py test                      # прогнать агента 0 по цветам (проверка ленты)
semafor.py unpair                    # забыть локальную привязку
```

Любую команду можно направить на конкретный адрес: `--host 192.168.1.108`.

## Несколько компьютеров

Повторите `install` + `register` с каждого компьютера (каждый занимает свой слот
хоста, до `MAX_HOSTS`). На устройстве коротким нажатием кнопки переключайте, чьи
агенты показывает лента; если у неактивного хоста есть 🔴-агент — на OLED «!».

## Первичная настройка WiFi

Если устройство не видит сохранённую сеть дольше 30 c, оно поднимает точку
доступа `Semafor-XXXX` с captive-порталом (`192.168.4.1`) — подключитесь и
введите SSID/пароль. Настройки сохраняются в NVS.

## Ручная проверка без хуков

```sh
TOKEN=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/semafor.json')))['token'])")
curl -H "X-Semafor-Token: $TOKEN" -X POST http://semafor.local/agent/0 -d state=working
curl http://semafor.local/status
```
