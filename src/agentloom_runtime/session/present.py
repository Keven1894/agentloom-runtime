"""Build a trilingual presentation pack from an archived conversation.

The archive body stays the source of truth. This module calls a local chat
model (MindRouter on Spark by default) to overlay English and Spanish copies
for search and display. Thinking models must have think disabled and a
completion cap, or a long turn will burn the gateway timeout.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from agentloom_runtime.config import load_env
from agentloom_runtime.session.transcript import TranscriptDocument

__all__ = [
    "DEFAULT_MODEL",
    "batched",
    "mindrouter_chat",
    "parse_json_object",
    "present_transcript",
    "prose_items",
    "retry_delay_seconds",
    "split_pieces",
]

DEFAULT_MODEL = "qwen3:32b"
DEFAULT_TIMEOUT = 240
DEFAULT_RETRIES = 4
DEFAULT_BATCH_CHARS = 1000

# The gateway aborts a backend attempt at 180s. Measured generation on Spark is
# 20.8 tok/s at the tenth percentile, so anything allowed to reach ~3700 tokens
# times out by construction, and three of those in a row take the whole backend
# offline. Cap the completion well under that ceiling and keep one request's
# source text small enough that the cap is never the binding constraint.
MAX_COMPLETION_TOKENS = 2048
PIECE_CHARS = 1000

CJK = re.compile(r"[\u3400-\u9fff]")
THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
LANG = {"en": "English", "es": "Spanish"}

ChatFn = Callable[[list[dict[str, str]]], str]


def parse_json_object(text: str) -> dict[str, Any]:
    text = THINK.sub("", text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    blob = text[start : end + 1]
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        try:
            # Qwen often embeds raw newlines/control chars inside JSON strings.
            parsed = json.loads(blob, strict=False)
        except json.JSONDecodeError as exc:
            try:
                parsed = json.loads(_repair_invalid_escapes(blob), strict=False)
            except json.JSONDecodeError as exc2:
                parsed = json.loads(_close_dangling_string(blob, exc2), strict=False)
    if not isinstance(parsed, dict):
        raise ValueError("model output JSON is not an object")
    return parsed


def _close_dangling_string(blob: str, exc: json.JSONDecodeError) -> str:
    """Re-add a closing quote the model dropped before the final brace.

    A value that ends in a backtick-quoted markdown span makes Qwen close the
    span and then jump straight to ``}``, leaving the JSON string open. The
    decode fails at the *start* of that string, so the whole object is lost to
    a single missing character.
    """
    if not exc.msg.startswith("Unterminated string"):
        raise exc
    if blob[exc.pos : exc.pos + 1] != '"' or not blob.endswith("}"):
        raise exc
    return blob[:-1].rstrip() + '"}'


_JSON_ESCAPES = frozenset('"\\/bfnrt')


def _repair_invalid_escapes(blob: str) -> str:
    """Double backslashes that are not valid JSON escapes.

    Qwen copies Windows paths and markdown (``C:\\projects``, ``\\env``) into
    JSON strings without escaping the slash, which ``json.loads`` rejects as
    ``Invalid \\escape``. Temperature retries reproduce the same defect.
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(blob)
    while i < n:
        ch = blob[i]
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == '\\' and i + 1 < n:
            nxt = blob[i + 1]
            if nxt == 'u' and i + 5 < n and all(
                c in '0123456789abcdefABCDEF' for c in blob[i + 2 : i + 6]
            ):
                out.append(blob[i : i + 6])
                i += 6
                continue
            if nxt in _JSON_ESCAPES:
                out.append(blob[i : i + 2])
                i += 2
                continue
            out.append('\\\\')
            i += 1
            continue
        if ch == '"':
            in_string = False
        out.append(ch)
        i += 1
    return ''.join(out)


def retry_delay_seconds(exc: BaseException) -> Optional[float]:
    """Backoff for a MindRouter failure that is usually gone after a short wait.

    A 404 here is almost never a wrong URL. MindRouter answers unknown or
    unloaded models with ``model_not_found``, which is also what it returns
    while the spark-ollama circuit breaker is open. Sleep long enough for
    the 30s recovery window instead of burning the rest of the queue.
    """
    if isinstance(exc, urllib.error.HTTPError):
        body = getattr(exc, "body_text", "") or ""
        if exc.code == 404 and "model_not_found" in body:
            return 30.0
        if exc.code in {429, 500, 502, 503, 504}:
            return 15.0
        return None
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        return 15.0
    if isinstance(exc, urllib.error.URLError) and "timed out" in str(exc.reason).lower():
        return 15.0
    return None


def batched(items: list[dict[str, Any]], max_chars: int = DEFAULT_BATCH_CHARS) -> list[list[dict[str, Any]]]:
    out: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    size = 0
    for item in items:
        n = len(item["text"])
        if cur and size + n > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += n
    if cur:
        out.append(cur)
    return out


def _split_text(text: str, max_chars: int) -> list[str]:
    """Cut text on line boundaries, falling back to a hard cut for one long line."""
    out: list[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:max_chars])
            line = line[max_chars:]
        if cur and len(cur) + len(line) > max_chars:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out or [text]


def split_pieces(
    items: list[dict[str, Any]], *, max_chars: int = PIECE_CHARS
) -> list[dict[str, Any]]:
    """Cut turns into request-sized pieces, keyed so the parts can be rejoined.

    A single turn can run to 12k characters, which no one request can translate
    inside the gateway's budget. Parts of the same turn carry ``seq#part`` keys
    and are stitched back into one overlay string once every part is in.
    """
    pieces: list[dict[str, Any]] = []
    for item in items:
        seq = str(item["seq"])
        text = item["text"]
        if len(text) <= max_chars:
            pieces.append({"seq": seq, "key": seq, "text": text, "part": 0, "parts": 1})
            continue
        chunks = _split_text(text, max_chars)
        for part, chunk in enumerate(chunks):
            pieces.append(
                {
                    "seq": seq,
                    "key": f"{seq}#{part}",
                    "text": chunk,
                    "part": part,
                    "parts": len(chunks),
                }
            )
    return pieces


def prose_items(doc: TranscriptDocument, *, cjk_only: bool = True) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for turn in doc.turns:
        texts = [b.text for b in turn.blocks if b.type == "text" and b.text]
        if not texts:
            continue
        blob = texts[0]
        if cjk_only and not CJK.search(blob):
            continue
        items.append({"seq": turn.seq, "text": blob})
    return items


def _mindrouter_credentials() -> tuple[str, str]:
    load_env()
    from dotenv import dotenv_values

    from agentloom_runtime.config import find_env_file

    found = find_env_file()
    file_vals = dotenv_values(found) if found else {}
    base = (
        os.environ.get("MINDROUTER_BASE_URL")
        or os.environ.get("mindrouter-base-url")
        or file_vals.get("mindrouter-base-url")
        or file_vals.get("MINDROUTER_BASE_URL")
        or ""
    )
    key = (
        os.environ.get("MINDROUTER_API_KEY")
        or os.environ.get("mindrouter-api-key")
        or file_vals.get("mindrouter-api-key")
        or file_vals.get("MINDROUTER_API_KEY")
        or ""
    )
    if not base or not key:
        raise RuntimeError("MindRouter base URL and API key are missing from the environment")
    return str(base).rstrip("/"), str(key)


def _mindrouter_once(
    messages: list[dict[str, str]],
    *,
    model: str,
    max_tokens: int,
    timeout: int,
    temperature: float = 0.1,
) -> str:
    base, key = _mindrouter_credentials()
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "think": False,
            "stream": False,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            exc.body_text = exc.read().decode("utf-8", "replace")  # type: ignore[attr-defined]
        except Exception:
            exc.body_text = ""  # type: ignore[attr-defined]
        raise
    message = (data.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content") or ""
    return THINK.sub("", content).strip()


def mindrouter_chat(
    messages: list[dict[str, str]],
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = MAX_COMPLETION_TOKENS,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    temperature: float = 0.1,
) -> str:
    max_tokens = min(int(max_tokens), MAX_COMPLETION_TOKENS)
    last: Optional[BaseException] = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            return _mindrouter_once(
                messages,
                model=model,
                max_tokens=max_tokens,
                timeout=timeout,
                temperature=temperature,
            )
        except BaseException as exc:
            last = exc
            delay = retry_delay_seconds(exc)
            if delay is None or attempt >= attempts:
                if isinstance(exc, urllib.error.HTTPError):
                    body = getattr(exc, "body_text", "") or ""
                    raise RuntimeError(
                        f"HTTP Error {exc.code}: {exc.reason}"
                        + (f": {body[:300]}" if body else "")
                    ) from exc
                raise
            print(
                f"mindrouter: attempt {attempt}/{attempts} {type(exc).__name__}: {exc}; "
                f"sleep {delay:.0f}s",
                flush=True,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def _translate_map(
    chat_fn: ChatFn,
    locale: str,
    batch: list[dict[str, Any]],
) -> dict[str, str]:
    payload = {str(item["seq"]): item["text"] for item in batch}
    prompt = (
        f"/no_think\nTranslate each value into {LANG[locale]}.\n"
        "Keep markdown, code fences, file paths, commands, URLs, numbers, and proper nouns.\n"
        "Return ONLY a JSON object with the same keys and translated string values.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    parsed = parse_json_object(chat_fn([{"role": "user", "content": prompt}]))
    out: dict[str, str] = {}
    for item in batch:
        seq = str(item["seq"])
        val = parsed.get(seq)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"missing translation for seq {seq}")
        out[seq] = val
    return out


def _listing_copy(doc: TranscriptDocument, items: list[dict[str, Any]]) -> dict[str, str]:
    title = (doc.title or "").strip()
    if not title and items:
        title = items[0]["text"].splitlines()[0][:80]
    description = ""
    if items:
        description = " ".join(items[0]["text"].split())[:280]
    return {"title": title, "description": description}


def _translate_listing(chat_fn: ChatFn, original: dict[str, str]) -> dict[str, Any]:
    prompt = (
        "/no_think\nTranslate title and description into English and Spanish.\n"
        "Keep file paths, commands, URLs, numbers, and proper nouns.\n"
        "Return ONLY JSON: {\"title\": {\"en\": \"...\", \"es\": \"...\"}, "
        "\"description\": {\"en\": \"...\", \"es\": \"...\"}}.\n\n"
        + json.dumps(original, ensure_ascii=False)
    )
    parsed = parse_json_object(chat_fn([{"role": "user", "content": prompt}]))
    pack: dict[str, Any] = {
        "title": {"original": original["title"]},
        "description": {"original": original["description"]},
    }
    for field in ("title", "description"):
        loc = parsed.get(field) or {}
        if not isinstance(loc, dict):
            continue
        for locale in ("en", "es"):
            val = loc.get(locale)
            if isinstance(val, str) and val.strip():
                pack[field][locale] = val.strip()
    return pack


def build_presentation(
    doc: TranscriptDocument,
    *,
    chat_fn: ChatFn,
    locales: tuple[str, ...] = ("en", "es"),
    batch_chars: int = DEFAULT_BATCH_CHARS,
) -> dict[str, Any]:
    items = prose_items(doc, cjk_only=True)
    if not items:
        items = prose_items(doc, cjk_only=False)[:4]
    original = _listing_copy(doc, items)
    pack = _translate_listing(chat_fn, original)
    turns: dict[str, dict[str, list[str]]] = {}
    batches = batched(items, max_chars=batch_chars)
    print(
        f"present: {len(items)} CJK turns in {len(batches)} batch(es)",
        flush=True,
    )
    for locale in locales:
        loc_map: dict[str, list[str]] = {}
        for i, batch in enumerate(batches, 1):
            print(
                f"present: {locale} {i}/{len(batches)} seq={[x['seq'] for x in batch]}",
                flush=True,
            )
            loc_map.update(
                {seq: [text] for seq, text in _translate_map(chat_fn, locale, batch).items()}
            )
        turns[locale] = loc_map
    pack["turns"] = turns
    return pack


def present_transcript(
    transcript_id: str,
    *,
    model: str = DEFAULT_MODEL,
    chat_fn: Optional[ChatFn] = None,
) -> dict[str, Any]:
    from agentloom_runtime.session.store import load_transcript, set_transcript_presentation

    doc = load_transcript(transcript_id=transcript_id)
    if doc is None:
        raise KeyError(transcript_id)
    fn = chat_fn or (
        lambda messages: mindrouter_chat(messages, model=model)
    )
    pack = build_presentation(doc, chat_fn=fn)
    rec = set_transcript_presentation(transcript_id, pack)
    return {
        "transcript_id": rec.transcript_id,
        "source_ref": rec.source_ref,
        "model": model,
        "title": (rec.presentation or {}).get("title") or {},
        "turns_en": len((rec.presentation or {}).get("turns", {}).get("en") or {}),
        "turns_es": len((rec.presentation or {}).get("turns", {}).get("es") or {}),
    }
