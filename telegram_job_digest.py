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
    "jsdevjob", #Javascript jobs
    "itjobsgeorgia", #Tech Jobs Georgia
    "it_jobs_georgia", #T-Jobs Georgia | IT вакансии в Грузии
    "jobsearchhhhh", #Вакансии IT (СНГ, ЕС, Весь Мир)
    "frontend_vakansii", #Frontend | Вакансии
    "visa_sponsored_jobss", #Visa sponsored jobs+ resources
    "Pol_relocation", #IT СV: Poland Relocation
    "cyprusithr", #CY iT HR
    "it_vakansii_jobs", #СЕТИ — IT & Digital вакансии
    "rabotafrontend", #FrontEnd Работа
    "WorkingDubai", #РАБОТА В ДУБАЕ | ВАКАНСИИ В ОАЭ
    "jobsarm", #Работа в Армении
    "clickjobsuz", #Click Jobs - IT Jobs
    "it_remote", #Удаленная работа в IT
    "program_job", #Работа для программиста | IT вакансии
    "hot_itjobs", #<HOT IT JOBS>: developers, programmers, sysadmins
    "epamkazakhstan", #EPAM Kazakhstan
    "devkz_jobs", #Dev KZ | Vacancy
    "olgaitvacancies", #Olga IT Vacancies
]

# Keywords for filtering (case-insensitive substring search)
KEYWORDS = [
    "фронтенд",
    "frontend",
    "front-end",
    "angular",
    "ангуляр",
    "typescript",
    "веб-разработчик",
    "web developer",
    "ui developer"
]

# Keywords that mark a post as a candidate's resume/CV rather than a job
# opening. Any message matching one of these is skipped, even if it also
# matches KEYWORDS above (e.g. a resume listing "Frontend" as a skill).
EXCLUDE_KEYWORDS = [
    "#resume",
    "#cv",
    "#резюме",
    "резюме",
    "#ищу",
    "ищу работу",
    "ищу вакансию",
    "ищу проект",
    "в поиске работы",
    "в поиске проекта",
    "seeking a job",
    "looking for a job",
    "open to work",
]

LOOKBACK_HOURS = 24  # Time window for the very first execution
DEDUP_WORDS = 12  # Number of initial words used for deduplication

# Some monitored channels are forum-enabled supergroups where jobs live in
# specific topics alongside unrelated chat, or split across several topics.
# For those, restrict scanning to just the listed topic IDs so off-topic
# chatter never reaches the keyword filters.
CHANNEL_TOPIC_IDS = {
    "pol_relocation": [36109],  # "Vacancies & CVs" topic in IT CV: Poland Relocation
    "cyprusithr": [46685, 46679],  # vacancy topics in CY iT HR (general + Cyprus-based roles)
}

SESSION_NAME = "job_digest_session"
# Shown in Telegram → Settings → Devices, so the session is recognisable as this script
DEVICE_INFO = {
    "device_model": "Jobs Digest (GitHub Actions)",
    "system_version": "python-tg-jobs-digest",
    "app_version": "1.0",
}
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

def contains_any(text: str, keywords: list[str]) -> bool:
    """Check if the text contains any of the given keywords as a whole word."""
    if not text:
        return False
    low = text.lower()
    for kw in keywords:
        # The pattern searches for the entire keyword, preventing it from being matched
        # inside long words or links (e.g., /frontend/ in a URL)
        pattern = r'(?:^|[^a-zA-Z0-9а-яА-ЯёЁ\-])' + re.escape(kw.lower()) + r'(?:$|[^a-zA-Z0-9а-яА-ЯёЁ\-])'
        if re.search(pattern, low):
            return True
    return False

def matches_keywords(text: str) -> bool:
    """Check if the job posting text contains any configured keywords."""
    return contains_any(text, KEYWORDS)

def is_excluded(text: str) -> bool:
    """Check if the text looks like a candidate's resume/CV post rather than a job opening."""
    return contains_any(text, EXCLUDE_KEYWORDS)

def dedup_key(text: str) -> str:
    """Generate a unique MD5 hash from the first N words of the text."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    words = normalized.with_suffix('').name.split()[:DEDUP_WORDS] if hasattr(normalized, 'with_suffix') else normalized.split()[:DEDUP_WORDS]
    return hashlib.md5(" ".join(words).encode("utf-8")).hexdigest()

KEYCAP_DIGITS = {str(d): f"{d}️⃣" for d in range(10)}

def number_emoji(n: int) -> str:
    """Render a positive integer as a sequence of keycap digit emoji (1️⃣, 2️⃣, … 1️⃣0️⃣, …)."""
    return "".join(KEYCAP_DIGITS[d] for d in str(n))

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

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH, **DEVICE_INFO)
    await client.start(phone=PHONE)

    found: list[tuple[str, str | None, str]] = []

    for channel in CHANNELS:
        try:
            entity = await client.get_entity(channel)
            username = getattr(entity, "username", None)
            # None means "no topic restriction" (regular channel or whole forum)
            topic_ids = CHANNEL_TOPIC_IDS.get(channel.lower(), [None])

            for topic_id in topic_ids:
                # Scan from newest to oldest. Stop when we reach the date of the last run.
                async for message in client.iter_messages(entity, limit=100, reply_to=topic_id):
                    msg_date = message.date
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)

                    if msg_date <= since:
                        break  # All subsequent posts are older, stop loop for this topic

                    text = message.text or ""
                    if is_excluded(text):
                        continue
                    if not matches_keywords(text):
                        continue

                    h = dedup_key(text)
                    if h in seen_hashes:
                        continue
                    seen_hashes.add(h)

                    if username and topic_id:
                        link = f"https://t.me/{username}/{topic_id}/{message.id}"
                    elif username:
                        link = f"https://t.me/{username}/{message.id}"
                    else:
                        link = None
                    found.append((entity.title or channel, link, text.strip()))
        except Exception as exc:
            print(f"[!] Failed to process channel '{channel}': {exc}", file=sys.stderr)

    # Format local time for report header
    local_time_str = datetime.now(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M')

    if found:
        item_separator = "▫️" * 13
        job_blocks = []
        for idx, (title, link, text) in enumerate(found, start=1):
            snippet = text if len(text) <= 250 else text[:250].strip() + "…"
            number = number_emoji(idx)

            if link:
                header = f"{number} 📌 <b><a href='{link}'>{title}</a></b>"
            else:
                header = f"{number} 📌 <b>{title}</b>"

            job_blocks.append(f"{header}\n{snippet}")

        # Join blocks with a separator, but skip it after the last one
        parts = [
            block if idx == len(job_blocks) - 1 else f"{block}\n\n{item_separator}"
            for idx, block in enumerate(job_blocks)
        ]

        banner_line = "🗞" + "━" * 20 + "🗞"
        digest_header = (
            f"{banner_line}\n"
            f"<b>JOB DIGEST</b>\n"
            f"{local_time_str} · Найдено: {len(found)}\n"
            f"{banner_line}"
        )

        # Initialize bot and send the formatted text blocks
        bot = TelegramClient('bot_session', API_ID, API_HASH)
        try:
            await bot.start(bot_token=BOT_TOKEN)
            for chunk in make_chunks([digest_header] + parts):
                await bot.send_message(TG_USER_ID, chunk, link_preview=False, parse_mode="html")
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