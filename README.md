# lms-tools

Набор скриптов и утилит для LM Studio (Windows).

## Скрипты

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
