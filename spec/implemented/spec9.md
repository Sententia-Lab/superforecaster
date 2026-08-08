# Spec 9 — The key panel

**Status: implemented** (August 2026). ADR 61.

## Why

Four secrets decide whether a run can happen, and the app exposes one of them.

The `Admin token` button is a `window.prompt` writing `localStorage`. The LLM key, the
Tavily key, and the Wikipedia key are settable only by editing `backend/.env` and
restarting the server. A user whose Tavily key expired sees `no web search` in the header,
and the app offers no way to fix it.

**Outcome:** one `Keys` panel in the header, replacing the `Admin token` button, holding
all four keys with a clear status for each.

## The four keys

| Panel field | Where the value lives | Backing name |
|---|---|---|
| Admin token | Browser `localStorage`, sent as `Authorization: Bearer` | compared server-side against `ADMIN_API_KEY`, set in `backend/.env` |
| LLM API key | Server process | `ANTHROPIC_API_KEY` |
| Tavily API key | Server process | `TAVILY_API_KEY` |
| Wikipedia API key | Server process | `WIKIPEDIA_API_KEY` |

The Wikipedia key does not exist yet. `tools.search_wikipedia` calls
`https://en.wikipedia.org/w/api.php` anonymously, and its docstring says "No API key
required" — which is true. Wikimedia issues optional access tokens that raise rate limits,
so the field is **optional**: set, the request carries `Authorization: Bearer <key>`;
unset, behaviour is what it is today. The docstring changes to say the key is optional
rather than absent.

## Why the keys live in the process

`config.get_settings()` re-reads `os.environ` on every call and caches nothing.
`resolve_agent_model()` does the same. Setting `os.environ["TAVILY_API_KEY"]` at runtime is
therefore picked up by the next request, with no cache to invalidate and no restart.

Stated plainly, because each of these is a real cost:

- A key set through the panel is **server-wide**. Every run on that server spends it,
  whoever started it. This suits a personal instance, not a shared deploy.
- Keys are **in-process only**. A restart drops them. `backend/.env` stays the way to make
  a key permanent, and nothing writes a secret to disk.
- The endpoint is **write-only**. No route returns a key value, ever.

## Backend

### `config.py`

```python
RUNTIME_KEYS = frozenset({"ANTHROPIC_API_KEY", "TAVILY_API_KEY", "WIKIPEDIA_API_KEY"})

def set_runtime_key(name: str, value: str) -> None:
    """Set or clear one allowlisted key for the life of this process."""
```

The allowlist is the point. Without it the endpoint writes any name into `os.environ`, and
`DATABASE_PATH` and `FRONTEND_DIR` are both in there.

`origin(name)` gains a fourth answer. It returns `environment` / `.env` / `unset` today; a
runtime-set key would report `.env`, which is false. It now checks the runtime set first
and returns `session`.

`Settings` gains `wikipedia_api_key: str | None`.

### `api/main.py`

```python
class KeyUpdate(BaseModel):
    anthropic_api_key: str | None = None    # None = leave alone
    tavily_api_key: str | None = None       # ""   = clear
    wikipedia_api_key: str | None = None    # value = set

@app.put("/config/keys", tags=["health"])
def set_keys(body: KeyUpdate, _: None = Depends(require_admin)) -> dict[str, object]
```

`GET /config` gains a `keys` object of origin strings — never values. `PUT /config/keys`
returns the same shape, so the panel redraws from the response with no follow-up GET, the
pattern `editStepPayload` already uses. `require_admin` is skipped in local mode, matching
every other admin route.

### `tools.py`

`_search_wikipedia` sends `Authorization: Bearer <key>` when
`get_settings().wikipedia_api_key` is set, and no header otherwise.

## Data lineage — setting the Tavily key

```
Keys panel  ->  PUT /config/keys
```

```json
{"tavily_api_key": "tvly-abc123"}
```

```
  -> require_admin(request)          [Bearer vs ADMIN_API_KEY; skipped in local mode]
  -> config.set_runtime_key("TAVILY_API_KEY", "tvly-abc123")
       os.environ["TAVILY_API_KEY"] = "tvly-abc123"     [writes: process environment]
  -> 200
```

```json
{"auth_required": true,
 "search_enabled": true,
 "model": "anthropic:claude-sonnet-4-6",
 "keys": {"llm": "environment", "tavily": "session", "wikipedia": "unset"}}
```

```
  -> App.jsx setConfig(resp)     ->  the `no web search` chip disappears
  -> next cell: tools._tavily_search -> get_settings().tavily_api_key   (re-read, no cache)
```

Nothing is written to disk at any step.

## Frontend

`components/KeyPanel.jsx` is a modal reusing the existing `.modal-backdrop` / `.modal` /
`.modal-actions` styles, with `EditorField` for the rows.

```
┌ Keys ──────────────────────────────────────────────┐
│ Admin token            [set in this browser]       │
│ [                                    ]             │
│ Sent as a bearer token. The server compares it     │
│ against ADMIN_API_KEY in backend/.env.             │
│                                                    │
│ LLM API key            [from environment]          │
│ [                                    ]             │
│ ANTHROPIC_API_KEY                                  │
│                                                    │
│ Tavily API key         [set this session]          │
│ [                                    ]             │
│ TAVILY_API_KEY — web search. Without it the agents │
│ fall back to Wikipedia alone.                      │
│                                                    │
│ Wikipedia API key      [unset]                     │
│ [                                    ]             │
│ WIKIPEDIA_API_KEY — optional, raises rate limits.  │
│                                                    │
│              [ Cancel ]  [ Save ]                  │
└────────────────────────────────────────────────────┘
```

- Inputs are `type="password"` and **always render empty**. The server never sends a value
  back, so there is nothing to prefill. The chip carries the state.
- A field left blank is omitted from the request. A field whose chip is not `unset` gets a
  `Clear` link that sends `""`.
- The admin token goes through the existing `getToken` / `setToken` and never enters the
  request body.
- Chip tones: `environment` and `.env` green, `session` yellow, `unset` plain.
- `Save` is disabled while a run is streaming. Changing the LLM key mid-run would apply to
  the next agent call inside that same run.

`App.jsx` drops `onAdminToken` and the `window.prompt`. The header button becomes `Keys`
and is **always shown** — the current one renders only when `config.auth_required`, which
hides it exactly where a local user wants to paste an LLM key.

## Tests

- `set_runtime_key` rejects a name outside `RUNTIME_KEYS`.
- `origin()` returns `session` after a runtime set, and `environment` for a preset variable.
- `PUT /config/keys` returns 401 with no admin token when not in local mode.
- No response body from `/config` or `/config/keys` contains a key value.
