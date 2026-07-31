#!/usr/bin/env python3
"""
Robin — Console UI backend (FastAPI).

A second, drop-in front door for robin's OSINT pipeline. It wraps the SAME
unmodified backend modules (llm.py, search.py, scrape.py, health.py,
llm_utils.py, config.py) that the Streamlit UI (ui.py) and the CLI (cli.py)
use — nothing about the owner's pipeline is changed here. The Streamlit app
keeps working exactly as before; this just serves a production web console
over HTTP with Server-Sent-Events streaming.

Run:
    .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8000

Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

import config
import llm_utils
from langchain_core.messages import AIMessage, HumanMessage
from llm import (
    PRESET_PROMPTS,
    answer_followup,
    build_followup_context,
    filter_results,
    generate_summary,
    get_llm,
    refine_query,
    suggest_pivots,
)
from llm_utils import BufferedStreamingHandler, get_model_choices, get_model_display_names
from search import get_search_results
from scrape import scrape_multiple
from health import check_llm_health, check_search_engines, check_tor_proxy

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INVESTIGATIONS_DIR = BASE_DIR / "investigations"

# Mirror of the labels shown in the Streamlit sidebar (ui.py) so a saved
# investigation's preset round-trips identically between the two UIs.
PRESET_LABELS = {
    "threat_intel": "🔍 Dark Web Threat Intel",
    "ransomware_malware": "🦠 Ransomware / Malware Focus",
    "personal_identity": "👤 Personal / Identity Investigation",
    "corporate_espionage": "🏢 Corporate Espionage / Data Leaks",
}

app = FastAPI(title="Robin — Dark Web OSINT Console")

# Stage log — every pipeline step writes a timestamped line here so a stalled
# run can be diagnosed from disk without a user staring at a dead spinner.
STAGE_LOG = BASE_DIR / "stage.log"
_log = logging.getLogger("robin.pipeline")

# LLM request timeout. robin's own _common_llm_params sets no timeout, so a
# hanging gateway (free OpenAI-compatible relays stall on streaming sockets)
# blocks a stage forever. Patch the SHARED dict once — get_llm() spreads it
# into every model's constructor (ChatOpenAI/Anthropic/Google all honour it),
# so a stalled HTTP call dies instead of spinning.
llm_utils._common_llm_params.setdefault("request_timeout", 90)
llm_utils._common_llm_params.setdefault("max_retries", 1)

# Hard ceilings per pipeline stage (seconds). The LLM timeout above is the
# first line of defence; these are the backstop so the UI can NEVER hang
# indefinitely even if a socket ignores the timeout.
_STAGE_TIMEOUT = {
    "load": 150, "refine": 120, "search": 300,
    "filter": 150, "scrape": 240, "synth": 600,
}


def _with_timeout(stage: str, fn, *args):
    """Run `fn(*args)` with a hard wall-clock ceiling. Re-raises on timeout as
    RuntimeError so the pipeline can emit a clear, stage-labelled error.

    shutdown(wait=False) is critical: a `with`-block executor would join the
    hung worker on exit and block this thread anyway. We let the orphan thread
    die on its own (the LLM request_timeout / socket timeout eventually fires)
    and return control to the pipeline immediately."""
    limit = _STAGE_TIMEOUT.get(stage, 300)
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args)
    try:
        return fut.result(timeout=limit)
    except FuturesTimeoutError:
        raise RuntimeError(
            f"stage '{stage}' exceeded {limit}s — the LLM/search socket "
            "stalled (gateway or Tor circuit unresponsive). Try again; if it "
            "recurs, the model endpoint is the bottleneck."
        )
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _emit(q, hb, **kw):
    """Push an event AND record it to the stage log."""
    q.put(kw)
    try:
        with STAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} "
                    f"{kw.get('t','?'):8s} {json.dumps({k:v for k,v in kw.items() if k!='t'}, default=str)}\n")
    except Exception:  # noqa: BLE001 — logging must never break the pipeline
        pass
    if hb is not None:
        hb[0] = time.time()


def _heartbeat(q, hb, stop):
    """While a stage runs, emit a periodic 'still alive' tick so the wire and
    the UI never look dead during a legitimately long Tor search/scrape."""
    while not stop.wait(8):
        if hb[0] is None:
            continue
        _emit(q, None, t="heartbeat", elapsed=round(time.time() - hb[0]))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sse(obj: dict) -> str:
    """Format one Server-Sent-Events frame (default `message` event)."""
    return f"data: {json.dumps(obj)}\n\n"


def _is_set(value) -> bool:
    return bool(value and str(value).strip() and "your_" not in str(value))


def _resolve_model(requested: str | None) -> str:
    """Pick the requested model, falling back to the configured custom model."""
    m = (requested or "").strip()
    if m:
        return m
    return (config.CUSTOM_API_MODEL or "").strip()


def _save_investigation(query: str, refined_query: str, model: str,
                        preset_label: str, sources: list, summary: str) -> str:
    """Persist a completed investigation (same on-disk format as ui.py)."""
    INVESTIGATIONS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"investigation_{timestamp}.json"
    data = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "refined_query": refined_query,
        "model": model,
        "preset": preset_label,
        "sources": sources,
        "summary": summary,
    }
    (INVESTIGATIONS_DIR / fname).write_text(json.dumps(data, indent=2))
    return fname


def _load_investigations() -> list:
    if not INVESTIGATIONS_DIR.exists():
        return []
    files = sorted(INVESTIGATIONS_DIR.glob("investigation_*.json"), reverse=True)
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            data["_filename"] = f.name
            out.append(data)
        except Exception:
            continue
    return out


def _to_lc_messages(history: list, max_turns: int = 5) -> list:
    """Window the last `max_turns` Q&A turns into LangChain messages."""
    recent = history[-(max_turns * 2):] if history else []
    msgs = []
    for turn in recent:
        if turn.get("role") == "user":
            msgs.append(HumanMessage(content=turn.get("content", "")))
        else:
            msgs.append(AIMessage(content=turn.get("content", "")))
    return msgs


def _stream_response(q: queue.Queue, timeout: int = 300):
    """Async generator that drains a queue of event dicts into an SSE stream.

    A `None` sentinel marks end-of-stream. Emits a timeout error if the worker
    goes quiet for `timeout` seconds (hidden services can be slow, but not that
    slow)."""
    async def gen():
        loop = asyncio.get_event_loop()
        while True:
            try:
                ev = await loop.run_in_executor(None, q.get, True, timeout)
            except queue.Empty:
                yield _sse({"t": "error", "stage": "timeout",
                            "message": f"pipeline stalled — no events for {timeout}s"})
                return
            if ev is None:
                return
            yield _sse(ev)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Config / models / providers
# --------------------------------------------------------------------------- #
@app.get("/api/presets")
async def api_presets():
    return {"presets": [{"key": k, "label": v} for k, v in PRESET_LABELS.items()]}


@app.get("/api/models")
async def api_models():
    models = get_model_choices()
    display = get_model_display_names(models)
    recommended = ""
    custom = (config.CUSTOM_API_MODEL or "").strip().lower()
    if custom:
        for m in models:
            if m.strip().lower() == custom:
                recommended = m
                break
    if not recommended and models:
        recommended = models[0]
    return {
        "models": [{"key": m, "label": display.get(m, m)} for m in models],
        "recommended": recommended,
    }


@app.get("/api/providers")
async def api_providers():
    from config import (
        ANTHROPIC_API_KEY, GOOGLE_API_KEY, LLAMA_CPP_BASE_URL, OLLAMA_BASE_URL,
        OPENAI_API_KEY, OPENROUTER_API_KEY,
    )
    providers = [
        ("OpenAI", _is_set(OPENAI_API_KEY), True),
        ("Anthropic", _is_set(ANTHROPIC_API_KEY), True),
        ("Google", _is_set(GOOGLE_API_KEY), True),
        ("OpenRouter", _is_set(OPENROUTER_API_KEY), True),
        ("Ollama", _is_set(OLLAMA_BASE_URL), False),
        ("llama.cpp", _is_set(LLAMA_CPP_BASE_URL), False),
        ("Custom API", _is_set(config.CUSTOM_API_BASE_URL), False),
    ]
    return {
        "providers": [
            {"name": n, "configured": ok, "required": req}
            for n, ok, req in providers
        ]
    }


@app.get("/api/config")
async def api_config_get():
    """Current custom-provider state (raw key is never echoed — only has_key)."""
    return {
        "base_url": config.CUSTOM_API_BASE_URL or "",
        "model": config.CUSTOM_API_MODEL or "",
        "has_key": bool(config.CUSTOM_API_KEY),
    }


@app.post("/api/config")
async def api_config(request: Request):
    """Update the custom OpenAI-compatible provider at runtime.

    Blank fields mean *keep the existing value* — never wipe a working .env
    config because the user opened the drawer and hit save without editing.
    `llm_utils` reads `config.CUSTOM_API_*` dynamically on every resolve, so
    mutating the config module takes effect immediately — same mechanism the
    Streamlit sidebar uses."""
    body = await request.json()
    base = (body.get("base_url") or "").strip()
    key = (body.get("api_key") or "").strip()
    model = (body.get("model") or "").strip()
    if base:
        config.CUSTOM_API_BASE_URL = base
    if key:
        config.CUSTOM_API_KEY = key
    if model:
        config.CUSTOM_API_MODEL = model
    return {
        "ok": True,
        "base_url": config.CUSTOM_API_BASE_URL,
        "model": config.CUSTOM_API_MODEL,
        "has_key": bool(config.CUSTOM_API_KEY),
    }


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
@app.get("/api/health/tor")
async def api_health_tor():
    return check_tor_proxy()


@app.get("/api/health/llm")
async def api_health_llm(model: str | None = None):
    return check_llm_health(_resolve_model(model))


@app.get("/api/health/engines")
async def api_health_engines():
    return {"engines": check_search_engines()}


# --------------------------------------------------------------------------- #
# Investigations (persistence)
# --------------------------------------------------------------------------- #
@app.get("/api/investigations")
async def api_investigations():
    return {"investigations": _load_investigations()}


@app.get("/api/investigations/{filename}")
async def api_investigation(filename: str):
    # Refuse path traversal — only bare filenames inside the investigations dir.
    safe = Path(filename).name
    path = INVESTIGATIONS_DIR / safe
    if not path.exists() or path.suffix != ".json":
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        data = json.loads(path.read_text())
        data["_filename"] = safe
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --------------------------------------------------------------------------- #
# Investigation pipeline (SSE)
# --------------------------------------------------------------------------- #
def _run_pipeline(payload: dict, q: queue.Queue) -> None:
    """Execute robin's exact pipeline in a worker thread, pushing SSE events.

    Mirrors ui.py stage-for-stage: load → refine → search → filter → scrape →
    summarize (streamed) → pivots → save. Every blocking step is wrapped in a
    hard timeout (_with_timeout) and a heartbeat thread ticks every 8s so a
    stalled gateway or Tor circuit surfaces as a clear error instead of an
    infinite spinner. Each event is also appended to STAGE_LOG for diagnosis."""
    hb = [None]                       # [last_activity_ts]; shared with heartbeat
    stop = threading.Event()
    hb_thread = threading.Thread(target=_heartbeat, args=(q, hb, stop), daemon=True)
    hb_thread.start()

    def emit(**kw):
        _emit(q, hb, **kw)

    try:
        model = _resolve_model(payload.get("model"))
        preset = payload.get("preset") or "threat_intel"
        custom = (payload.get("custom_instructions") or "").strip()
        threads = int(payload.get("threads", 4))
        max_results = int(payload.get("max_results", 50))
        max_scrape = int(payload.get("max_scrape", 10))
        query = (payload.get("query") or "").strip()

        if not model:
            emit(t="error", stage="load",
                 message="No model configured — set a custom provider in Settings.")
            return
        if not query:
            emit(t="error", stage="refine", message="Empty query.")
            return

        emit(t="stage", stage="load", status="start")
        llm = _with_timeout("load", get_llm, model)
        emit(t="stage", stage="load", status="done")

        emit(t="stage", stage="refine", status="start")
        refined = _with_timeout("refine", refine_query, llm, query).strip()
        emit(t="stage", stage="refine", status="done")
        emit(t="refined", query=refined)

        emit(t="stage", stage="search", status="start")
        results = _with_timeout("search", get_search_results,
                                refined.replace(" ", "+"), threads)
        if len(results) > max_results:
            results = results[:max_results]
        emit(t="stage", stage="search", status="done", count=len(results))
        emit(t="counts", results=len(results))

        emit(t="stage", stage="filter", status="start")
        filtered = _with_timeout("filter", filter_results, llm, refined, results)
        if len(filtered) > max_scrape:
            filtered = filtered[:max_scrape]
        emit(t="stage", stage="filter", status="done", count=len(filtered))
        emit(t="filtered", sources=filtered)

        emit(t="stage", stage="scrape", status="start")
        scraped = _with_timeout("scrape", scrape_multiple, filtered, threads)
        emit(t="stage", stage="scrape", status="done", count=len(scraped))
        emit(t="scraped", count=len(scraped))

        emit(t="stage", stage="synth", status="start")
        streamed = {"text": ""}

        def _push(chunk: str):
            streamed["text"] += chunk
            emit(t="chunk", text=chunk)

        llm.callbacks = [BufferedStreamingHandler(ui_callback=_push)]

        def _synth():
            return generate_summary(
                llm, query, scraped, preset=preset, custom_instructions=custom
            )

        summary_text = _with_timeout("synth", _synth)
        # Reasoning models stream no tokens; fall back to the returned text.
        if not streamed["text"].strip() and summary_text:
            streamed["text"] = summary_text
            emit(t="chunk", text=summary_text)
        emit(t="stage", stage="synth", status="done")
        emit(t="summary", full=streamed["text"])

        # Pivots on a fresh LLM with no UI callback, never blocking the run.
        try:
            pivots = suggest_pivots(get_llm(model), query, scraped, preset=preset)
        except Exception as e:  # noqa: BLE001 — pivots are optional
            _log.warning("pivots failed: %s", e)
            pivots = []
        emit(t="pivots", items=pivots)

        fname = _save_investigation(
            query=query, refined_query=refined, model=model,
            preset_label=PRESET_LABELS.get(preset, preset),
            sources=filtered, summary=streamed["text"],
        )
        emit(t="saved", filename=fname)
        emit(t="done")
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        stage = getattr(e, "stage", "pipeline")
        _log.exception("pipeline failed at %s", stage)
        emit(t="error", stage=stage, message=f"{type(e).__name__}: {e}")
    finally:
        stop.set()
        hb_thread.join(timeout=2)
        q.put(None)


@app.post("/api/investigate")
async def api_investigate(request: Request):
    payload = await request.json()
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_run_pipeline, args=(payload, q), daemon=True).start()
    return _stream_response(q)


# --------------------------------------------------------------------------- #
# Follow-up chat (SSE)
# --------------------------------------------------------------------------- #
def _run_followup(payload: dict, q: queue.Queue) -> None:
    def emit(**kw):
        q.put(kw)

    try:
        model = _resolve_model(payload.get("model"))
        preset = payload.get("preset") or "threat_intel"
        custom = (payload.get("custom_instructions") or "").strip()
        question = (payload.get("question") or "").strip()
        if not model or not question:
            emit(t="error", message="Missing model or question.")
            return

        context = build_followup_context(
            payload.get("query", ""),
            payload.get("refined", ""),
            payload.get("sources", []),
            payload.get("scraped"),
            payload.get("summary", ""),
        )
        history = _to_lc_messages(payload.get("history", []))

        acc = {"text": ""}

        def _push(chunk: str):
            acc["text"] += chunk
            emit(t="chunk", text=chunk)

        llm = get_llm(model)
        llm.callbacks = [BufferedStreamingHandler(ui_callback=_push)]
        answer = answer_followup(
            llm, question, context, history=history,
            preset=preset, custom_instructions=custom,
        )
        if not acc["text"].strip() and answer:
            acc["text"] = answer
            emit(t="chunk", text=answer)
        emit(t="done", text=acc["text"])
    except Exception as e:  # noqa: BLE001
        emit(t="error", message=f"{type(e).__name__}: {e}")
    finally:
        q.put(None)


@app.post("/api/followup")
async def api_followup(request: Request):
    payload = await request.json()
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_run_followup, args=(payload, q), daemon=True).start()
    return _stream_response(q)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
