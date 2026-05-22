# Assignment-6: Autonomous MCP Agent — Architecture Analysis

---

## 1. Big Picture — What This System Is

This project is an **Autonomous Research Agent framework** built from scratch (no LangChain/LangGraph). It is composed of **two independently runnable subsystems** that work together:

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ASSIGNMENT-6 SYSTEM                          │
│                                                                     │
│   ┌──────────────────┐          ┌──────────────────────────────┐   │
│   │   mcp_server.py  │◄────────►│      llm_gatewayV3/          │   │
│   │  (Tool Server)   │  stdio   │  (LLM Orchestration Layer)   │   │
│   └──────────────────┘          └──────────────────────────────┘   │
│                                                                     │
│   The agent loop (to be built) sits on top of both                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component 1 — `mcp_server.py` (MCP Tool Server)

### What It Does
An MCP (Model Context Protocol) server that exposes **9 real-world tools** over **stdio transport** so an LLM agent can call them.

### Transport
```
Agent (Python) ──stdio JSON-RPC──► mcp_server.py ──► real APIs / filesystem
```
Runs as: `python mcp_server.py`

### Tools Exposed

| Tool | Purpose | Notes |
|------|---------|-------|
| `web_search(query, max_results=5)` | Search the web | Tavily primary → DuckDuckGo fallback; hard-capped at 5 results |
| `fetch_url(url, timeout=20)` | Scrape a URL to clean markdown | Uses `crawl4ai` + headless Chromium |
| `get_time(timezone="UTC")` | Current time in any IANA timezone | Returns ISO + human-readable |
| `currency_convert(amount, from, to)` | Live FX conversion | Calls `frankfurter.dev` API |
| `read_file(path)` | Read file from sandbox | UTF-8 only |
| `list_dir(path)` | List sandbox directory | Returns name/type/size |
| `create_file(path, content)` | Create new file | Errors if already exists |
| `update_file(path, content)` | Overwrite existing file | Errors if not found |
| `edit_file(path, find, replace)` | Find-and-replace in file | `replace_all` flag |

### Key Design Decisions

- **Sandbox isolation**: All file tools are sandboxed to `./sandbox/` via `_safe()` — path traversal attacks (`../../etc`) are rejected.
- **Usage metering**: `usage.json` tracks Tavily API calls with a soft monthly cap of 950/1000 — prevents accidental quota burn.
- **crawl4ai stdout fix**: The server does a low-level `os.dup2(2,1)` trick to suppress crawl4ai's Rich output from corrupting the MCP stdio JSON-RPC stream.

---

## 3. Component 2 — `llm_gatewayV3/` (LLM Orchestration Gateway)

### What It Does
A **FastAPI HTTP server** that acts as a unified LLM API router. Your agent sends a single `POST /v1/chat` and the gateway handles provider selection, failover, rate limiting, caching, and structured output validation.

Runs on: **port 8101** (`python main.py` or `./run.sh`)

### Architecture Overview

```
                         POST /v1/chat
                              │
                    ┌─────────▼──────────┐
                    │   FastAPI  main.py  │
                    └─────────┬──────────┘
                              │
              ┌───────────────▼────────────────┐
              │         auto_route?             │
              │  YES → Router Pool (cheap LLM)  │
              │  NO  → Use provider order list   │
              └───────────────┬────────────────┘
                              │
              ┌───────────────▼────────────────┐
              │   Tier Classification:          │
              │   TINY / LARGE / HUGE           │
              └───────────────┬────────────────┘
                              │
              ┌───────────────▼────────────────┐
              │    Worker Pool (Router.pick)    │
              │  ─ Rate/quota/cooldown checks   │
              │  ─ Capability matching          │
              │  ─ Context window checks        │
              └───────────────┬────────────────┘
                              │
              ┌───────────────▼────────────────┐
              │   Provider Adapter (providers.py│
              │   Gemini / Groq / Ollama / ...  │
              └───────────────┬────────────────┘
                              │
              ┌───────────────▼────────────────┐
              │   Response Validation           │
              │   (jsonschema + Pydantic retry) │
              └───────────────┬────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │    ChatResponse     │
                    └────────────────────┘
```

---

## 4. The 2-Tier Routing System (V3's Key Innovation)

This is the most sophisticated part of the codebase.

### Tier 1 — Router Pool (cheap, fast LLMs)
Classifies each request into `TINY | LARGE | HUGE` using a lightweight LLM call.

```
Default Router Order: cerebras → groq → nvidia → github
```

Router prompt:
```
Given a token_count and content sample, output exactly one of: TINY, LARGE, or HUGE.
- TINY:  < 1000 tokens, simple content
- LARGE: 1000–8000 tokens, OR dense content
- HUGE:  > 8000 tokens  →  rejected (503)
```

Sanity clamp: If the router LLM hallucinates `HUGE` on a small input, it's overridden to `LARGE`.

### Tier 2 — Worker Pool (actual LLM doing the work)
Picks best available provider based on the tier:

| Tier | Preferred Worker Order |
|------|----------------------|
| TINY | github → openrouter → groq → nvidia → cerebras → gemini → ollama |
| LARGE | gemini → groq → nvidia → cerebras → github → openrouter → ollama |

---

## 5. Provider Adapters (`providers.py`)

All 7 providers inherit from `BaseProvider` with a unified interface:

```python
async def chat(messages, *, max_tokens, temperature, model, tools, 
               tool_choice, reasoning, response_format, system_blocks) -> dict
```

| Provider | Base Class | Special Feature |
|----------|-----------|----------------|
| `GeminiProvider` | `BaseProvider` | Context caching, native thinking/reasoning |
| `GroqProvider` | `OpenAICompatProvider` | Reasoning support |
| `CerebrasProvider` | `OpenAICompatProvider` | Fast inference |
| `NvidiaProvider` | `OpenAICompatProvider` | Large context |
| `OpenRouterProvider` | `OpenAICompatProvider` | Model marketplace |
| `GitHubProvider` | `OpenAICompatProvider` | GitHub Models |
| `OllamaProvider` | `BaseProvider` | Local models; prompted tool fallback |

**Normalized output** — every adapter returns the same dict shape regardless of the provider's native API format.

### Gemini Context Caching
`cache.py` (`GeminiCache`) avoids re-sending large system prompts on every call. The system prompt is uploaded once, and subsequent calls reference the cache key — saving tokens and latency.

---

## 6. Rate Limiting & Failover (`router.py`)

`RateState` tracks per-provider:
- **RPM** (requests per minute) — sliding window
- **RPD** (requests per day) — daily counter with midnight reset
- **TPM** (tokens per minute) — sliding window  
- **Daily token cap** — hard daily budget for Cerebras
- **Cooldown** — minimum seconds between calls
- **Backoff** — exponential backoff on errors (429→60s, 5xx→20s, auth→600s)

`Router.pick()` iterates through candidates and returns the first one that passes all checks. If all fail → 503 with full attempt log.

---

## 7. Schemas & Contracts (`schemas.py`)

All data flows through **Pydantic v2** models:

```
ChatRequest ──► gateway ──► ChatResponse
                              └── RouterDecision (if auto_route used)
                              └── ToolCall[] (if tools used)
```

Key fields on `ChatRequest`:
- `auto_route: "perception"|"memory"|"decision"` — which cognitive layer is calling (maps to tier-selection)
- `tools: list[ToolDef]` — tool definitions passed through to the LLM
- `response_format: ResponseFormat` — enforce JSON schema output
- `reasoning: "off"|"low"|"medium"|"high"` — thinking budget

---

## 8. Database & Dashboard (`db.py` + `static/`)

### SQLite logging (`gateway_v3.db`)
Every LLM call is logged:

```sql
calls(provider, model, input_tokens, output_tokens, latency_ms, 
      status, error, prompt_chars, response_chars, tool_calls,
      reasoning_applied, call_role, router_decision, timestamp)
```

`call_role` distinguishes: `worker` | `router_perception` | `router_memory` | `router_decision`

### Dashboard (`static/dashboard.html`)
Live monitoring UI served at `GET /` showing:
- Provider health (RPM/RPD/TPM usage)
- Recent call history
- Router vs. worker call breakdown
- Token usage graphs

### Help page (`static/help.html`)
API reference for the gateway endpoints.

---

## 9. REST API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat` | Main inference endpoint |
| `GET` | `/v1/providers` | List wired providers + shortcuts |
| `GET` | `/v1/capabilities` | Per-provider capability matrix |
| `GET` | `/v1/status` | Live rate-state + today's usage |
| `GET` | `/v1/routers` | Router pool health + tier order |
| `GET` | `/v1/calls` | Recent call history (filterable) |
| `GET` | `/` | Dashboard HTML |
| `GET` | `/help` | Help/docs page |

---

## 10. Full Data Flow — Agent Query Lifecycle

```
User Query
    │
    ▼
[Agent Loop — to be built]
    │
    ├─► Perception Layer
    │       POST /v1/chat  {auto_route: "perception", ...}
    │       Gateway: Router classifies → picks TINY worker
    │       Returns: PerceptionOutput {intent, entities, needs_tools, needs_memory}
    │
    ├─► Memory Layer
    │       Checks state/memory.json for relevant past facts
    │
    ├─► Decision Layer
    │       POST /v1/chat  {auto_route: "decision", ...}
    │       Returns: DecisionOutput {action, tool_name, reasoning}
    │
    ├─► Action Layer
    │       Calls MCP tool via stdio: web_search / fetch_url / read_file / etc.
    │       mcp_server.py executes → returns structured result
    │
    ├─► Memory Update
    │       Stores result to state/memory.json
    │
    └─► Loop until final_answer or MAX_ITER reached
```

---

## 11. What's Built vs. What Needs Building

| Component | Status |
|-----------|--------|
| `mcp_server.py` — 9 tools | ✅ Complete |
| `llm_gatewayV3/` — Full LLM gateway | ✅ Complete |
| Dashboard & monitoring | ✅ Complete |
| `agent6.py` — Main orchestration loop | ✅ Complete |
| `perception.py` — Perception layer | ✅ Complete |
| `decision.py` — Decision layer | ✅ Complete |
| `action.py` — MCP tool caller | ✅ Complete |
| `memory.py` — Persistent memory | ✅ Complete |
| `schemas.py` — Agent Pydantic contracts | ✅ Complete |
| `state/memory.json` — Memory store | ✅ Complete |

---

## 12. Architecture Diagram (Complete)

```
┌──────────────────────────────────────────────────────────────────┐
│                    USER / ASSIGNMENT QUERIES                     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│                        agent6.py                                 │
│                   [Agentic Loop — Complete]                      │
│  while iterations < MAX_ITER:                                    │
│    perception → decision → action → memory → check_done          │
└─┬──────────────┬──────────────┬──────────────┬───────────────────┘
  │              │              │              │
  ▼              ▼              ▼              ▼
perception    decision       action         memory
  .py           .py           .py            .py
  │              │              │              │
  │              │              │          state/
  │              │              │          memory.json
  │              │              │
  └──────────────┴──────────────┘
         HTTP POST /v1/chat
                │
  ┌─────────────▼──────────────────────────────────────────────────┐
  │                   llm_gatewayV3/main.py                        │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │  Router Pool (router_pool):                             │   │
  │  │  cerebras → groq → nvidia → github                     │   │
  │  │  Classifies: TINY / LARGE / HUGE                        │   │
  │  └────────────────────────┬────────────────────────────────┘   │
  │                           │                                    │
  │  ┌────────────────────────▼────────────────────────────────┐   │
  │  │  Worker Pool (router):                                  │   │
  │  │  TINY:  github→openrouter→groq→nvidia→cerebras→gemini   │   │
  │  │  LARGE: gemini→groq→nvidia→cerebras→github→openrouter   │   │
  │  │  Rate limiting: RPM/RPD/TPM/cooldown/backoff            │   │
  │  └────────────────────────┬────────────────────────────────┘   │
  │                           │                                    │
  │  ┌────────────────────────▼────────────────────────────────┐   │
  │  │  providers.py:                                          │   │
  │  │  Gemini | Groq | Cerebras | Nvidia | OR | GitHub |Ollama│   │
  │  └─────────────────────────────────────────────────────────┘   │
  │                                                                 │
  │  db.py → gateway_v3.db (SQLite call log)                       │
  │  cache.py → GeminiCache (system prompt caching)                │
  └─────────────────────────────────────────────────────────────────┘
                             │
         ┌───────────────────┘ (action layer calls tools)
         │
  ┌──────▼──────────────────────────────────────────────────────────┐
  │                   mcp_server.py (stdio)                         │
  │  web_search | fetch_url | get_time | currency_convert           │
  │  read_file | list_dir | create_file | update_file | edit_file   │
  │  → sandbox/ (isolated filesystem)                               │
  └─────────────────────────────────────────────────────────────────┘
```
