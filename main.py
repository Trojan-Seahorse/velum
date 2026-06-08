"""Velum · LLM PII firewall proxy

Hermes → [argus-redact per-entity strategy config] → DMXAPI (DeepSeek V4 Pro)

Design:
- PII detection + per-entity-type strategy via argus-redact `redact()` with `config=` dict.
  Default: identifier mode (per-type prefix: P/person, O/org, L/loc, T/phone, reversible)
  Optional: pseudonym-llm mode via !pii 伪名 (per-message, realistic fake: e.g. 张明)
- Restore PII in LLM response before returning to user
- Fail-open: pass-through on argus-redact error
- SSE streaming: buffer-then-restore (PII spans content + reasoning_content)
- No caching — DeepSeek handles prefix caching automatically

Strategy config:
  person/org/school/loc      → remove (P-NNNNN / O-NNNNN / L-NNNNN, reversible)
  phone/email/id/address     → remove (T-NNNNN / E-NNNNN / I-NNNNN / A-NNNNN, reversible)
  bank_card/credit_card      → mask   (****1234, irreversible)
  self_reference/medical     → keep   (LLM needs context)
  date                      → remove (D-NNNNN, reversible)

Per-type prefix (argus-redact v0.6+ default):
  All identifier types use distinct prefixes without brackets. This:
  - Gives LLM semantic type cues (P-00128 is a person, T-39281 is a phone)
  - Avoids bracket ambiguity ([TEL-79329] vs TEL-79329)

Mode prefix detection:
  !pii         → show PII firewall status
  !pii 伪名    → switch to redact_pseudonym_llm() (this message only)
  !pii debug   → analyze text without calling LLM
  !pii TYPE,TYPE → override entity strategies (this message only)
  !pii off      → skip PII entirely (raw pass-through, this message only)
  default      → identifier mode (no prefix needed)

Separator: space between sub-command and message body.
  !pii off <text>   → disable PII, send <text> as-is
  !pii debug <text> → analyze PII in <text>

All modes are per-message. No persistent state.

Environment:
  UPSTREAM_URL   — DMXAPI base URL (required)
  PII_ENABLED    — enable PII pipeline (default: true)
"""

import asyncio
import json
import os
from copy import deepcopy
from functools import partial
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Velum")

UPSTREAM = os.environ["UPSTREAM_URL"].rstrip("/")
PII_ENABLED = os.environ.get("PII_ENABLED", "true").lower() == "true"

# ── Health ──────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    status: dict = {"status": "ok", "upstream": UPSTREAM, "pii": "disabled"}
    if PII_ENABLED:
        try:
            from argus_redact import redact
            status["pii"] = "ok"
        except Exception as e:
            status["pii"] = f"error:{e}"
    return status


# ── Model list ─────────────────────────────────────────────────────────────


@app.get("/v1/models")
async def list_models():
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{UPSTREAM}/models")
            if r.status_code == 200 and r.content:
                return JSONResponse(content=r.json(), status_code=200)
    except Exception:
        pass
    return JSONResponse(content={
        "object": "list",
        "data": [{
            "id": "deepseek-v4-pro-guan",
            "object": "model",
            "created": 0,
            "owned_by": "dmxapi",
        }],
    })


# ── PII config & helpers ───────────────────────────────────────────────────

# Per-entity strategy config for argus-redact redact() API.
# PERSON/ORG/SCHOOL/LOC → remove (P-164 etc., reversible identifier)
# PHONE/EMAIL/ID/ADDRESS  → remove (labeled identifiers, reversible)
# BANK_CARD               → mask  (****1234, irreversible)
# SELF_REFERENCE/MEDICAL  → keep  (LLM needs context)
# DATE/MONEY              → remove (identifier — prevents keep-combination leakage)
DEFAULT_CONFIG = {
    "person":           {"strategy": "remove"},
    "organization":     {"strategy": "remove"},
    "school":           {"strategy": "remove"},
    "location":         {"strategy": "remove"},
    "phone":            {"strategy": "remove"},   # override default mask
    "phone_landline":   {"strategy": "remove"},
    "email":            {"strategy": "remove"},   # override default mask
    "id_number":        {"strategy": "remove"},
    "address":          {"strategy": "remove"},
    "date_of_birth":    {"strategy": "remove"},
    "workplace":        {"strategy": "remove"},
    "date":             {"strategy": "remove"},   # identifier, not keep
    "bank_card":        {"strategy": "mask"},
    "credit_card":      {"strategy": "mask"},
    "self_reference":   {"strategy": "keep"},
    # medical: argus-redact does not support keep → defaults to remove
    # money:   not in argus-redact 56-type catalog → NER won't detect
}

# Mode prefix: !pii <sub-command>
# Sub-commands: debug, 伪名/pseudonym/fake, TYPE,TYPE
# All modes are per-message — no persistent state.
_MODE_PII = "!pii"

# Per-type prefix: argus-redact v0.6+ default = P/person, O/org, L/loc, T/phone, etc.
# No brackets — avoids markdown/LM bracket-stripping issues. LLM gets semantic type cues.
# (Previously used unified_prefix="E" to hide types; per-type is now the default.)

# Compound location names with non-standard administrative suffixes
# (园区, 新区, 经开区, 高新区, 自贸区, etc.) that HanLP cascaded NER
# fails to detect when embedded in org names. Loaded from data file
# so the list can be maintained without code changes.
_LOCATION_NAMES_PATH = os.path.join(os.path.dirname(__file__), "location_names.txt")
LOCATION_NAMES: list[str] = []
try:
    with open(_LOCATION_NAMES_PATH, "r", encoding="utf-8") as f:
        LOCATION_NAMES = [
            line.strip() for line in f
            if line.strip() and not line.strip().startswith("#")
        ]
    print(f"[velum] Loaded {len(LOCATION_NAMES)} compound location names from location_names.txt")
except FileNotFoundError:
    print("[velum] location_names.txt not found — compound location injection disabled")
except Exception as e:
    print(f"[velum] Error loading location_names.txt: {e}")


def parse_mode_prefix(text: str) -> tuple:
    """Detect mode prefix in user message, return (mode, payload, config_override).

    Single entry point: !pii <sub-command>
      !pii                      → show status
      !pii debug [text]         → debug mode (skip LLM, return PII analysis)
      !pii 伪名 / pseudonym     → pseudonym-llm mode (this message only)
      !pii off [text]           → skip PII entirely (raw pass-through, this message only)
      !pii TYPE,TYPE            → partial override (keep specified types, this message only)

    All modes are per-message — the next message without a prefix
    defaults back to identifier mode. There is no persistent state.

    Half-width ! and full-width ！ are both accepted (no input-method switching).

    Returns:
      mode: "identifier" | "pseudonym" | "partial" | "debug" | "off"
      payload: text with prefix stripped (or original if no prefix)
      config_override: dict of entity→strategy overrides (for TYPE,TYPE)
    """
    # Normalize full-width ！→ half-width ! (Chinese IME convenience)
    stripped = text.strip().replace("\uff01", "!")

    if not stripped.startswith(_MODE_PII):
        return "identifier", text, {}

    rest = stripped[len(_MODE_PII):].strip()

    # sub-command: debug
    if rest.startswith("debug"):
        payload = rest[len("debug"):].strip()
        return "debug", payload, {}

    # sub-command: 伪名 / pseudonym / fake
    if rest in ("伪名", "pseudonym", "fake"):
        return "pseudonym", "", {}

    # sub-command: off / disable / none → skip PII entirely (raw pass-through)
    for cmd in ("off", "disable", "none"):
        if rest == cmd:
            return "off", "", {}
        if rest.startswith(cmd + " "):
            payload = rest[len(cmd):].strip()
            return "off", payload, {}

    # bare !pii → show status (debug mode with empty payload triggers _debug_status)
    if not rest:
        return "debug", "", {}

    # Else: TYPE,TYPE → partial override
    types = [t.strip().lower() for t in rest.replace(",", " ").split()]
    override = {}
    for t in types:
        mapped = {
            "loc": "location", "org": "organization",
            "tel": "phone", "id": "id_number", "id_card": "id_number",
            "addr": "address",
        }.get(t, t)
        override[mapped] = {"strategy": "keep"}
    return "partial", "", override


async def redact_text(text: str, mode: str = "identifier", config_override: dict = None) -> dict:
    """Detect + redact PII via argus-redact redact() API with per-entity strategy config.

    Default mode ("identifier"): uses DEFAULT_CONFIG → remove/pseudonym/mask/keep.
    Pseudonym mode: uses redact_pseudonym_llm() for realistic fake names.
    Partial mode: merges config_override (keep specified types) into DEFAULT_CONFIG.

    Returns: {has_pii: bool, redacted: str, key: dict|None}
    Fail-open: returns original text on error.
    """
    if not PII_ENABLED or not text or not text.strip():
        return {"has_pii": False, "redacted": text, "key": None}

    try:
        if mode == "pseudonym":
            from argus_redact import redact_pseudonym_llm
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, partial(redact_pseudonym_llm, text, lang="zh", _polluted_input_ok=True)
            )
            if (result.downstream_text
                    and result.downstream_text != text
                    and result.key):
                return {"has_pii": True, "redacted": result.downstream_text, "key": result.key}
            return {"has_pii": False, "redacted": text, "key": None}

        # identifier or partial mode
        from argus_redact import redact

        config = deepcopy(DEFAULT_CONFIG)
        if config_override:
            config.update(config_override)

        loop = asyncio.get_running_loop()
        redact_kwargs = dict(config=config, lang="zh")
        if LOCATION_NAMES:
            redact_kwargs["names"] = LOCATION_NAMES
        redacted_text, redact_key = await loop.run_in_executor(
            None, partial(redact, text, **redact_kwargs)
        )

        if redacted_text and redacted_text != text and redact_key:
            return {"has_pii": True, "redacted": redacted_text, "key": redact_key}
        return {"has_pii": False, "redacted": text, "key": None}
    except Exception as e:
        print(f"[velum] argus-redact error ({e}) · pass-through")
        return {"has_pii": False, "redacted": text, "key": None}


async def restore_text(text: str, key: dict) -> str:
    """Restore PII via argus-redact Python API.

    Works on any text containing identifiers (content, reasoning_content,
    SSE metadata) — argus_redact.restore() does deterministic string
    replacement based on the key mapping.

    Defensive: also registers de-bracketed variants of keys (e.g. [TEL-79329]
    → TEL-79329) to handle LLM bracket-stripping in extraction tasks.
    Fail-open: returns original text on error.
    """
    if not PII_ENABLED or not key:
        return text

    try:
        from argus_redact import restore
        loop = asyncio.get_running_loop()

        # Defensive: register de-bracketed key variants for bracket-stripping resilience
        augmented_key = dict(key)
        for k, v in key.items():
            if k.startswith("[") and k.endswith("]"):
                debracketed = k[1:-1]  # [TEL-79329] → TEL-79329
                if debracketed not in augmented_key:
                    augmented_key[debracketed] = v

        return await loop.run_in_executor(
            None, partial(restore, text, augmented_key)
        )
    except Exception as e:
        print(f"[velum] argus restore err: {e}")
    return text


# ── Message helpers ────────────────────────────────────────────────────────


def get_last_user_content(messages: list) -> Optional[str]:
    """Extract the last user message content from messages array."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle content array (multi-modal format)
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                return "".join(parts) if parts else ""
            return str(content) if content else ""
    return None


def set_last_user_content(messages: list, new_content: str):
    """Replace the last user message content in-place."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            msg["content"] = new_content
            return


# ── Debug handler ────────────────────────────────────────────────────────────


def _debug_status() -> str:
    """Build status report for bare !pii / !pii debug."""
    lines = [
        "PII Firewall 状态",
        "=" * 30,
        f"PII 开关:  {'启用' if PII_ENABLED else '禁用'}",
        f"复合地名:  {len(LOCATION_NAMES)} 条",
        f"前缀格式:  per-type (P-人 O-组织 L-地点 T-电话 ...)",
        "",
        "可用命令:",
        "  !pii                   查看此状态",
        "  !pii debug <文本>      分析 PII 检测结果",
        "  !pii 伪名              伪名模式 (本消息)",
        "  !pii off <文本>        跳过脱敏 (本消息)",
        "  !pii TYPE,TYPE         放行指定类型 (本消息)",
        "",
        "策略配置:",
    ]
    for entity, cfg in sorted(DEFAULT_CONFIG.items()):
        lines.append(f"  {entity:20s} → {cfg['strategy']}")
    return "\n".join(lines)


def _debug_analyze(text: str) -> str:
    """Run PII analysis on text, return formatted result."""
    try:
        from argus_redact import redact

        config = deepcopy(DEFAULT_CONFIG)
        redact_kwargs = dict(config=config, lang="zh", detailed=True)
        if LOCATION_NAMES:
            redact_kwargs["names"] = LOCATION_NAMES

        r, k, details = redact(text, **redact_kwargs)
        entities = details.get("entities", [])

        # dedup: names regex + HanLP NER can both match same entity
        seen = set()
        unique = []
        for e in entities:
            if e["original"] not in seen:
                seen.add(e["original"])
                unique.append(e)
        entities = unique

        lines = [
            f"原文: {text}",
            f"脱敏: {r}",
            f"实体数: {len(entities)}",
            "",
        ]

        if not entities:
            lines.append("未检测到任何实体 → 文本直接放行")
        else:
            lines.append("检测到的实体:")
            for e in entities:
                lines.append(
                    f"  [{e.get('layer', '?')}] {e.get('type', '?')}"
                    f"  {e['original']} → {e['replacement']}"
                )
            lines.append("")
            lines.append("密钥映射:")
            for placeholder, original in k.items():
                lines.append(f"  {placeholder} → {original}")
        return "\n".join(lines)
    except Exception as e:
        return f"PII 分析异常: {e}"


# ── Chat completions ────────────────────────────────────────────────────────


@app.api_route("/v1/chat/completions", methods=["POST"])
async def chat_completions(request: Request):
    body = await request.body()
    body_json = json.loads(body)
    messages = body_json.get("messages", [])
    stream = body_json.get("stream", False)
    model = body_json.get("model", "?")

    # ── Mode detection + PII redaction ──
    pii_key: Optional[dict] = None
    pii_mode = "identifier"
    user_text = get_last_user_content(messages)
    if user_text:
        pii_mode, clean_text, config_override = parse_mode_prefix(user_text)

        # !pii debug mode: short-circuit — return PII analysis as SSE stream
        # !pii off: skip PII entirely — pii_key stays None, no redaction
        # (SSE format required for Hermes gateway compatibility)
        if pii_mode == "debug":
            response_text = _debug_status() if not clean_text else _debug_analyze(clean_text)

            async def debug_sse():
                event = json.dumps({
                    "id": "pii-debug",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "pipeline-debug",
                    "choices": [{
                        "index": 0,
                        "delta": {"role": "assistant", "content": response_text},
                        "finish_reason": "stop",
                    }],
                }, ensure_ascii=False)
                yield f"data: {event}\n\ndata: [DONE]\n".encode("utf-8")

            return StreamingResponse(debug_sse(), media_type="text/event-stream")

        # !pii off mode: skip PII detection and redaction entirely
        if pii_mode == "off":
            # Replace user text with clean payload (strip the !pii off prefix)
            if clean_text:
                set_last_user_content(messages, clean_text)
                body_json["messages"] = messages
                body = json.dumps(body_json).encode("utf-8")
            print(f"[velum] REQ stream={stream} msgs={len(messages)} model={model} mode=off")
            # Fall through to upstream call with pii_key=None
        else:
            pii_result = await redact_text(clean_text or user_text, pii_mode, config_override)
            if pii_result["has_pii"]:
                set_last_user_content(messages, pii_result["redacted"])
                pii_key = pii_result["key"]
                body_json["messages"] = messages
                body = json.dumps(body_json).encode("utf-8")

            print(
                f"[velum] REQ stream={stream} msgs={len(messages)} "
                f"model={model} mode={pii_mode} pii={'YES' if pii_key else 'no'}"
            )

    # ── DeepSeek thinking injection ──
    if model.startswith("deepseek") and "thinking" not in body_json:
        body_json["thinking"] = {"type": "enabled"}
        body = json.dumps(body_json).encode("utf-8")
        print("[velum] thinking injected: enabled (deepseek default override)")

    # ── Build upstream headers ──
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "transfer-encoding", "content-length")
    }
    headers["content-type"] = "application/json"

    # ── Non-streaming ──
    if not stream:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{UPSTREAM}/chat/completions", content=body, headers=headers
            )

        if r.status_code != 200:
            preview = r.text[:500]
            print(f"[velum] UPSTREAM {r.status_code}: {preview}")
            return JSONResponse(
                content={"error": f"Upstream {r.status_code}", "detail": preview},
                status_code=502,
            )

        if not r.content:
            return JSONResponse(
                content={"error": "Upstream returned empty body"}, status_code=502
            )

        try:
            resp_json = r.json()
        except Exception:
            return JSONResponse(
                content={"error": "Upstream non-JSON", "detail": r.text[:500]},
                status_code=502,
            )

        # ── PII restore (content + reasoning_content) ──
        if pii_key:
            message = resp_json.get("choices", [{}])[0].get("message", {})
            for field in ("content", "reasoning_content"):
                text = message.get(field, "")
                if text:
                    message[field] = await restore_text(text, pii_key)
            print(f"[velum] PII restored")

        print(f"[velum] OK non-stream")
        return JSONResponse(content=resp_json, status_code=r.status_code)

    # ── Streaming ──
    # Strategy: if PII detected, buffer all SSE chunks → extract content +
    # reasoning_content → restore → repack as single SSE event.
    # Why not restore raw SSE: DeepSeek SSE chunks split identifiers across
    # lines, so full identifier may never appear as continuous text.
    # Without PII, stream chunks in real time.

    sse_buffer: list[bytes] = []

    async def generate():
        async with httpx.AsyncClient(timeout=120) as stream_client:
            async with stream_client.stream(
                "POST", f"{UPSTREAM}/chat/completions",
                content=body, headers=headers,
            ) as upstream_resp:
                if pii_key:
                    # Buffer all → extract fields → restore → repack
                    async for chunk in upstream_resp.aiter_bytes():
                        sse_buffer.append(chunk)

                    full_sse = b"".join(sse_buffer).decode("utf-8", errors="replace")
                    content_parts = []
                    reasoning_parts = []
                    last_chunk = {}
                    for line in full_sse.split("\n"):
                        if not line.startswith("data: ") or line[6:] == "[DONE]":
                            continue
                        try:
                            c = json.loads(line[6:])
                            # Track last chunk for finish_reason / usage
                            last_chunk = c
                            delta = c.get("choices", [{}])[0].get("delta", {})
                            if delta.get("content"):
                                content_parts.append(delta["content"])
                            if delta.get("reasoning_content"):
                                reasoning_parts.append(delta["reasoning_content"])
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass

                    full_content = "".join(content_parts)
                    full_reasoning = "".join(reasoning_parts)

                    restored_content = await restore_text(full_content, pii_key)
                    restored_reasoning = await restore_text(full_reasoning, pii_key) if full_reasoning else ""

                    print(
                        f"[velum] stream PII restored "
                        f"content={len(full_content)}→{len(restored_content)} "
                        f"reasoning={len(full_reasoning)}→{len(restored_reasoning)}"
                    )

                    # Yield single SSE event with restored content
                    event = {
                        "id": last_chunk.get("id", "pipeline"),
                        "object": "chat.completion.chunk",
                        "created": last_chunk.get("created", 0),
                        "model": last_chunk.get("model", "deepseek-v4-pro"),
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": restored_content,
                                "reasoning_content": restored_reasoning or None,
                            },
                            "finish_reason": "stop",
                        }],
                        "usage": last_chunk.get("usage"),
                    }
                    out = "data: " + json.dumps(event, ensure_ascii=False) + "\n\ndata: [DONE]\n"
                    yield out.encode("utf-8")
                else:
                    # No PII: real-time streaming
                    async for chunk in upstream_resp.aiter_bytes():
                        sse_buffer.append(chunk)
                        yield chunk

        print(f"[velum] stream DONE pii={'YES' if pii_key else 'no'} chunks={len(sse_buffer)}")

    return StreamingResponse(generate(), media_type="text/event-stream")
