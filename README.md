# Substack to Kindle

Sends Substack newsletters to your Kindle as EPUBs with generated cover images.

## How it works

1. Fetches emails from Gmail (direct Substack or forwarded)
2. Extracts article content from `div.body.markup`, discards all chrome
3. Downloads images, generates a cover (title + first image + author)
4. Converts to EPUB via Calibre, sends to Kindle

## Setup

### Prerequisites

1. **Gmail** with [App Password](https://myaccount.google.com/apppasswords) (2FA required)
2. **Kindle email** [whitelisted](https://www.amazon.com/gp/help/customer/display.html?nodeId=GX9XLEVV8G4DB28H) to accept from your Gmail

### Local development

```bash
brew install calibre
uv sync
cp .env.example .env  # fill in credentials
```

### Docker

```bash
docker build -t substack-kindle .
docker run -d --restart unless-stopped --env-file .env substack-kindle
```

## Usage

```bash
# Poll for unseen emails forever, send to Kindle
uv run processor.py --daemon --kindle

# Poll without sending to Kindle (save EPUBs locally)
uv run processor.py --daemon

# Process unseen emails once, then exit
uv run processor.py --unseen --kindle

# Fetch last 3 emails
uv run processor.py -n 3

# Fetch emails since date
uv run processor.py --since 2025-01-01
```

**Flags:**
- `--daemon` — run forever, polling for unseen emails
- `--unseen` — one-shot, process only unseen emails
- `-n N` — limit to N most recent emails
- `--since DATE` — only emails after YYYY-MM-DD
- `--kindle` — send EPUBs to Kindle
- `--output-dir DIR` — EPUB output directory (default: ./epubs)

## Configuration (.env)

| Variable | Description |
|----------|-------------|
| `EMAIL_ADDRESS` | Gmail address |
| `EMAIL_PASSWORD` | Gmail [App Password](https://myaccount.google.com/apppasswords) |
| `KINDLE_EMAIL` | Your Kindle email |
| `CHECK_INTERVAL` | Seconds between checks (default: 300) |
