"""Re-create the user session file and print it as base64 for the GitHub secret."""
import asyncio
import base64
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

SESSION_NAME = "job_digest_session"
# Keep in sync with DEVICE_INFO in telegram_job_digest.py
DEVICE_INFO = {
    "device_model": "Jobs Digest (GitHub Actions)",
    "system_version": "python-tg-jobs-digest",
    "app_version": "1.0",
}


async def main() -> None:
    client = TelegramClient(SESSION_NAME, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"], **DEVICE_INFO)
    await client.start(phone=os.environ["TG_PHONE"])
    me = await client.get_me()
    print(f"\nAuthorized as id={me.id} username={me.username}")
    await client.disconnect()

    with open(f"{SESSION_NAME}.session", "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode()
    with open("session_base64.txt", "w") as fh:
        fh.write(encoded)
    print("session_base64.txt updated — put its contents into the TG_SESSION_BASE64 secret")


if __name__ == "__main__":
    asyncio.run(main())
