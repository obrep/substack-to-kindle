#!/usr/bin/env python3
"""
Substack to Kindle Processor

Fetches Substack newsletters from Gmail, cleans HTML, converts to EPUB,
and optionally sends to Kindle.

Usage:
    python processor.py --daemon                 # poll for unseen emails forever
    python processor.py --daemon --kindle        # poll and send to Kindle
    python processor.py --unseen                 # process unseen emails once
    python processor.py -n 3 --kindle            # fetch last 3, send to Kindle
    python processor.py --since 2025-01-01       # fetch emails since date
"""

import argparse
import email
import hashlib
import imaplib
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================


@dataclass
class Config:
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    email_address: str = ""
    email_password: str = ""
    kindle_email: str = ""
    check_interval: int = 300
    epub_dir: Path = Path("/data/epubs")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            imap_host=os.environ.get("IMAP_HOST", "imap.gmail.com"),
            imap_port=int(os.environ.get("IMAP_PORT", "993")),
            smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            email_address=os.environ.get("EMAIL_ADDRESS", ""),
            email_password=os.environ.get("EMAIL_PASSWORD", ""),
            kindle_email=os.environ.get("KINDLE_EMAIL", ""),
            check_interval=int(os.environ.get("CHECK_INTERVAL", "300")),
            epub_dir=Path(os.environ.get("EPUB_DIR", "/data/epubs")),
        )


# =============================================================================
# HTML CLEANING
# =============================================================================


class SubstackCleaner:
    """Cleans Substack newsletter HTML for e-reader consumption.

    Uses structural CSS class targeting — Substack emails consistently
    wrap article content in div.body.markup.
    """

    def __init__(self, html: str):
        self.soup = BeautifulSoup(html, "html.parser")

    def remove_styles_and_scripts(self) -> "SubstackCleaner":
        for tag in self.soup.find_all(["style", "script", "meta", "link"]):
            tag.decompose()
        return self

    def extract_content(self) -> "SubstackCleaner":
        body_divs = self.soup.find_all("div", class_="body")
        if body_divs:
            # Paywall emails have multiple div.body.markup — pick the largest
            content_div = max(body_divs, key=lambda d: len(d.get_text(strip=True)))
            self.soup = BeautifulSoup(str(content_div), "html.parser")
        return self

    def simplify_images(self) -> "SubstackCleaner":
        for img in list(self.soup.find_all("img")):
            src = img.get("src", "")
            alt = img.get("alt", "")
            style = img.get("style", "")
            if "height:1px" in style or "width:1px" in style:
                img.decompose()
                continue
            img.attrs = {}
            if src:
                img["src"] = src.split("?")[0] if "substackcdn" not in src else src
            if alt:
                img["alt"] = alt
            img["style"] = "max-width: 100%; height: auto;"
        return self

    def strip_hyperlinks(self) -> "SubstackCleaner":
        for a in list(self.soup.find_all("a")):
            if a.parent is None:
                continue
            a.unwrap()
        return self

    def clean(self) -> "SubstackCleaner":
        return (
            self.remove_styles_and_scripts()
            .extract_content()
            .simplify_images()
            .strip_hyperlinks()
        )

    def get_clean_html(self, title: str = "Newsletter") -> str:
        body_content = (
            self.soup.body.decode_contents() if self.soup.body else str(self.soup)
        )
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
        body {{ font-family: Georgia, serif; line-height: 1.6; margin: 2em; max-width: 40em; }}
        img {{ max-width: 100%; height: auto; display: block; margin: 1em 0; }}
        blockquote {{ border-left: 3px solid #ccc; margin-left: 0; padding-left: 1em; color: #444; font-style: italic; }}
        pre, code {{ font-family: monospace; background: #f5f5f5; padding: 0.2em; }}
        pre {{ padding: 1em; overflow-x: auto; white-space: pre-wrap; }}
        h1, h2, h3 {{ line-height: 1.3; }}
        hr {{ border: none; border-top: 1px solid #ccc; margin: 2em 0; }}
    </style>
</head>
<body>
{body_content}
</body>
</html>"""


def clean_substack_html(html: str, title: str = "Newsletter") -> str:
    cleaner = SubstackCleaner(html)
    cleaner.clean()
    return cleaner.get_clean_html(title)


# =============================================================================
# EMAIL PARSING
# =============================================================================


@dataclass
class ParsedEmail:
    subject: str
    author: str
    html_content: str
    date: datetime
    message_id: str


def parse_email_message(msg) -> ParsedEmail:
    """Extract content from an email message object."""
    # Parse subject
    subject = ""
    for header in email.header.decode_header(msg["Subject"] or ""):
        if isinstance(header[0], bytes):
            subject += header[0].decode(header[1] or "utf-8", errors="replace")
        else:
            subject += str(header[0])
    subject = re.sub(r"^Fw:\s*", "", subject)

    # Parse author
    from_header = msg["From"] or ""
    author = "Unknown"
    refs = msg.get("References", "") or ""
    original_from = ""
    if "substack" in refs.lower() and "substack" not in from_header.lower():
        original_from = msg.get("X-Original-From", "") or ""
    if from_header and not original_from:
        original_from = from_header
    if original_from:
        match = re.match(r"(.+?)\s*<", original_from)
        if match:
            author = match.group(1).strip().strip('"')
        else:
            author = from_header.split("@")[0]

    # Parse date
    date = datetime.now()
    if msg["Date"]:
        try:
            date = email.utils.parsedate_to_datetime(msg["Date"])
        except Exception:
            pass

    # Extract HTML content
    html_content = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                html_content = payload.decode(charset, errors="replace")
                break
        if not html_content:
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    html_content = f"<html><body><pre>{text}</pre></body></html>"
                    break
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        if msg.get_content_type() == "text/html":
            html_content = payload.decode(charset, errors="replace")
        else:
            text = payload.decode(charset, errors="replace")
            html_content = f"<html><body><pre>{text}</pre></body></html>"

    # Unwrap Proton Mail forwarded emails
    if html_content and "protonmail_quote" in html_content:
        soup = BeautifulSoup(html_content, "html.parser")
        quote_div = soup.find("div", class_="protonmail_quote")
        if quote_div:
            header_text = ""
            for child in quote_div.children:
                if hasattr(child, "name") and child.name == "blockquote":
                    break
                if isinstance(child, str):
                    header_text += child
                elif hasattr(child, "get_text"):
                    header_text += child.get_text()
            from_match = re.search(r"From:\s*(.+?)(?:\s*<|$)", header_text, re.M)
            if from_match:
                author = from_match.group(1).strip()

            bq = quote_div.find("blockquote")
            if bq:
                html_content = str(bq)

    message_id = msg["Message-ID"] or str(hash(subject + str(date)))

    return ParsedEmail(
        subject=subject,
        author=author,
        html_content=html_content,
        date=date,
        message_id=message_id,
    )


# =============================================================================
# CONVERSION
# =============================================================================

CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def download_images(html: str, image_dir: Path, html_dir: Path):
    """Download remote images to local files and rewrite src to relative paths.
    Returns (modified_html, first_image_path_or_None)."""
    image_dir.mkdir(parents=True, exist_ok=True)
    soup = BeautifulSoup(html, "html.parser")
    rel_path = os.path.relpath(image_dir, html_dir)
    first_image = None

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or not src.startswith("http"):
            continue

        style = img.get("style", "")
        if "height:1px" in style or "width:1px" in style:
            img.decompose()
            continue

        url_hash = hashlib.md5(src.encode()).hexdigest()[:12]
        try:
            req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
            ext = CONTENT_TYPE_TO_EXT.get(content_type, ".jpg")
            local_name = f"{url_hash}{ext}"
            local_path = image_dir / local_name
            local_path.write_bytes(data)
            img["src"] = f"{rel_path}/{local_name}"
            if first_image is None:
                first_image = local_path
            logger.info(f"Downloaded image: {local_name} ({len(data)} bytes)")
        except Exception as e:
            logger.warning(f"Failed to download image: {e}")
            img.decompose()

    return str(soup), first_image


def generate_cover(title: str, author: str, article_image: Path, output_path: Path):
    """Generate a cover image with title (top), article image (middle), author (bottom)."""
    WIDTH, HEIGHT = 1200, 1600
    MARGIN = 60
    TEXT_AREA_W = WIDTH - 2 * MARGIN

    cover = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
    draw = ImageDraw.Draw(cover)

    # Try to load a good font, fall back to default
    font_title = ImageFont.load_default(size=52)
    font_author = ImageFont.load_default(size=36)

    # Draw title at top — word-wrap manually
    def wrap_text(text, font, max_width):
        words = text.split()
        lines = []
        line = ""
        for word in words:
            test = f"{line} {word}".strip()
            if draw.textlength(test, font=font) <= max_width:
                line = test
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines

    MAX_TITLE_LINES = 3
    title_lines = wrap_text(title, font_title, TEXT_AREA_W)
    if len(title_lines) > MAX_TITLE_LINES:
        title_lines = title_lines[:MAX_TITLE_LINES]
        title_lines[-1] = (
            title_lines[-1].rsplit(" ", 1)[0] + "..."
            if " " in title_lines[-1]
            else title_lines[-1] + "..."
        )
    title_y = MARGIN
    for line in title_lines:
        line_w = draw.textlength(line, font=font_title)
        draw.text(
            ((WIDTH - line_w) / 2, title_y), line, fill="#1a1a1a", font=font_title
        )
        title_y += 64
    title_y += 20

    # Draw author at bottom
    author_lines = wrap_text(author, font_author, TEXT_AREA_W)
    author_h = len(author_lines) * 48
    author_y = HEIGHT - MARGIN - author_h
    for line in author_lines:
        line_w = draw.textlength(line, font=font_author)
        draw.text(
            ((WIDTH - line_w) / 2, author_y), line, fill="#555555", font=font_author
        )
        author_y += 48

    # Place article image in the middle area
    img_top = title_y + 20
    img_bottom = HEIGHT - MARGIN - author_h - 40
    img_area_h = img_bottom - img_top
    if img_area_h > 100 and article_image and article_image.exists():
        try:
            img = Image.open(article_image)
            img.thumbnail((TEXT_AREA_W, img_area_h), Image.LANCZOS)
            x = (WIDTH - img.width) // 2
            y = img_top + (img_area_h - img.height) // 2
            cover.paste(img, (x, y))
        except Exception as e:
            logger.warning(f"Failed to add image to cover: {e}")

    cover.save(str(output_path), "JPEG", quality=90)
    return output_path


def convert_html_to_epub(
    html_path: Path,
    epub_path: Path,
    title: str,
    author: str,
    pubdate: datetime = None,
    cover: Path = None,
) -> bool:
    """Convert HTML to EPUB using Calibre's ebook-convert."""
    cmd = [
        "ebook-convert",
        str(html_path),
        str(epub_path),
        "--title",
        title,
        "--authors",
        author,
        "--language",
        "en",
        "--epub-inline-toc",
    ]
    if cover:
        cmd += ["--cover", str(cover)]
    else:
        cmd += ["--no-default-epub-cover"]
    if pubdate:
        cmd += ["--pubdate", pubdate.strftime("%Y-%m-%dT%H:%M:%S")]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.error(f"Conversion failed: {result.stderr}")
        return False

    logger.info(f"Created EPUB: {epub_path}")
    return True


# =============================================================================
# KINDLE SENDING
# =============================================================================


def send_to_kindle(config: Config, epub_path: Path, title: str) -> bool:
    msg = MIMEMultipart()
    msg["From"] = config.email_address
    msg["To"] = config.kindle_email
    msg["Subject"] = title
    msg.attach(MIMEText(f"Newsletter: {title}", "plain"))

    with open(epub_path, "rb") as f:
        part = MIMEBase("application", "epub+zip")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition", f'attachment; filename="{epub_path.name}"'
        )
        msg.attach(part)

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
            server.starttls()
            server.login(config.email_address, config.email_password)
            server.send_message(msg)
        logger.info(f"Sent to Kindle: {title}")
        return True
    except Exception as e:
        logger.error(f"Failed to send: {e}")
        return False


# =============================================================================
# IMAP HELPERS
# =============================================================================


def find_emails(mail, since=None, unseen_only=False):
    """Search for direct Substack emails and forwarded emails."""
    filters = ""
    if since:
        filters += f" SINCE {since.strftime('%d-%b-%Y')}"
    if unseen_only:
        filters += " UNSEEN"

    all_ids = set()
    _, direct = mail.search(None, f'(FROM "@substack.com"{filters})')
    if direct[0]:
        all_ids.update(direct[0].split())

    # Also search forwarded emails (Fw: prefix) — will filter by References header later
    _, forwarded = mail.search(None, f'(SUBJECT "Fw:"{filters})')
    if forwarded[0]:
        all_ids.update(forwarded[0].split())

    return sorted(all_ids, key=lambda x: int(x))


def is_substack_email(msg) -> bool:
    """Check if email is a Substack newsletter (not a receipt or other transactional email)."""
    from_header = (msg["From"] or "").lower()
    refs = (msg.get("References", "") or "").lower()
    subject = (msg["Subject"] or "").lower()

    if "substack" not in from_header and "substack" not in refs:
        return False

    # Filter out transactional emails
    if "your payment receipt" in subject:
        return False

    return True


def make_filename(parsed: ParsedEmail) -> str:
    safe_title = re.sub(r"[^\w\s-]", "", parsed.subject)[:50].strip()
    return safe_title


def process_to_epub(parsed: ParsedEmail, output_dir: Path):
    """Clean, download images, convert to EPUB. Returns EPUB path or None."""
    if not parsed.html_content:
        return None

    clean_html = clean_substack_html(parsed.html_content, parsed.subject)
    filename_base = make_filename(parsed)

    # Download images
    image_dir = output_dir / f"{filename_base}_images"
    clean_html, cover_image = download_images(clean_html, image_dir, output_dir)

    # Write HTML for Calibre
    html_path = output_dir / f"{filename_base}.html"
    html_path.write_text(clean_html, encoding="utf-8")

    # Generate cover
    cover_path = output_dir / f"{filename_base}_cover.jpg"
    generate_cover(parsed.subject, parsed.author, cover_image, cover_path)

    # Convert to EPUB
    epub_path = output_dir / f"{filename_base}.epub"
    success = convert_html_to_epub(
        html_path, epub_path, parsed.subject, parsed.author, parsed.date, cover_path
    )

    # Clean up
    html_path.unlink(missing_ok=True)
    cover_path.unlink(missing_ok=True)
    shutil.rmtree(image_dir, ignore_errors=True)

    return epub_path if success else None


# =============================================================================
# MAIN
# =============================================================================


def fetch_and_process(
    config: Config,
    output_dir: Path,
    since=None,
    limit=None,
    kindle=False,
    unseen_only=False,
):
    """Fetch emails from Gmail, convert to EPUB, optionally send to Kindle.
    Marks successfully processed emails as read."""
    output_dir.mkdir(parents=True, exist_ok=True)

    mail = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
    mail.login(config.email_address, config.email_password)
    mail.select("INBOX")

    email_ids = find_emails(mail, since, unseen_only)
    if not email_ids:
        logger.info("No emails to process")
        mail.logout()
        return

    if limit:
        email_ids = email_ids[-limit:]

    logger.info(f"Found {len(email_ids)} email(s) to process")

    for eid in email_ids:
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        if not is_substack_email(msg):
            continue

        try:
            parsed = parse_email_message(msg)
            logger.info(f"Processing: {parsed.subject} ({parsed.author})")

            epub_path = process_to_epub(parsed, output_dir)
            if epub_path:
                logger.info(f"  Created: {epub_path}")
                if kindle and config.kindle_email:
                    if send_to_kindle(config, epub_path, parsed.subject):
                        logger.info(f"  Sent to Kindle")
                # Mark as read so we don't process again
                mail.store(eid, "+FLAGS", "\\Seen")
            else:
                logger.info("  Skipped: no content")
        except Exception as e:
            logger.error(f"  Error: {e}")

    mail.logout()


def main():
    parser = argparse.ArgumentParser(
        description="Substack to Kindle processor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python processor.py --daemon                 # poll for unseen emails forever
  python processor.py --daemon --kindle        # poll and send to Kindle
  python processor.py --unseen                 # process unseen emails once
  python processor.py --unseen --kindle        # process unseen, send to Kindle
  python processor.py -n 3                     # fetch last 3 emails
  python processor.py --since 2025-01-01       # fetch emails since date
""",
    )
    parser.add_argument(
        "-n", "--limit", type=int, help="Only process the N most recent emails"
    )
    parser.add_argument(
        "--since", help="Only process emails after this date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--unseen", action="store_true", help="Only process unseen emails"
    )
    parser.add_argument("--kindle", action="store_true", help="Send EPUBs to Kindle")
    parser.add_argument(
        "--output-dir", default="./epubs", help="Output directory (default: ./epubs)"
    )
    parser.add_argument(
        "--daemon", action="store_true", help="Run as daemon polling for unseen emails"
    )
    args = parser.parse_args()

    # Require at least one fetch mode
    if not args.daemon and not args.unseen and not args.since and not args.limit:
        parser.error("Specify a mode: --daemon, --unseen, --since DATE, or -n N")

    # Shared setup for fetch/daemon
    config = Config.from_env()
    if not config.email_address or not config.email_password:
        print(
            "ERROR: EMAIL_ADDRESS and EMAIL_PASSWORD required in .env", file=sys.stderr
        )
        sys.exit(1)

    if args.kindle and not config.kindle_email:
        print("ERROR: KINDLE_EMAIL required in .env for --kindle", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir)
    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None

    # Mode 2: Daemon — poll for unseen emails forever
    if args.daemon:
        logger.info(
            f"Starting daemon, checking every {config.check_interval}s, kindle={args.kindle}"
        )
        while True:
            try:
                fetch_and_process(
                    config,
                    output_dir,
                    since=since,
                    limit=args.limit,
                    kindle=args.kindle,
                    unseen_only=True,
                )
            except Exception as e:
                logger.error(f"Error: {e}")
            time.sleep(config.check_interval)

    # Mode 3: One-shot fetch
    else:
        fetch_and_process(
            config,
            output_dir,
            since=since,
            limit=args.limit,
            kindle=args.kindle,
            unseen_only=args.unseen,
        )
        print(f"\nDone! EPUBs saved to: {output_dir}")


if __name__ == "__main__":
    main()
