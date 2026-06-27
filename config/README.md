# Config folder

This folder holds example local configuration files.

Tracked example:

```text
ollama_complexity.example.json
```

Suggested local copy:

```text
ollama_complexity.json
```

The local `ollama_complexity.json` file is intentionally ignored by Git so each machine can use its own Ollama context/output settings.

---

## Ollama complexity presets

`queries/ask_database.py` has built-in default complexity settings. You can optionally point it to a JSON config file with `OLLAMA_COMPLEXITY_CONFIG`.

PowerShell:

```powershell
Copy-Item config\ollama_complexity.example.json config\ollama_complexity.json
$env:OLLAMA_COMPLEXITY_CONFIG = "config\ollama_complexity.json"
```

Example `.env` entry:

```text
OLLAMA_COMPLEXITY_CONFIG=config/ollama_complexity.json
```

The config shape is:

```json
{
  "default": "standard",
  "options": {
    "standard": {
      "label": "Standard — faster (8K context, 1000-token output)",
      "description": "Best for normal lookups, filters, rankings, and most single-step questions.",
      "num_ctx": 8192,
      "num_predict": 1000,
      "timeout": 180
    },
    "complex": {
      "label": "Complex — larger prompt room (16K context, 1800-token output)",
      "description": "Use for multi-step calculations, multi-year comparisons, or more detailed SQL generation.",
      "num_ctx": 16384,
      "num_predict": 1800,
      "timeout": 240
    }
  }
}
```

Environment variables still override individual runtime values:

```text
OLLAMA_NUM_CTX
OLLAMA_NUM_PREDICT
OLLAMA_TIMEOUT
```
