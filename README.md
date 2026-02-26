# Close Comments – Facebook / Meta Graph API

> **OBS: Detta skript är otestat och har inte körts mot Meta Graph API i produktion. Verifiera noggrant med `--dry-run` innan du kör skarpt.**

CLI-verktyg som stänger kommentarsfältet (`is_comment_enabled=false`) på gamla Facebook-sideinlägg via Meta Graph API.

## Funktioner

- Läser token och konfiguration från `.env`
- Stödjer en enskild sida, flera sidor, eller alla sidor som token har tillgång till
- Filtrerar inlägg efter ålder (`--older-than-days`) eller datumintervall (`--since` / `--until`)
- Dry-run-läge som bara listar vad som skulle ändras
- Automatisk paginering av inlägg
- Retry med exponential backoff vid rate limits och temporära fel
- Genererar JSON- eller CSV-rapport per körning

## Förutsättningar

### Python

Python 3.8+

```bash
pip install -r requirements.txt
```

### Meta-behörigheter

Token måste ha följande behörigheter:

| Behörighet | Syfte |
|---|---|
| `pages_manage_posts` | Uppdatera `is_comment_enabled` |
| `pages_read_user_content` | Lista inlägg |
| `pages_read_engagement` | Läsa inläggsfält |

Användaren/systemanvändaren som token tillhör måste ha en administratörs- eller redaktörsroll på sidan.

## Konfiguration

Kopiera `.env.example` till `.env` och fyll i:

```bash
cp .env.example .env
```

```env
META_GRAPH_VERSION=v22.0
PAGE_ACCESS_TOKEN=EAAxxxx...
# eller
# USER_ACCESS_TOKEN=EAAxxxx...
# DEFAULT_PAGE_ID=123456789
```

- **`PAGE_ACCESS_TOKEN`** – sidspecifik token (räcker om du bara hanterar en sida)
- **`USER_ACCESS_TOKEN`** – användar-/systemanvändartoken (krävs för `--scope all`)
- **`DEFAULT_PAGE_ID`** – valfritt fallback-sid-ID

## Användning

```
python close_comments.py [flaggor]
```

### Flaggor

| Flagga | Beskrivning | Default |
|---|---|---|
| `--scope all\|page` | `all` = alla sidor via `/me/accounts`; `page` = specifika sidor | `page` |
| `--page-id ID` | Sid-ID (när `--scope page`) | — |
| `--page-ids ID,ID,...` | Kommaseparerade sid-ID:n | — |
| `--older-than-days N` | Stäng kommentarer på inlägg äldre än N dagar | `365` |
| `--since YYYY-MM-DD` | Startdatum (ersätter `--older-than-days`) | — |
| `--until YYYY-MM-DD` | Slutdatum (ersätter `--older-than-days`) | — |
| `--dry-run` | Bara lista; inga ändringar | `false` |
| `--limit N` | Max antal inlägg att läsa per sida | obegränsat |
| `--max-updates N` | Max antal inlägg att uppdatera per sida | obegränsat |
| `--sleep-ms N` | Paus mellan API-anrop (ms) | `200` |
| `--output PATH` | Sökväg för rapport (`.json` eller `.csv`) | auto |
| `--log-json` | Logga i JSON-format | `false` |
| `-v, --verbose` | Debug-loggning | `false` |

### Prioritet för tidsfilter

Om `--since` och/eller `--until` anges används dessa **istället för** `--older-than-days`.

## Exempelkörningar

### 1. Dry-run: visa inlägg äldre än 365 dagar för en sida

```bash
python close_comments.py \
  --scope page \
  --page-id 123456789 \
  --older-than-days 365 \
  --dry-run
```

### 2. Stäng kommentarer på alla sidor

```bash
python close_comments.py \
  --scope all \
  --older-than-days 365
```

### 3. Inlägg inom ett datumintervall

```bash
python close_comments.py \
  --scope page \
  --page-id 123456789 \
  --since 2024-01-01 \
  --until 2024-12-31
```

### 4. Begränsa antal uppdateringar och spara CSV-rapport

```bash
python close_comments.py \
  --scope page \
  --page-id 123456789 \
  --older-than-days 180 \
  --max-updates 50 \
  --output report.csv
```

### 5. Flera sidor

```bash
python close_comments.py \
  --scope page \
  --page-ids 111111,222222,333333 \
  --older-than-days 365
```

## Rapport

Varje körning genererar en rapport (JSON eller CSV) med fält:

| Fält | Beskrivning |
|---|---|
| `page_id` | Sidans ID |
| `page_name` | Sidans namn |
| `post_id` | Inläggets ID |
| `created_time` | Publiceringsdatum (ISO 8601) |
| `permalink` | Länk till inlägget |
| `message_preview` | Första 100 tecken av inläggstext |
| `status` | `updated`, `dry_run`, `error`, `unexpected_response` |

## Felhantering

- HTTP 429 / 5xx → automatisk retry med exponential backoff (upp till 5 försök)
- Graph API rate-limit-felkoder (4, 17, 32, 613) → samma backoff
- Saknade behörigheter loggas tydligt med felmeddelande och `fbtrace_id`
- Exitkod `2` om det finns fel bland uppdateringarna

## Projektstruktur

```
close_comments/
├── close_comments.py   # CLI-huvudskript
├── meta_client.py      # HTTP-klient med retry + paginering
├── .env.example        # Mall för konfiguration
├── .gitignore
├── requirements.txt
└── README.md
```
