"""Juno Travel Concierge: one small server that serves the app and talks to the AI.

This version uses Groq's free tier, so it costs nothing to run.
Run locally:   uvicorn main:app --reload
Needs:         GROQ_API_KEY in the environment (see .env.example)
"""
import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Tried in order. Each model has its own free allowance, so when one is busy the next one answers.
MODELS = [m.strip() for m in os.getenv("JUNO_MODELS", "openai/gpt-oss-120b,openai/gpt-oss-20b").split(",") if m.strip()]
# Groq's free plan allows about 8,000 tokens per minute per model, and the reply length we ask for
# counts toward that. Keep the reply allowance well under it, or Groq refuses the request outright.
JSON_TOKENS = int(os.getenv("JUNO_JSON_TOKENS", "3500"))
CHAT_TOKENS = int(os.getenv("JUNO_CHAT_TOKENS", "900"))
HOURLY_LIMIT = int(os.getenv("JUNO_HOURLY_LIMIT", "40"))  # AI requests per visitor per hour
CONTACT = os.getenv("JUNO_CONTACT", "juno-app")  # shown to the map service, e.g. your email

app = FastAPI(title="Juno Travel Concierge")
log = logging.getLogger("juno")
logging.basicConfig(level=logging.INFO, format="%(levelname)s juno: %(message)s")
_hits: dict[str, deque] = defaultdict(deque)


def headers() -> dict:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise HTTPException(503, "GROQ_API_KEY is not set on the server.")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def limit(request: Request) -> None:
    """Per-visitor hourly cap so one person cannot use up the free daily allowance."""
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "?")
    now, q = time.time(), _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= HOURLY_LIMIT:
        raise HTTPException(429, "Hourly limit reached.")
    q.append(now)


def parse_json(text: str):
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1])


class JsonAsk(BaseModel):
    prompt: str = Field(max_length=30_000)


class Message(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=12_000)


class Chat(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=14)


def payload_for(model: str, messages: list, max_tokens: int, **extra) -> dict:
    p = {"model": model, "messages": messages, "max_completion_tokens": max_tokens, **extra}
    if "gpt-oss" in model:
        p["reasoning_effort"] = "low"  # less hidden thinking, so more of the allowance goes to the answer
    return p


def why(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", {}).get("message", ""))[:300]
    except ValueError:
        return r.text[:300]


def wait_for(r: httpx.Response) -> float:
    try:
        return min(float(r.headers.get("retry-after", "8")), 30.0)
    except ValueError:
        return 8.0


async def ask_groq(messages: list, max_tokens: int, want_json: bool) -> str:
    """One answer from Groq, trying each model and waiting briefly when the free limit is hit."""
    hdrs = headers()
    problem = "The AI service could not answer right now."
    async with httpx.AsyncClient(timeout=120) as http:
        for attempt in range(3):
            shortest_wait = None
            for model in MODELS:
                extra = {"response_format": {"type": "json_object"}} if want_json else {}
                p = payload_for(model, messages, max_tokens, **extra)
                try:
                    r = await http.post(GROQ_URL, headers=hdrs, json=p)
                    if r.status_code == 400 and want_json:
                        log.warning("%s refused JSON mode (%s); retrying without it", model, why(r))
                        p.pop("response_format", None)
                        r = await http.post(GROQ_URL, headers=hdrs, json=p)
                except httpx.HTTPError as e:
                    log.warning("%s: network error %s", model, e)
                    continue
                if r.status_code == 200:
                    try:
                        text = r.json()["choices"][0]["message"]["content"] or ""
                    except (ValueError, KeyError, IndexError):
                        text = ""
                    if text.strip():
                        return text
                    log.warning("%s returned an empty answer", model)
                    continue
                detail = why(r)
                log.warning("%s answered %s: %s", model, r.status_code, detail)
                if r.status_code == 401:
                    raise HTTPException(503, "Groq rejected the GROQ_API_KEY. Check the key in Render's Environment settings.")
                if r.status_code in (429, 413):
                    w = wait_for(r)
                    shortest_wait = w if shortest_wait is None else min(shortest_wait, w)
                    problem = "Juno has used up the free AI allowance for this minute."
                elif r.status_code == 404:
                    problem = f"The AI model {model} is not available on Groq any more. Set JUNO_MODELS to a current model."
                else:
                    problem = f"The AI service answered with an error ({r.status_code})."
            if shortest_wait is None or attempt == 2:
                break
            log.info("all models busy; waiting %.0fs", shortest_wait)
            await asyncio.sleep(shortest_wait)
    raise HTTPException(429 if "allowance" in problem else 502, problem)


@app.post("/api/json")
async def ask_json(body: JsonAsk, request: Request):
    """Trip matches, hotels and itineraries: the AI answers with one JSON object."""
    limit(request)
    messages = [{"role": "user", "content": body.prompt}]
    for _ in range(2):  # one more try if the answer was not valid JSON
        text = await ask_groq(messages, JSON_TOKENS, want_json=True)
        try:
            return parse_json(text)
        except (ValueError, TypeError):
            log.warning("answer was not valid JSON: %s", text[:200])
    raise HTTPException(502, "The AI's answer came back garbled. Please try again.")


@app.post("/api/chat")
async def chat(body: Chat, request: Request):
    """City guide chat: streams the reply as plain text."""
    limit(request)
    hdrs = headers()
    messages = [m.model_dump() for m in body.messages]
    if messages[-1]["role"] != "user":
        raise HTTPException(400, "The last message must come from the user.")

    async def stream():
        async with httpx.AsyncClient(timeout=90) as http:
            for model in MODELS:
                sent = False
                try:
                    async with http.stream("POST", GROQ_URL, headers=hdrs,
                                           json=payload_for(model, messages, CHAT_TOKENS, stream=True)) as r:
                        if r.status_code != 200:
                            await r.aread()
                            log.warning("chat %s answered %s: %s", model, r.status_code, why(r))
                            continue
                        async for line in r.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                piece = json.loads(data)["choices"][0]["delta"].get("content")
                            except (ValueError, KeyError, IndexError, AttributeError):
                                continue
                            if piece:
                                sent = True
                                yield piece
                    if sent:
                        return
                except httpx.HTTPError as e:
                    log.warning("chat %s: network error %s", model, e)
                    if sent:
                        return
        yield "Juno is catching her breath. Please ask again in a minute."

    return StreamingResponse(stream(), media_type="text/plain; charset=utf-8")


@app.get("/api/where")
async def where(lat: float, lon: float):
    """Turns GPS coordinates into a street address, using OpenStreetMap's free lookup."""
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            r = await http.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 18, "addressdetails": 1},
                headers={"User-Agent": f"JunoTravelConcierge/1.0 ({CONTACT})", "Accept-Language": "en"},
            )
            r.raise_for_status()
            d = r.json()
    except (httpx.HTTPError, ValueError):
        return {}
    a = d.get("address", {})
    city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("state") or ""
    address = ", ".join(d.get("display_name", "").split(", ")[:4])
    return {"city": city, "address": address}


@app.get("/api/health")
async def health():
    return {"ok": True, "models": MODELS, "key_set": bool(os.getenv("GROQ_API_KEY"))}


@app.get("/api/test")
async def test():
    """Open this address in a browser to check that Juno can really reach the AI."""
    if not os.getenv("GROQ_API_KEY"):
        return {"working": False, "problem": "GROQ_API_KEY is not set in Render's Environment settings."}
    try:
        text = await ask_groq([{"role": "user", "content": "Reply with the single word: ready"}], 200, want_json=False)
        return {"working": True, "ai_said": text.strip()[:60], "models": MODELS}
    except HTTPException as e:
        return {"working": False, "problem": e.detail}


# The app itself. Files may sit in a "static" folder or right next to main.py; both work.
BASE = Path(__file__).parent
STATIC = BASE / "static" if (BASE / "static" / "index.html").exists() else BASE
ASSETS = {"index.html", "manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png", "apple-touch-icon.png"}


@app.get("/")
async def home():
    return FileResponse(STATIC / "index.html")


@app.get("/{name}")
async def asset(name: str):
    if name not in ASSETS:
        raise HTTPException(404, "Not found.")
    kind = "application/manifest+json" if name.endswith(".webmanifest") else None
    return FileResponse(STATIC / name, media_type=kind)
