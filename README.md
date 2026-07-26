# lms-tools

Набор скриптов и утилит для локальных LLM на Windows (LM Studio, TextGen и связанное).

## Скрипты

| Файл | Назначение |
|------|------------|
| [`install-comet-mcp.ps1`](install-comet-mcp.ps1) | Perplexity Comet MCP для LM Studio (облачный поиск) |
| [`textgen-8060s.ps1`](textgen-8060s.ps1) | TextGen (oobabooga): установка / запуск / удаление, AMD Radeon 8060S |

---

### `textgen-8060s.ps1`

Один PowerShell-скрипт для **portable TextGen** (не conda one-click — на Windows AMD/ROCm через conda не ставится).

**Что делает:**
- Скачивает portable zip с GitHub Releases (`windows-rocm` или `windows-vulkan`)
- Ставит в `C:\textgen-8060s`
- Пишет `CMD_FLAGS` (`--listen 0.0.0.0`, API, llama.cpp, gpu-layers -1)
- Показывает **все** LAN IP (Wi‑Fi, Ethernet, vEthernet, …)
- Запускает UI
- Удаление с меню (оставить модели / снести всё)

**Быстрый старт:**

```powershell
# из cmd / PowerShell (нужна обычная консоль, не ISE)
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\textgen-8060s.ps1
```

Или одной строкой с raw GitHub:

```powershell
irm https://raw.githubusercontent.com/15230041523004/lms-tools/main/textgen-8060s.ps1 -OutFile textgen-8060s.ps1
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\textgen-8060s.ps1
```

**Параметры:**

| Ключ | Действие |
|------|----------|
| (по умолчанию) | install при необходимости + run, backend спросить (Enter = ROCm) |
| `-Backend ROCm` | portable ROCm |
| `-Backend Vulkan` | portable Vulkan |
| `-Reinstall` | перекачать app |
| `-NoRun` | только установить |
| `-Uninstall` | меню удаления |
| `-InstallRoot путь` | другой каталог (по умолчанию `C:\textgen-8060s`) |

**Удаление (`-Uninstall`):**

1. EVERYTHING — app + user_data + models + logs + firewall  
2. APP ONLY — движок, **модели оставить**  
3. APP + LOGS — модели оставить  
4. CANCEL  

После выбора нужно ввести **`YES`**.

**После установки:**
- UI: `http://127.0.0.1:7860`
- API: `http://127.0.0.1:5000/v1`
- Модели (GGUF): `C:\textgen-8060s\user_data\models`
- Флаги: `C:\textgen-8060s\user_data\CMD_FLAGS.txt`

> На Radeon 8060S при проблемах с ROCm: `-Backend Vulkan -Reinstall`.

---

### `install-comet-mcp.ps1`

Установщик **Perplexity Comet MCP** для LM Studio.

> ⚠️ Это облачное решение (Perplexity), не полностью локальное.

**Что делает:**
- Проверяет / устанавливает Node.js (через winget или MSI)
- Устанавливает пакет `perplexity-comet-mcp`
- Создаёт готовый фрагмент `mcp.json` с **полным путём** к `npx.cmd` и `Path` (LM Studio на Windows иначе не видит node/npx)

**Быстрая установка (одной строкой):**

```powershell
irm https://raw.githubusercontent.com/15230041523004/lms-tools/main/install-comet-mcp.ps1 | iex
```

Если политика выполнения блокирует:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; irm https://raw.githubusercontent.com/15230041523004/lms-tools/main/install-comet-mcp.ps1 | iex
```

**Локальный запуск:**

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\install-comet-mcp.ps1
```

**После установки:**
1. Скачай браузер [Comet](https://www.perplexity.ai/comet)
2. В LM Studio → Program → Install → Edit `mcp.json` вставь конфиг из вывода скрипта (или вручную):

```json
{
  "mcpServers": {
    "comet-bridge": {
      "command": "C:\\Program Files\\nodejs\\npx.cmd",
      "args": ["-y", "perplexity-comet-mcp"],
      "env": {
        "Path": "C:\\Program Files\\nodejs;C:\\Windows\\System32"
      }
    }
  }
}
```

3. Сохрани → **Restart** у `mcp/comet-bridge` (красный треугольник должен пропасть)
4. Используй модель с **tool calling** + system prompt, который требует реальные tool calls (не текстовую симуляцию)
