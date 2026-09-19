#!/usr/bin/env python3
"""Personal Telegram channel monitor.

Commands:
    fetch    - download new posts from the channels listed in channels.txt
    list     - list posts stored locally in posts.db
    show     - print one stored post in full
    rewrite  - rewrite a stored post with Claude (needs ANTHROPIC_API_KEY)

Secrets (TG_API_ID, TG_API_HASH, ANTHROPIC_API_KEY) are read from a local
.env file (see .env.example) and are never sent anywhere except the
Telegram/Anthropic APIs themselves. Fetched posts, the local database and
the Telegram session file stay on this machine only - see README.md.
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "posts.db"
CHANNELS_PATH = BASE_DIR / "channels.txt"
SESSION_NAME = str(BASE_DIR / "tg_monitor_session")


def load_env(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ (no overwrite)."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def read_channels(path: Path) -> list[str]:
    if not path.exists():
        return []
    channels = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        channels.append(line)
    return channels


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            date TEXT,
            text TEXT,
            rewritten TEXT,
            fetched_at TEXT NOT NULL,
            UNIQUE(channel, message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_state (
            channel TEXT PRIMARY KEY,
            last_message_id INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def require_env(*names: str) -> dict:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        print(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nSet them in your local .env file (see .env.example).",
            file=sys.stderr,
        )
        sys.exit(1)
    return {n: os.environ[n] for n in names}


def cmd_fetch(args: argparse.Namespace) -> None:
    try:
        from telethon.sync import TelegramClient
    except ImportError:
        print(
            "telethon is not installed. Run:\n"
            "  pip install telethon anthropic --break-system-packages",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = require_env("TG_API_ID", "TG_API_HASH")
    api_id = int(creds["TG_API_ID"])
    api_hash = creds["TG_API_HASH"]

    channels = read_channels(CHANNELS_PATH)
    if not channels:
        print(f"No channels found in {CHANNELS_PATH.name}. Add one channel link per line.")
        return

    conn = get_db()

    # First run of TelegramClient() will ask for phone number + SMS code
    # interactively and store the session locally in tg_monitor_session*
    # (gitignored). That login only needs to happen once.
    with TelegramClient(SESSION_NAME, api_id, api_hash) as client:
        for channel in channels:
            cur = conn.execute(
                "SELECT last_message_id FROM channel_state WHERE channel = ?",
                (channel,),
            )
            row = cur.fetchone()
            min_id = row[0] if row else 0

            new_count = 0
            max_id_seen = min_id
            messages = list(
                client.iter_messages(channel, min_id=min_id, limit=args.limit)
            )
            for message in reversed(messages):
                if not message.text:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO posts
                        (channel, message_id, date, text, fetched_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        channel,
                        message.id,
                        message.date.isoformat() if message.date else None,
                        message.text,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                new_count += 1
                max_id_seen = max(max_id_seen, message.id)

            if max_id_seen > min_id:
                conn.execute(
                    """
                    INSERT INTO channel_state (channel, last_message_id)
                    VALUES (?, ?)
                    ON CONFLICT(channel) DO UPDATE SET last_message_id = excluded.last_message_id
                    """,
                    (channel, max_id_seen),
                )
            conn.commit()
            print(f"{channel}: fetched {new_count} new post(s)")

    conn.close()


def cmd_list(args: argparse.Namespace) -> None:
    conn = get_db()
    query = "SELECT id, channel, date, text FROM posts"
    params: list = []
    if args.channel:
        query += " WHERE channel = ?"
        params.append(args.channel)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("No posts stored yet. Run 'fetch' first.")
        return

    for post_id, channel, date, text in rows:
        snippet = (text or "").replace("\n", " ").strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        print(f"[{post_id}] {channel} {date or ''}\n    {snippet}")


def cmd_show(args: argparse.Namespace) -> None:
    conn = get_db()
    row = conn.execute(
        "SELECT id, channel, message_id, date, text, rewritten FROM posts WHERE id = ?",
        (args.id,),
    ).fetchone()
    conn.close()

    if not row:
        print(f"No post with id {args.id}", file=sys.stderr)
        sys.exit(1)

    post_id, channel, message_id, date, text, rewritten = row
    print(f"id: {post_id}")
    print(f"channel: {channel}")
    print(f"message_id: {message_id}")
    print(f"date: {date}")
    print("---")
    print(text)
    if rewritten:
        print("--- rewritten ---")
        print(rewritten)


DEFAULT_REWRITE_PROMPT = (
    "Rewrite the following Telegram post so it is clear and concise, "
    "keeping the original meaning and language. Return only the rewritten text."
)


def cmd_rewrite(args: argparse.Namespace) -> None:
    try:
        import anthropic
    except ImportError:
        print(
            "anthropic is not installed. Run:\n"
            "  pip install telethon anthropic --break-system-packages",
            file=sys.stderr,
        )
        sys.exit(1)

    creds = require_env("ANTHROPIC_API_KEY")

    conn = get_db()
    row = conn.execute("SELECT text FROM posts WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"No post with id {args.id}", file=sys.stderr)
        sys.exit(1)
    text = row[0]

    client = anthropic.Anthropic(api_key=creds["ANTHROPIC_API_KEY"])
    prompt = args.prompt or DEFAULT_REWRITE_PROMPT

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"{prompt}\n\n---\n{text}",
            }
        ],
    )
    rewritten = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    conn.execute("UPDATE posts SET rewritten = ? WHERE id = ?", (rewritten, args.id))
    conn.commit()
    conn.close()

    print(rewritten)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor Telegram channels listed in channels.txt."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="fetch new posts from channels.txt")
    p_fetch.add_argument(
        "--limit", type=int, default=100, help="max messages to scan per channel"
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_list = sub.add_parser("list", help="list stored posts")
    p_list.add_argument("--channel", help="filter by channel")
    p_list.add_argument("--limit", type=int, default=20, help="max posts to show")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one stored post in full")
    p_show.add_argument("id", type=int, help="post id from 'list'")
    p_show.set_defaults(func=cmd_show)

    p_rewrite = sub.add_parser("rewrite", help="rewrite a post with Claude")
    p_rewrite.add_argument("id", type=int, help="post id from 'list'")
    p_rewrite.add_argument("--prompt", help="custom rewrite instruction")
    p_rewrite.set_defaults(func=cmd_rewrite)

    return parser


def main() -> None:
    load_env(ENV_PATH)
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
