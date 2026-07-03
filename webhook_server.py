"""
FLASH Charities - WhatsApp Webhook Receiver
------------------------------------------------
This is a small always-on web server that Meta will send events to whenever:
- Someone replies to your WhatsApp messages
- A message's delivery status changes (sent -> delivered -> read -> failed)

It logs everything to simple CSV files you can open in Excel anytime, and
also gives you a basic web page to view recent replies without needing Excel.

HOW THIS FITS IN:
1. You deploy this file to a free hosting service (see DEPLOY_INSTRUCTIONS.md)
2. You get a public URL like: https://flash-webhook.onrender.com
3. You give that URL to Meta (App Dashboard -> WhatsApp -> Configuration)
4. From then on, every reply and status update gets logged here automatically,
   24/7, even when your own computer is off.

FILES THIS CREATES (in the same folder as this script):
- replies.csv       -> every incoming message from contributors
- statuses.csv      -> delivery status updates (sent/delivered/read/failed)
"""

from flask import Flask, request, jsonify
import csv
import os
from datetime import datetime

app = Flask(__name__)

# =========================================================
# FILL THIS IN — choose any secret word/phrase, then use the
# EXACT SAME value when configuring the webhook in Meta App Dashboard
# =========================================================
VERIFY_TOKEN = "flash2026verify"

REPLIES_CSV = "replies.csv"
STATUSES_CSV = "statuses.csv"


def append_csv(filepath, fieldnames, row):
    file_exists = os.path.exists(filepath)
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Meta calls this once, when you first configure the webhook URL,
    to confirm you actually control this server."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    """Meta calls this every time there's a new message or status update."""
    data = request.get_json()

    try:
        entry = data.get("entry", [])
        for e in entry:
            changes = e.get("changes", [])
            for change in changes:
                value = change.get("value", {})

                # Incoming messages (replies from contributors)
                messages = value.get("messages", [])
                contacts = value.get("contacts", [])
                contact_name = contacts[0]["profile"]["name"] if contacts else ""

                for msg in messages:
                    from_number = msg.get("from", "")
                    timestamp = msg.get("timestamp", "")
                    msg_type = msg.get("type", "")

                    text_body = ""
                    if msg_type == "text":
                        text_body = msg.get("text", {}).get("body", "")
                    elif msg_type == "button":
                        text_body = msg.get("button", {}).get("text", "")
                    elif msg_type == "interactive":
                        interactive = msg.get("interactive", {})
                        if "button_reply" in interactive:
                            text_body = interactive["button_reply"].get("title", "")
                        elif "list_reply" in interactive:
                            text_body = interactive["list_reply"].get("title", "")

                    readable_time = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""

                    append_csv(
                        REPLIES_CSV,
                        ["received_at", "from_number", "contact_name", "message_type", "message_text"],
                        {
                            "received_at": readable_time,
                            "from_number": from_number,
                            "contact_name": contact_name,
                            "message_type": msg_type,
                            "message_text": text_body,
                        }
                    )
                    print(f"NEW REPLY from {contact_name} ({from_number}): {text_body}")

                # Status updates (sent/delivered/read/failed)
                statuses = value.get("statuses", [])
                for status in statuses:
                    recipient = status.get("recipient_id", "")
                    status_type = status.get("status", "")
                    timestamp = status.get("timestamp", "")
                    readable_time = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""

                    error_msg = ""
                    if status_type == "failed":
                        errors = status.get("errors", [])
                        if errors:
                            error_msg = errors[0].get("title", "")

                    append_csv(
                        STATUSES_CSV,
                        ["updated_at", "recipient_number", "status", "error"],
                        {
                            "updated_at": readable_time,
                            "recipient_number": recipient,
                            "status": status_type,
                            "error": error_msg,
                        }
                    )
    except Exception as ex:
        print(f"Error processing webhook: {ex}")

    return jsonify({"status": "received"}), 200


@app.route("/", methods=["GET"])
def home():
    """Simple status page + recent replies viewer."""
    replies_html = "<p>No replies yet.</p>"
    if os.path.exists(REPLIES_CSV):
        with open(REPLIES_CSV, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            reader.reverse()  # newest first
            rows_html = "".join(
                f"<tr><td>{r['received_at']}</td><td>{r['contact_name']}</td>"
                f"<td>{r['from_number']}</td><td>{r['message_text']}</td></tr>"
                for r in reader[:100]
            )
            replies_html = f"""
            <table border="1" cellpadding="8" style="border-collapse: collapse;">
                <tr><th>Time</th><th>Name</th><th>Number</th><th>Message</th></tr>
                {rows_html}
            </table>
            """

    return f"""
    <html>
    <head><title>FLASH Charities - WhatsApp Replies</title></head>
    <body style="font-family: sans-serif; padding: 20px;">
        <h1>FLASH Charities WhatsApp Webhook</h1>
        <p>Status: Running</p>
        <h2>Recent Replies (newest first)</h2>
        {replies_html}
    </body>
    </html>
    """


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
