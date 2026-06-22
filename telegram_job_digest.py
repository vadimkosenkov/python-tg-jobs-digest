#!/usr/bin/env python3
"""
Telegram Job Digest
====================
Собирает посты из выбранных каналов за период с последнего запуска,
фильтрует по ключевым словам, убирает дубли (одна и та же вакансия часто
постится в нескольких каналах) и присылает сводку в "Избранное" (Saved Messages).

ПЕРВЫЙ ЗАПУСК интерактивный: попросит код подтверждения из приложения Telegram
(и пароль 2FA, если он у тебя включён). После этого создастся файл сессии
(*.session), и все следующие запуски — уже без участия человека, можно вешать
на cron / планировщик.

Установка зависимости:
    pip install telethon / pip3 install telethon
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
# Загружаем переменные из локального файла .env
load_dotenv()

# Скрипт берет данные из виртуального окружения
# (на ПК — из .env, на GitHub Actions — из секретов репозитория)
API_ID = int(os.environ.get('TG_API_ID'))
API_HASH = os.environ.get('TG_API_HASH')
PHONE = os.environ.get('TG_PHONE')

# Восстанавливаем сессию из секретов GitHub (если мы в облаке)
if 'TG_SESSION_BASE64' in os.environ:
    with open("job_digest_session.session", "wb") as f:
        f.write(base64.b64decode(os.environ['TG_SESSION_BASE64']))

if 'TG_STATE_BASE64' in os.environ:
    with open("job_digest_state.json", "wb") as f:
        f.write(base64.b64decode(os.environ['TG_STATE_BASE64']))

# ───────────────────────── НАСТРОЙКИ ─────────────────────────
# Каналы для мониторинга — юзернеймы без "@" (для приватных каналов нужно
# быть участником; для публичных — username работает в любом случае)
CHANNELS = [
    "itvacancykz",
    "jtbl_vacancy",
    "Remoteit",
    "rocket_tech_jobs",
    "workitkz",
    "opento_dev",
    "Relocats",
    "opento_cyprus",
    "jsgurujobs",
]

# Ключевые слова, без учёта регистра. Поиск подстрокой, так что "python"
# поймает и "Python developer", и "опыт работы с Python" и т.п.
KEYWORDS = [
    "фронтенд",
    "front",
    "frontend",
    "front-end",
    "angular",
]

# Сколько часов назад смотреть при самом первом запуске.
# Дальше скрипт сам помнит, докуда дочитал (файл состояния).
LOOKBACK_HOURS = 24

# Сколько первых слов поста брать для дедупликации одинаковых вакансий
# из разных каналов
DEDUP_WORDS = 12

SESSION_NAME = "job_digest_session"
STATE_FILE = Path("job_digest_state.json")

# ────────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def matches_keywords(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(kw.lower() in low for kw in KEYWORDS)

def dedup_key(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    words = normalized.with_suffix('').name.split()[:DEDUP_WORDS] if hasattr(normalized, 'with_suffix') else normalized.split()[:DEDUP_WORDS]
    return hashlib.md5(" ".join(words).encode("utf-8")).hexdigest()

def make_chunks(parts: list[str], limit: int = 3900) -> list[str]:
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
    state = load_state()
    seen_hashes = set(state.get("seen_hashes", []))
    last_run_iso = state.get("last_run")

    # Всегда работаем в UTC, чтобы время на ПК и на серверах GitHub совпадало
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
            # Сканируем от самых свежих к старым. Как только дойдем до даты прошлого запуска — выходим.
            async for message in client.iter_messages(entity, limit=100):
                msg_date = message.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)

                if msg_date <= since:
                    break  # Все последующие посты еще старее, прекращаем цикл для этого канала

                text = message.text or ""
                if not matches_keywords(text):
                    continue

                h = dedup_key(text)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                username = getattr(entity, "username", None)
                link = f"https://t.me{username}/{message.id}" if username else None
                found.append((entity.title or channel, link, text.strip()))
        except Exception as exc:
            print(f"[!] Не получилось обработать канал '{channel}': {exc}", file=sys.stderr)

    # Форматируем красивую локальную дату для сообщения
    local_time_str = datetime.now(timezone.utc).astimezone().strftime('%d.%m.%Y %H:%M')

    if found:
        parts = []
        for title, link, text in found:
            snippet = text if len(text) <= 600 else text[:600] + "…"
            header = f"📌 {title}" + (f" — {link}" if link else "")
            parts.append(f"{header}\n{snippet}")

        digest_header = f"🗞 Дайджест вакансий за {local_time_str} — найдено: {len(found)}"
        for chunk in make_chunks([digest_header] + parts):
            await client.send_message("me", chunk, link_preview=False)
    else:
        await client.send_message(
            "me", f"🗞 Дайджест вакансий за {local_time_str}: за этот период ничего по ключевым словам не нашлось."
        )

    # Сохраняем состояние для следующего запуска
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["seen_hashes"] = list(seen_hashes)[-2000:]
    save_state(state)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())