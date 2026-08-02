# Ollama configuration

Ask Database has built-in query-complexity presets. A machine can override them
with a local JSON file without changing tracked source code.

Create the ignored local copy:

```powershell
Copy-Item config/ollama_complexity.example.json config/ollama_complexity.json
```

Point `.env` at it:

```text
OLLAMA_COMPLEXITY_CONFIG=config/ollama_complexity.json
```

The tracked example has this shape:

```json
{
  "default": "standard",
  "options": {
    "standard": {
      "label": "Standard - faster",
      "description": "Best for normal lookups, filters, rankings, and most single-step questions.",
      "num_ctx": 8192,
      "num_predict": 1000,
      "timeout": 180
    },
    "complex": {
      "label": "Complex - larger prompt room",
      "description": "Best for multi-step calculations and multi-year comparisons.",
      "num_ctx": 16384,
      "num_predict": 1800,
      "timeout": 240
    }
  }
}
```

Process-level values override the selected preset:

```text
OLLAMA_NUM_CTX
OLLAMA_NUM_PREDICT
OLLAMA_TIMEOUT
```

Endpoint and model settings belong in `.env`, normally:

```text
OLLAMA_ENDPOINTS=http://localhost:11434/api/chat
OLLAMA_MODEL=qwen3.5:9b
```

`OLLAMA_ENDPOINTS` may contain a comma-separated fallback list. The separate
grant-adjudication CLI uses `OLLAMA_URL`; set both when the same Ollama server
supports the web app and grant workflows.
