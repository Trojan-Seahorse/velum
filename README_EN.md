# Velum · LLM PII Firewall

**A transparent proxy that automatically detects and redacts personally identifiable information before it reaches the LLM. Restores PII in responses before returning to the user.**

```
Request:  User → Client → [Velum] → LLM API
           PII intercepted, replaced with anonymous identifiers

Response: User ← Client ← [Velum] ← LLM API
           Identifiers restored to original values
```

## Why

LLM APIs are black boxes — your conversation data is logged, stored, and potentially used for model training. Sending messages containing real names, phone numbers, ID numbers, or addresses is equivalent to handing that data to a third party.

Velum intercepts PII before it leaves your network, replacing it with anonymous identifiers (e.g., `P-00128`, `L-23017`). The LLM never sees the original data. Responses are automatically restored — transparent to the user.

## Key Features

| Feature | Description |
|---------|-------------|
| **Transparent Proxy** | OpenAI-compatible API endpoint. Just change the URL. |
| **Multi-Mode Switch** | In-message `!pii` prefix toggles redaction strategies on the fly. |
| **Per-Type Identifier** | Each entity type uses a distinct prefix (P-person O-org L-loc T-phone...) — LLM can distinguish entity types. |
| **SSE Streaming Restore** | Full DeepSeek `reasoning_content` restoration. |
| **Compound Location Enhancement** | 430+ economic zone names (parks, new districts, etc.) to patch HanLP NER blind spots. |
| **Fail-Open** | Pass-through on error — never blocks service. |
| **Low Footprint** | Only argus-redact + FastAPI. Single container, < 500MB RAM. |

## Prerequisites

| Requirement | Note |
|-------------|------|
| Docker | 24+, with docker compose |
| LLM API | Any OpenAI-compatible API (DMXAPI, OpenAI, etc.) |
| Memory | ≥ 1GB (HanLP model ~400MB, auto-downloaded during build) |
| Network | Container must be able to reach the upstream LLM API |

> Python 3.12 is only needed for development/testing. Deployment uses Docker exclusively — no Python required on the host.

## Deployment

Choose one of the two methods below. **Option 2 is recommended** if you plan to use LLMs through IM clients like WeChat or Telegram.

---

### Option 1: Standalone Docker

Best for direct client connections (e.g., CherryStudio pointing straight to Velum).

**Step 1: Clone the repo**

```bash
git clone https://github.com/Trojan-Seahorse/velum.git
cd velum
```

The repository contains: `Dockerfile` (image build), `main.py` (proxy logic), `requirements.txt` (Python dependencies), `location_names.txt` (compound location names).

**Step 2: Build the image**

```bash
docker build -t velum .
```

During the build, the HanLP Chinese NER model (~400MB) is automatically downloaded. This may take a few minutes depending on network speed.

**Step 3: Start the container**

```bash
docker run -d \
  -p 8000:8000 \
  -e UPSTREAM_URL=https://your-llm-api.com/v1 \
  -e ARGUS_REDACT_PSEUDONYM_SALT=$(openssl rand -hex 16) \
  --name velum \
  velum
```

Parameter reference:

| Parameter | Meaning |
|-----------|---------|
| `-d` | Run in background |
| `-p 8000:8000` | Map container port 8000 to host (change the left number if needed, e.g. `-p 17829:8000`) |
| `-e UPSTREAM_URL=...` | **Required**. Your upstream LLM API URL. Any OpenAI-compatible API works |
| `-e ARGUS_REDACT_PSEUDONYM_SALT=...` | **Required**. Salt for pseudonym mode — use a random string |
| `--name velum` | Container name for easy log access |

> Velum proxies all request headers (including Authorization) to the upstream. It never stores your API key.

**Step 4: Verify**

```bash
curl http://localhost:8000/health
```

Expected response: `{"status":"ok","upstream":"https://...","pii":"ok"}`. If `pii` is not `ok`, see [Troubleshooting](#troubleshooting).

---

### Option 2: Docker Compose (with Hermes Gateway)

Best for using LLMs through IM clients (WeChat, Telegram, etc.). Hermes is a multi-platform agent gateway that routes messages from various channels through Velum.

Full 4-service architecture: `Velum` → `Hermes` → `Dashboard` + `WebUI`.

**Step 1: Prepare directory structure**

```bash
mkdir -p ~/hermes_agent
cd ~/hermes_agent

# Clone Velum as a subdirectory
git clone https://github.com/Trojan-Seahorse/velum.git
```

Resulting structure:

```
~/hermes_agent/
└── velum/           # Cloned Velum repo
    ├── Dockerfile
    ├── main.py
    ├── requirements.txt
    └── location_names.txt
```

**Step 2: Create `docker-compose.yml`**

Create `docker-compose.yml` in `~/hermes_agent/`:

```yaml
services:
  velum:
    build: ./velum
    container_name: velum
    ports:
      - "127.0.0.1:17829:8000"
    volumes:
      - ./velum/location_names.txt:/app/location_names.txt:ro
    environment:
      - PYTHONUNBUFFERED=1
      - UPSTREAM_URL=https://www.dmxapi.cn/v1
      - PII_ENABLED=true
      - ARGUS_REDACT_PSEUDONYM_SALT=your-random-salt-here
    restart: unless-stopped
    mem_limit: 1g
    networks:
      - hermes-net

  hermes:
    image: nousresearch/hermes-agent:latest
    container_name: hermes
    command: ["gateway", "run"]
    volumes:
      - ./hermes:/opt/data
    environment:
      - API_SERVER_ENABLED=true
      - API_SERVER_HOST=0.0.0.0
      - API_SERVER_KEY=your-api-key
    restart: unless-stopped
    mem_limit: 512m
    ports:
      - "17834:8642"
    networks:
      - hermes-net
    depends_on:
      - velum

  dashboard:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-dashboard
    command: ["dashboard", "--host", "0.0.0.0", "--no-open", "--insecure"]
    volumes:
      - ./hermes:/opt/data
    ports:
      - "17832:9119"
    restart: unless-stopped
    mem_limit: 256m
    networks:
      - hermes-net
    depends_on:
      - hermes

  webui:
    image: ghcr.io/nesquena/hermes-webui:latest
    container_name: hermes-webui
    volumes:
      - ./hermes:/home/hermeswebui/.hermes
      - ./workspace:/workspace
    ports:
      - "17833:8787"
    restart: unless-stopped
    mem_limit: 256m
    networks:
      - hermes-net
    depends_on:
      - hermes

networks:
  hermes-net:
    driver: bridge
```

Values to change:

| Setting | Location | Notes |
|---------|----------|-------|
| `UPSTREAM_URL` | velum environment | Your actual LLM API URL |
| `ARGUS_REDACT_PSEUDONYM_SALT` | velum environment | Replace with a random string |
| `API_SERVER_KEY` | hermes environment | Set your own API key |
| Port mappings | Each service's ports | Change the number before `:` if host ports conflict |

**Step 3: Configure Hermes to connect to Velum**

This is a common gotcha — Hermes stores its LLM backend URL in config files, NOT as an environment variable.

Hermes will generate initial config files on first startup. Start it once, then edit:

```bash
# First start to generate config files
docker compose up -d hermes

# Edit Hermes configuration
```

Create or edit `./hermes/config.yaml`:

```yaml
base_url: http://velum:8000/v1
model: deepseek-chat
```

Create or edit `./hermes/auth.json`:

```json
{
  "base_url": "http://velum:8000/v1",
  "api_key": "your-upstream-llm-api-key"
}
```

> **Important**: `velum` in the URL is the Docker service name, not localhost. Containers on the same `hermes-net` network reach each other by service name.

**Step 4: Start all services**

```bash
cd ~/hermes_agent
docker compose up -d
```

On first run, `docker compose build` will automatically build the Velum image (including HanLP model download). This takes 2–5 minutes.

**Step 5: Verify**

```bash
# Velum health check
curl http://localhost:17829/health
# → {"status":"ok","upstream":"https://www.dmxapi.cn/v1","pii":"ok"}

# View Velum logs
docker logs -f velum

# View Hermes logs
docker logs -f hermes
```

**Step 6: Configure Hermes channels**

Hermes Dashboard is at `http://your-nas-ip:17832`. Add WeChat, Telegram, or other channels from the Dashboard. See [Hermes documentation](https://github.com/NousResearch/hermes-agent) for details.

---

### Client Configuration

After deployment, point your LLM client to Velum's address. Use your upstream LLM API key — Velum proxies it, never stores it:

| Client | Configuration |
|--------|--------------|
| CherryStudio | Settings → Model Services → Add Provider → API URL: `http://your-host:17829/v1` |
| Hermes | `config.yaml`: `base_url: http://velum:8000/v1` (already configured above) |
| OpenAI SDK | `OpenAI(base_url="http://your-host:8000/v1", api_key="...")` |
| WeChat | Via Hermes WeChat adapter (scan QR in Dashboard) |

## Daily Use

Prepend `!pii` to your message to **temporarily** switch redaction mode for that message. The next message without the prefix returns to the default strategy. Both full-width `！` and half-width `!` are accepted.

| Command | Effect |
|---------|--------|
| `!pii` | Show firewall status and strategy |
| `!pii debug <text>` | Analyze PII detection without calling the LLM |
| `!pii pseudonym` / `!pii 伪名` | Use realistic fake names (other types still redacted) |
| `!pii org,loc` | Keep organization and location names unredacted |

### Examples

```
User: !pii pseudonym Look up Zhang Wei's contact info
      → This message sent in pseudonym mode. LLM sees fake names.
      → Response restored with real names before display.

User: Now look up Li Na's   ← No prefix — auto-returns to default mode
      → Normal redaction with per-type prefixes
```

```
User: !pii debug Li Ming works at Xiong'an New Area, phone 13900001111

Original: Li Ming works at Xiong'an New Area, phone 13900001111
Redacted: P-47141 works at P-72185, phone T-39281
Entities: 3

Detected:
  [1] person  Li Ming → P-47141
  [2] person  Xiong'an New Area → P-72185
  [3] phone   13900001111 → T-39281

User: Help me summarize   ← Next message — normal conversation
```

> **Note**: Hermes intercepts all `/`-prefixed commands at the gateway level. Velum uses `!pii` prefix — unaffected. Do NOT use `/pii`.

## Redaction Strategies

| Entity Type | Strategy | Example |
|-------------|----------|---------|
| person | remove | Li Ming → P-00128 |
| organization | remove | Acme Corp → O-09502 |
| school | remove | MIT → S-14439 |
| location | remove | Beijing → L-23017 |
| phone | remove | 13900001111 → T-39281 |
| email | remove | a@b.com → E-55612 |
| id_number | remove | 110101... → I-78403 |
| address | remove | 200 Tianfu Ave → A-66194 |
| bank_card | mask | 622202... → ****0123 |
| self_reference | keep | (untouched) |
| date | remove | 2024-03-15 → D-33501 |

## Environment Variables

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `UPSTREAM_URL` | ✅ | — | Upstream LLM API URL, e.g. `https://www.dmxapi.cn/v1` |
| `ARGUS_REDACT_PSEUDONYM_SALT` | ✅ | — | Salt for pseudonym mode. Use a random string; different instances should use different values |
| `PII_ENABLED` | — | `true` | Set to `false` to disable redaction (all messages pass through unchanged) |
| `PYTHONUNBUFFERED` | — | — | Set to `1` for real-time Docker log output (recommended) |

## Architecture

```
main.py (~620 lines)
├── /health                   Health check
├── /v1/models                Model list (proxied upstream)
├── /v1/chat/completions      OpenAI-compatible endpoint
│   ├── get_last_user_content  Extract last user message
│   ├── parse_mode_prefix      Parse !pii mode prefix
│   ├── redact_text            Call argus-redact for PII redaction
│   ├── [Upstream LLM call]
│   ├── restore_text           Restore PII in LLM response
│   └── SSE buffer-restore     DeepSeek streaming response handling
│
└── location_names.txt         Compound location names (430+ entries)
```

### PII Pipeline

```
User Message → parse_mode_prefix() → Mode Detection
             → redact_text() → argus-redact redact()
                   ├─ Layer 1: regex (names parameter injection)
                   └─ Layer 2: HanLP cascaded NER
             → Redacted Message → Upstream LLM
             → LLM Response → restore_text() → Restore PII → User
```

### SSE Streaming

DeepSeek's SSE stream splits identifiers across chunks (e.g., `P-00` + `128`), making per-chunk restoration impossible. Velum uses a **buffer-concat-restore** strategy: cache all SSE chunks → concatenate full text → restore PII → repack as single SSE event.

## argus-redact Engine

Velum delegates all PII detection to argus-redact, which uses a **three-layer progressive architecture**:

| Layer | Mechanism | Coverage |
|-------|-----------|----------|
| **Layer 1: Regex** | Rule-based regex for format-constrained PII (phone numbers, ID numbers, emails, bank cards) + `names` parameter for custom entity injection | Format-fixed PII + custom dictionaries |
| **Layer 2: Cascaded NER** | HanLP 2.x Chinese NER. Recognition order: person → location → organization (cascaded dependency) | Person, location, organization, school, date, etc. |
| **Layer 3: Semantic/LLM** | Reserved interface for context-dependent PII (currently disabled) | — |

### HanLP Model Stack

| Component | Details |
|-----------|---------|
| **Encoder** | ELECTRA-small (12-layer Transformer, ~14M params / 0.014B) |
| **NER Decoder** | Biaffine NER (treats NER as dependency parsing, natively supports nested/flat entities) |
| **Training Data** | MSRA (largest Chinese NER corpus) + OntoNotes 4.0 Chinese |
| **Entity Types** | 56 categories (PER/LOC/ORG/GPE/FAC/VEH/...) |
| **Annotation Scheme** | PKU standard: person(nr) → location(ns) → organization(NT), cascaded labeling |

> **Key insight**: Biaffine NER makes no flat-entity assumption — in `[北京/ns 大学/n]NT`, "北京" is simultaneously an independent location entity AND part of an organization phrase. This span-based approach natively handles compound entities.

## Compound Location Enhancement

HanLP's Chinese NER model has blind spots for economic zones with non-standard administrative suffixes (e.g., "成都天府新区", "雄安新区") — they are classified as neither ORG nor LOC, passing through undetected.

Velum includes 430+ compound location names (national new districts, economic development zones, high-tech zones, free trade zones) injected via argus-redact's `names` parameter at Layer 1 regex matching. The name list is maintained as a plain text file (`location_names.txt`) — no code changes needed to add or remove entries.

## Limitations

1. **`names` entities classified as `person`**: argus-redact's Layer 1 regex bypasses the NER classification pipeline, defaulting to person type. Does not affect privacy — per-type prefixes distinguish entity types, and `detailed=True` exposes type information.
2. **Standard administrative names still rely on NER**: Locations with standard suffixes (e.g., "海淀区卫健委") are covered by HanLP NER, not the `names` list.
3. **Short text NER may fail**: Inputs under 8 characters lack sufficient context for HanLP segmentation.
4. **money entity out of scope**: argus-redact's 56-type catalog does not include money; RMB amounts are not redacted.
5. **Transparent proxy, not encrypted transport**: If your upstream LLM API uses HTTP, messages travel in plaintext on the wire.

## Testing

Two test files are in the `tests/` directory for verifying argus-redact PII detection behavior. Test files are not included in the Docker image — run locally or copy into the container.

```bash
# Local (requires argus-redact[zh])
pip install argus-redact[zh]
python tests/test_custom_dict.py
python tests/test_strategies.py

# Or inside Docker container (copy first)
docker cp tests/test_custom_dict.py velum:/app/
docker exec velum python /app/test_custom_dict.py
```

### Test Environment

| Component | Version / Notes |
|-----------|----------------|
| **Runtime** | Docker 24+ (verified on UGREEN DXP4800, UGOS Pro) |
| **Python** | 3.12-slim |
| **argus-redact** | ≥ 0.5.0 (with HanLP Chinese NER) |
| **Gateway** | Hermes Agent (nousresearch/hermes-agent:latest) |
| **Upstream LLM** | DeepSeek V4 Pro (via DMXAPI) |
| **Memory** | < 500MB (including HanLP model) |

### Suitable For

- ✅ Personal LLM use via IM gateways (WeChat, Telegram, Web)
- ✅ Internal enterprise LLM proxy with unified PII policy
- ✅ Any OpenAI-compatible API upstream
- ⚠️ High-concurrency production needs load balancing (single-instance by default)
- ❌ Environments requiring full SOC2/HIPAA compliance (this is a technical tool, not a certified compliance solution)

## Troubleshooting

### `/health` returns `pii: error`

HanLP model download or loading failed. The model is pre-downloaded during Docker build — if it still fails, it's usually a network issue.

```bash
# Manual warm-up (triggers model download)
docker exec velum python -c "from argus_redact import redact; redact('test', lang='zh')"

# Check container network
docker exec velum python -c "import urllib.request; print(urllib.request.urlopen('https://pypi.org').status)"
```

### `!pii debug` shows missed detections

Short text (< 8 characters) or locations with non-standard suffixes may be missed by NER — a known HanLP limitation.

- **Workaround**: Use `!pii pseudonym` for that message
- **Permanent fix**: Add missed locations to `location_names.txt`, rebuild or restart the container

### Upstream LLM connection timeout

```bash
# Verify environment variable
docker exec velum env | grep UPSTREAM_URL

# Verify container can reach upstream
docker exec velum python -c "
import httpx
r = httpx.get('your_UPSTREAM_URL/models')
print(r.status_code)
"
```

### Hermes not receiving messages

1. Verify `./hermes/config.yaml` and `./hermes/auth.json` both have `base_url: http://velum:8000/v1`
2. Verify the API key is correct
3. Check Hermes logs: `docker logs -f hermes`

### Container OOM

HanLP model is ~400MB, pre-downloaded into the image. Allocate ≥ 1GB at runtime. Already configured with `mem_limit: 1g` in the Docker Compose example. For `docker run`:

```bash
docker run --memory=1g ...
```

## License

[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)

---

[中文文档](README.md)
