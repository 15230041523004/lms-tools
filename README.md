# lms-tools

Набор скриптов и утилит для LM Studio (Windows).

## Скрипты

### `install-comet-mcp.ps1`

Установщик **Perplexity Comet MCP** для LM Studio.

> ⚠️ Это облачное решение (Perplexity), не полностью локальное.

**Что делает:**
- Проверяет / устанавливает Node.js (через winget или MSI)
- Устанавливает пакет `perplexity-comet-mcp`
- Создаёт готовый фрагмент `mcp.json`

**Запуск:**

```powershell
# PowerShell (желательно от администратора при первой установке Node.js)
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\install-comet-mcp.ps1
```

**После установки:**
1. Скачай браузер [Comet](https://www.perplexity.ai/comet)
2. В LM Studio → Program → Install → Edit `mcp.json` вставь:

```json
{
  "mcpServers": {
    "comet-bridge": {
      "command": "perplexity-comet-mcp"
    }
  }
}
```

3. Включи сервер `comet-bridge` и используй модель с tool calling.
