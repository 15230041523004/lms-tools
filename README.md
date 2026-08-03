# lms-tools

Скрипты для локальных LLM на Windows: **TextGen (oobabooga) + Comet MCP из коробки**.

## Скрипт

| Файл | Назначение |
|------|------------|
| [`textgen-8060s.ps1`](textgen-8060s.ps1) | TextGen: установка / запуск / удаление + **Comet MCP** + **llama_log_viewer** |
| [`llama_log_viewer/script.py`](llama_log_viewer/script.py) | Расширение TextGen: вкладка полного консольного лога |
| [`_cmd_flags_codec.py`](_cmd_flags_codec.py) | Python `shlex` codec для merge `CMD_FLAGS.txt` |

Отдельный установщик Comet MCP для LM Studio **не нужен** и удалён: при работе через TextGen всё делает `textgen-8060s.ps1`.

---

### `textgen-8060s.ps1`

Один PowerShell-скрипт: **portable TextGen** + автоматический **Comet MCP** (stdio bridge + браузер Comet с CDP).

**Первый запуск** = установка всего недостающего (TextGen zip, Node, bridge, Comet при необходимости) + hardlink моделей LM Studio + старт.  
**Повторные запуски** = быстрый путь: bridge уже собран, порты освобождаются, `mcp.json` merge, Comet CDP, TextGen.

**Что делает:**
- Скачивает portable zip с GitHub Releases (`windows-rocm` или `windows-vulkan`)
- Ставит в `C:\textgen-8060s` (если нет прав записи → `%LOCALAPPDATA%\textgen-8060s`)
- Пишет `CMD_FLAGS`: UI на LAN + **OpenAI-compatible API**  
  `--listen --listen-host 0.0.0.0 --api --api-port 5000 --api-key sk-local`
- Открывает firewall на портах UI (7860) и API (5000)
- **По умолчанию** делает hardlink (или symlink) всех `*.gguf` из `%USERPROFILE%\.lmstudio\models` → `user_data\models`
- Создаёт **`user_data\characters\Assistant.json`** (нужен для `/v1/chat/completions` и Cline)
- Ставит расширение **`llama_log_viewer`** → вкладка **Llama Logs** в UI
  (полный stdout/stderr процесса TextGen вместе с выводом llama-server)
- Собирает MCP bridge (`perplexity-comet-mcp`) в `%LOCALAPPDATA%\comet-mcp-fixed`
- Пишет/мержит `user_data\mcp.json` с **stdio** сервером `comet-bridge`
- Запускает Comet с профилем `Comet-MCP-Profile` и CDP на **9223**
- Удаление с меню (включая MCP-only)

**Быстрый старт:**

```powershell
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\textgen-8060s.ps1
```

**Параметры:**

| Ключ | Действие |
|------|----------|
| (по умолчанию) | install + MCP + **link LM Studio models** + run |
| `-Backend ROCm` / `Vulkan` | portable backend |
| `-Reinstall` | перекачать app |
| `-NoRun` | только установить, не стартовать UI |
| `-SkipMcp` | без Node/Comet/bridge |
| `-SkipModelLinks` | **не** линковать GGUF из LM Studio |
| `-LinkModels` | пересоздать ссылки (если файл уже есть — заменить) |
| `-SkipDefaultCharacter` | **не** создавать `characters\Assistant.json` |
| `-DisableLlamaLogViewer` | **не** ставить вкладку логов: удаляет deploy из `user_data\extensions` и убирает из `--extensions` |
| `-ApiPort` / `-ApiKey` / `-ListenPort` | API и UI |
| `-Uninstall` | меню удаления |
| `-InstallRoot путь` | каталог установки |

**Повторный запуск:** останавливает предыдущий TextGen и освобождает **7860** / **5000**.

**После установки:**
- UI: `http://127.0.0.1:7860`
- API: `http://127.0.0.1:5000/v1` · key `sk-local`
- Модели: `…\user_data\models` (ссылки на LM Studio GGUF)
- Character: `…\user_data\characters\Assistant.json` (для chat API)
- MCP stdio: `…\user_data\mcp.json`
- Comet CDP: `http://127.0.0.1:9223/json/version`
- Console logs tab: UI → **Llama Logs** · текущий файл `logs\textgen-YYYYMMDD-HHMMSS.log`

### Вкладка Llama Logs (`llama_log_viewer` v1.7)

Показывает вывод, начинающийся со строки TextGen `INFO Starting TextGen`: сообщения
родительского TextGen, весь stderr llama-server и строки после загрузки модели
(`Loaded`, `LOADER`, `CONTEXT LENGTH`). Вывод launcher/MCP setup и служебная шапка
PowerShell transcript в файл не попадают.

**Зачем:** в portable TextGen + ROCm удобно смотреть полный консольный лог (загрузка
модели, ошибки llama-server, OOM) прямо во вкладке UI, без отдельного окна PowerShell
и без ручного «Обновить».

**Польза:**
- Живой хвост лога во время старта/инференса (diff с диска, не перечитывает весь файл)
- Long-poll (~12 с удержания, если файл не растёт) — UI не «дубеет» от кликов каждую секунду
- Автоскролл, подгрузка старых кусков при скролле вверх, снятие ANSI
- Footer «Обновлено» / «Запись в файл» без мигания (статичный HTML + клиентские метки)
- Область лога на высоту вкладки, footer у низа окна

| | |
|--|--|
| Как включается | `textgen-8060s.ps1` tees stdout/stderr `textgen.bat` в файл и в консоль; extension **только читает** файл |
| Когда появляется файл | сразу при старте `textgen.bat`, до первой строки `INFO Starting TextGen` |
| Жизненный цикл | каждый запуск → новый `logs\textgen-YYYYMMDD-HHMMSS.log`; `log_path.txt` указывает вкладке на текущий |
| Вывод | Один tee: TextGen parent + llama-server + `Loaded` / `LOADER` / `CONTEXT LENGTH` |
| UI | **long-poll** через hidden `#llama-log-poll` (`queue=False`); JS boot: HTML `img.onload` + `eval(atob(...))` (TextGen **не** вызывает extension `custom_js()`); **Автоскролл**; footer; layout fill; ANSI strip |
| Deploy | `user_data\extensions\llama_log_viewer\script.py` + sidecar `log_path.txt` |
| Отключить | `-DisableLlamaLogViewer` |

Native `LLAMA_ARG_LOG_FILE` / `--log-file` и `Start-Transcript` намеренно не используются: первый не создавал файл в конкретной ROCm-сборке, а второй не захватывал реальный вывод дочернего `textgen.bat`.

**CMD_FLAGS / Python:**
- **Bootstrap** (пусто/нет файла/managed-шаблон) — **без Python**. В логе: `CMD_FLAGS engine: v3-bootstrap`.
- **Merge** кастомного файла — только **system** `py -3` / `python` (timeout 60s).
  **`portable_env` / ROCm Python для codec запрещён** (из‑за зависаний).
- Нет system Python при кастомном `CMD_FLAGS` → rewrite managed-шаблона (с предупреждением), установка не зависает.
- Если в логе всё ещё `CMD_FLAGS Python: ...\portable_env\...` — запускается **старая** копия скрипта; берите `textgen-8060s.ps1` из этого репо.

### Fix 500: missing character `Assistant`

TextGen при `POST /v1/chat/completions` по умолчанию грузит персонажа **`Assistant`**.  
Если нет файла `user_data\characters\Assistant.json`, в логе:

```text
Could not find the character "Assistant" … ValueError → HTTP 500
```

Cline при этом часто показывает `UND_ERR_SOCKET` / terminated.

**Скрипт создаёт `Assistant.json` автоматически** при каждом запуске (если файла ещё нет).  
Существующий файл **не** перезаписывается. Отключить: `-SkipDefaultCharacter`.

Уже установленный TextGen: просто перезапустите `textgen-8060s.ps1` (или `-NoRun`) — файл появится.

### MCP в UI TextGen (важно)

Поле **«MCP servers»** в sidebar чата — **только для HTTP** URL (по одному на строку).

**Comet настроен через stdio** в файле `user_data\mcp.json`.  
**Оставьте HTTP-бокс пустым** — это нормально. Скрипт пишет `mcp.json` сам.

Чтобы tools Comet появились:

1. В логе старта TextGen должно быть, что найден `mcp.json`
2. Режим **Instruct** или **Chat-Instruct** (в чистом Chat tools часто скрыты)
3. Модель **с tool calling**
4. В sidebar включить tools (чекбоксы), когда MCP-сервер подключится
5. В отдельном окне Comet (MCP-профиль) при первом разе — логин Perplexity

### Cline / n8n (LAN)

| Поле | Значение |
|------|----------|
| Base URL (этот ПК) | `http://127.0.0.1:5000/v1` |
| Base URL (LAN) | `http://<LAN-IP>:5000/v1` |
| API Key | `sk-local` |
| Model ID | из `GET /v1/models` (модель **загружена** в UI) |

Нужны: работающий API, **`Assistant.json`**, загруженная модель.

> ROCm проблемы на 8060S: `-Backend Vulkan -Reinstall`.
