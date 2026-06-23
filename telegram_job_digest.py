#!/usr/bin/env python3
"""
Telegram Job Digest
====================
Aggregates job postings from selected Telegram channels since the last run,
filters them by specific keywords, removes duplicates (since the same job is
often cross-posted), and delivers a clean summary via a dedicated Telegram bot.

THE FIRST RUN is interactive: it will prompt you for a confirmation code sent to
your Telegram app (and your 2FA cloud password, if enabled). This process generates
a session file (*.session). All subsequent runs are fully automated and require no
human interaction, making it perfect for cron jobs or automated schedulers like
GitHub Actions.

Dependencies installation:
    pip install telethon python-dotenv
"""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from telethon import TelegramClient
from dotenv import load_dotenv

import asyncio
import hashlib
import json
import re
import sys
import os
import base64

# ───────────────────────── GitHub secrets ─────────────────────────
# Load variables from local .env file
load_dotenv()

# Script reads data from environment variables
# (from .env on local machine, from GitHub Actions secrets in cloud)
API_ID = int(os.environ.get('TG_API_ID'))
API_HASH = os.environ.get('TG_API_HASH')
PHONE = os.environ.get('TG_PHONE')
BOT_TOKEN = os.environ.get('TG_BOT_TOKEN')
TG_USER_ID = int(os.environ.get('TG_USER_ID'))

# Restore Telegram session from GitHub Secrets (for cloud deployment)
if 'TG_SESSION_BASE64' in os.environ:
    with open("job_digest_session.session", "wb") as f:
        f.write(base64.b64decode(os.environ['TG_SESSION_BASE64']))

if 'TG_STATE_BASE64' in os.environ:
    with open("job_digest_state.json", "wb") as f:
        f.write(base64.b64decode(os.environ['TG_STATE_BASE64']))

# ───────────────────────── CONFIGURATION ─────────────────────────
# Channels to monitor - usernames without "@" (for private channels you must
# be a member; for public channels, username works in any case)
CHANNELS = [
    "itvacancykz", #ITvacancy KZ & UZ
    "jtbl_vacancy", #JTBL | Вакансии для IT-специалистов
    "Remoteit", #Remote IT (Inflow)
    "rocket_tech_jobs", #Rocket Jobs: IT-работа в Казахстане и удалённо
    "workitkz", #IT Вакансии Казахстан
    "opento_dev", #Dev Jobs - ✈️ вакансии за рубежом
    "Relocats", #IT Relocation (Inflow)
    "opento_cyprus", #Cyprus Jobs - проверенные вакансии на Кипре
    "jsgurujobs", #SGuruJobs
    "careers_digital", #CC | Вакансии, Работа
    "jsdevjob" #Javascript jobs
]

# Keywords for filtering (case-insensitive substring search)
KEYWORDS = [
    "фронтенд",
    "front",
    "frontend",
    "front-end",
    "angular",
    "react"
]

LOOKBACK_HOURS = 24  # Time window for the very first execution
DEDUP_WORDS = 12  # Number of initial words used for deduplication

SESSION_NAME = "job_digest_session"
STATE_FILE = Path("job_digest_state.json")

# ────────────────────────────────────────────────────────────


def load_state() -> dict:
    """Load the script state from the JSON file."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: dict) -> None:
    """Save the current execution state to the JSON file."""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def matches_keywords(text: str) -> bool:
    """Check if the job posting text contains any configured keywords."""
    if not text:
        return False
    low = text.lower()
    return any(kw.lower() in low for kw in KEYWORDS)

def dedup_key(text: str) -> str:
    """Generate a unique MD5 hash from the first N words of the text."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    words = normalized.with_suffix('').name.split()[:DEDUP_WORDS] if hasattr(normalized, 'with_suffix') else normalized.split()[:DEDUP_WORDS]
    return hashlib.md5(" ".join(words).encode("utf-8")).hexdigest()

def make_chunks(parts: list[str], limit: int = 3900) -> list[str]:
    """Split a list of strings into text blocks within the Telegram character limit."""
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}\n\n{part}" if current else part
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = part
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

async def main() -> None:
    """Execute the core logic: fetch, filter, deduplicate, and dispatch the digest."""
    state = load_state()
    seen_hashes = set(state.get("seen_hashes", []))
    last_run_iso = state.get("last_run")

    # Sync local time with GitHub Actions by forcing UTC
    if last_run_iso:
        since = datetime.fromisoformat(last_run_iso)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start(phone=PHONE)

    found: list[tuple[str, str | None, str]] = []

    for channel in CHANNELS:
        try:
            entity = await client.get_entity(channel)
            # Scan from newest to oldest. Stop when we reach the date of the last run.
            async for message in client.iter_messages(entity, limit=100):
                msg_date = message.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)

                if msg_date <= since:
                    break  # All subsequent posts are older, stop loop for this channel

                text = message.text or ""
                if not matches_keywords(text):
                    continue

                h = dedup_key(text)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                username = getattr(entity, "username", None)
                link = f"https://t.me/{username}/{message.id}" if username else None
                found.append((entity.title or channel, link, text.strip()))
        except Exception as exc:
            print(f"[!] Failed to process channel '{channel}': {exc}", file=sys.stderr)

    # Format local time for report header
    local_time_str = datetime.now(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M')

    if found:
        parts = []
        for title, link, text in found:
            snippet = text if len(text) <= 250 else text[:250].strip() + "…"

            if link:
                header = f"📌 **[{title}]({link})**"
            else:
                header = f"📌 **{title}**"

            parts.append(f"{header}\n{snippet}\n\n───────────────────")

        digest_header = f"🗞 **Job digest for {local_time_str}** — found: {len(found)}\n"

        # Initialize bot and send the formatted text blocks
        bot = TelegramClient('bot_session', API_ID, API_HASH)
        try:
            await bot.start(bot_token=BOT_TOKEN)
            for chunk in make_chunks([digest_header] + parts):
                await bot.send_message(TG_USER_ID, chunk, link_preview=False, parse_mode="md")
        finally:
            await bot.disconnect()
    else:
        bot = TelegramClient('bot_session', API_ID, API_HASH)
        try:
            await bot.start(bot_token=BOT_TOKEN)
            await bot.send_message(
                TG_USER_ID, f"🗞 Job digest for {local_time_str}: nothing found for keywords in this period."
            )
        finally:
            await bot.disconnect()

    # Save execution history for the next sequence
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["seen_hashes"] = list(seen_hashes)[-2000:]
    save_state(state)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())