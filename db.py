"""
Persistent storage layer using Postgres (survives Render redeploys).

Render's free "Web Service" filesystem is wiped on every redeploy. A
Postgres database is a separate, persistent service, so storing messages
there means your inbox history survives redeploys forever.

Connects using the DATABASE_URL environment variable, which Render sets
automatically once you link a Postgres database to this web service.
"""

import os
import psycopg2
import psycopg2.extras
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set - link a Postgres database in Render first.")
    # Render's internal Postgres URLs sometimes start with postgres:// ; psycopg2 wants postgresql://
    url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    # connect_timeout bounds how long a hung/slow connection attempt can block a
    # calling thread (e.g. bulk_worker) - without this, a stalled connection can
    # freeze that thread forever, since it never reaches its own error handling.
    return psycopg2.connect(url, connect_timeout=10)


def init_db():
    """Create tables if they don't exist yet. Safe to call on every startup."""
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL not set - falling back to non-persistent storage.")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            ts TIMESTAMP NOT NULL DEFAULT NOW(),
            direction TEXT NOT NULL,
            contact_number TEXT NOT NULL,
            contact_name TEXT,
            message_type TEXT,
            message_text TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS statuses (
            id SERIAL PRIMARY KEY,
            ts TIMESTAMP NOT NULL DEFAULT NOW(),
            recipient_number TEXT NOT NULL,
            status TEXT,
            error TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bulk_sent_log (
            id SERIAL PRIMARY KEY,
            ts TIMESTAMP NOT NULL DEFAULT NOW(),
            name TEXT,
            phone TEXT NOT NULL,
            amount TEXT,
            date TEXT
        )
    """)
    # Migration: track sent status per-template, not globally, so the same
    # contact can receive multiple different broadcasts (e.g. QR image +
    # video) without one being wrongly skipped because of the other.
    cur.execute("ALTER TABLE bulk_sent_log ADD COLUMN IF NOT EXISTS template_name TEXT")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bulk_sent_phone_template ON bulk_sent_log(phone, template_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_contact ON messages(contact_number)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bulk_sent_phone ON bulk_sent_log(phone)")
    conn.commit()
    cur.close()
    conn.close()


def log_message(direction, contact_number, contact_name, message_type, message_text, ts=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (ts, direction, contact_number, contact_name, message_type, message_text) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (ts or datetime.now(), direction, contact_number, contact_name, message_type, message_text)
    )
    conn.commit()
    cur.close()
    conn.close()


def log_status(recipient_number, status, error, ts=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO statuses (ts, recipient_number, status, error) VALUES (%s, %s, %s, %s)",
        (ts or datetime.now(), recipient_number, status, error)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_conversations():
    """List of contacts with their latest message, newest first."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT ON (contact_number)
            contact_number AS number,
            COALESCE(NULLIF(contact_name, ''), contact_number) AS name,
            message_text AS last_message,
            ts AS last_time
        FROM messages
        ORDER BY contact_number, ts DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    rows = sorted(rows, key=lambda r: r["last_time"], reverse=True)
    for r in rows:
        r["last_time"] = r["last_time"].strftime("%Y-%m-%d %H:%M:%S")
    return rows


def get_messages(number):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT ts AS timestamp, direction, contact_number, contact_name, "
        "message_type, message_text FROM messages WHERE contact_number = %s ORDER BY ts ASC",
        (number,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    for r in rows:
        r["timestamp"] = r["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
    return rows


def load_sent_numbers(template_name=None):
    """(phone, name) pairs already sent a given template - tracked together so
    different contributors sharing one phone number (e.g. family members) are
    each treated as their own recipient, not collapsed into a single 'sent' flag
    for that number. If template_name is omitted, returns pairs sent ANY template."""
    conn = get_conn()
    cur = conn.cursor()
    if template_name:
        cur.execute("SELECT DISTINCT phone, name FROM bulk_sent_log WHERE template_name = %s", (template_name,))
    else:
        cur.execute("SELECT DISTINCT phone, name FROM bulk_sent_log")
    rows = {(r[0], r[1] or "") for r in cur.fetchall()}
    cur.close()
    conn.close()
    return rows


def record_sent(name, phone, amount, date, template_name=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO bulk_sent_log (name, phone, amount, date, template_name) VALUES (%s, %s, %s, %s, %s)",
        (name, phone, amount, date, template_name)
    )
    conn.commit()
    cur.close()
    conn.close()


def get_delivery_breakdown(template_name):
    """For everyone recorded as 'sent' for this template, check what (if any)
    real delivery status webhook ever came back. Numbers with NO status row at
    all are the likely 'silently dropped' ones - accepted by Meta, but never
    confirmed sent/delivered/failed."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT b.name, b.phone,
               (SELECT s.status FROM statuses s
                WHERE s.recipient_number = b.phone
                ORDER BY s.ts DESC LIMIT 1) AS latest_status
        FROM bulk_sent_log b
        WHERE b.template_name = %s
        ORDER BY b.ts ASC
    """, (template_name,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def get_phones_needing_retry(template_name, grace_minutes=60):
    """Phone NUMBERS (not phone+name pairs) that show a real delivery problem for
    this template - explicit failure, or sent long enough ago with zero status
    webhook ever received (likely silent drop). Kept phone-level because Meta's
    status webhooks are keyed by phone number, not by which specific contributor
    on a shared number the status applies to."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT recipient_number FROM statuses WHERE status = 'failed'")
    failed = {r[0] for r in cur.fetchall()}

    cur.execute("""
        SELECT DISTINCT b.phone FROM bulk_sent_log b
        WHERE b.template_name = %s
          AND b.ts < NOW() - INTERVAL '%s minutes'
          AND NOT EXISTS (SELECT 1 FROM statuses s WHERE s.recipient_number = b.phone)
    """, (template_name, grace_minutes))
    silent_drops = {r[0] for r in cur.fetchall()}

    cur.close()
    conn.close()
    return failed | silent_drops


def get_recently_failed_numbers(hours=72):
    """Phone numbers with a real delivery FAILURE reported via the status webhook
    in the last N hours. A send can return success immediately (queued) and then
    genuinely fail later (e.g. daily limit hit mid-batch) - those numbers should
    NOT be treated as 'already sent' or they'd be skipped forever on retry."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT recipient_number FROM statuses "
        "WHERE status = 'failed' AND ts > NOW() - INTERVAL '%s hours'",
        (hours,)
    )
    rows = {r[0] for r in cur.fetchall()}
    cur.close()
    conn.close()
    return rows


def get_incoming_texts():
    """All incoming (direction='in') messages, oldest first, for RSVP tallying."""
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT contact_number AS number,
               COALESCE(NULLIF(contact_name, ''), contact_number) AS name,
               message_text AS text,
               ts
        FROM messages
        WHERE direction = 'in'
        ORDER BY ts ASC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
