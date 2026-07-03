"""
FLASH Charities - WhatsApp Inbox + Bulk Sender
------------------------------------------------------------------------------
Everything in one web app:

INBOX TAB:
- Contact list + WhatsApp-style chat threads
- Free-text replies (within 24h window)
- Attach & send images/videos in chat (within 24h window)
- "+ New Chat" — start a conversation via your approved Telugu template

BULK SEND TAB:
- Paste CSV rows (name,phone,amount,date)
- Sends your Telugu template to everyone, in the background, with live progress
- Optional image/video header support (needs a media-header template approved
  in WhatsApp Manager first)
- Skips numbers already sent (sent log), respects a per-run cap you set

SETUP: see INBOX_UPGRADE_INSTRUCTIONS.md
Environment variables needed on Render:
  WHATSAPP_TOKEN  = permanent System User access token
  INBOX_PASSWORD  = password for this page
"""

from flask import Flask, request, jsonify, Response
import csv
import io
import os
import time
import threading
import requests as http_requests
from datetime import datetime
from functools import wraps

app = Flask(__name__)

# =========================================================
# CONFIGURATION
# =========================================================
VERIFY_TOKEN = "flash2026verify"

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
INBOX_PASSWORD = os.environ.get("INBOX_PASSWORD", "flash123")

PHONE_NUMBER_ID = "1211131482079268"
WABA_ID = "1730916004764751"  # WhatsApp Business Account ID (for listing templates)

# Default (text-header) template — your approved Telugu one
TEXT_TEMPLATE_NAME = "donor_thank_you_meeting_update_te"
TEMPLATE_LANGUAGE = "te"

MESSAGES_CSV = "messages.csv"
STATUSES_CSV = "statuses.csv"
SENT_LOG_CSV = "bulk_sent_log.csv"

MSG_FIELDS = ["timestamp", "direction", "contact_number", "contact_name", "message_type", "message_text"]

GRAPH = "https://graph.facebook.com/v20.0"


# =========================================================
# AUTH
# =========================================================
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != INBOX_PASSWORD:
            return Response("Login required", 401,
                            {"WWW-Authenticate": 'Basic realm="FLASH Inbox"'})
        return f(*args, **kwargs)
    return decorated


# =========================================================
# STORAGE HELPERS
# =========================================================
def append_csv(filepath, fieldnames, row):
    file_exists = os.path.exists(filepath)
    with open(filepath, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def read_all_messages():
    if not os.path.exists(MESSAGES_CSV):
        return []
    with open(MESSAGES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_sent_numbers():
    if not os.path.exists(SENT_LOG_CSV):
        return set()
    with open(SENT_LOG_CSV, newline="", encoding="utf-8") as f:
        return {row["phone"] for row in csv.DictReader(f)}


def clean_phone(phone):
    phone = str(phone).strip().replace(" ", "").replace("-", "").replace("+", "")
    if not phone.startswith("91") and len(phone) == 10:
        phone = "91" + phone
    return phone


def log_outgoing(number, name, mtype, text):
    append_csv(MESSAGES_CSV, MSG_FIELDS, {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "direction": "out",
        "contact_number": number,
        "contact_name": name,
        "message_type": mtype,
        "message_text": text,
    })


# =========================================================
# META API HELPERS
# =========================================================
def meta_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}


def send_template(to, name, amount, date, template_name=None, media_type=None, media_url=None, language=None):
    """Send a template. If media_type+media_url given, the template must have
    an image/video header. Otherwise uses a text header with the name."""
    tname = template_name or TEXT_TEMPLATE_NAME
    lang = language or TEMPLATE_LANGUAGE
    components = []

    if media_type and media_url:
        components.append({
            "type": "header",
            "parameters": [{"type": media_type, media_type: {"link": media_url}}]
        })
    else:
        components.append({
            "type": "header",
            "parameters": [{"type": "text", "text": name}]
        })

    components.append({
        "type": "body",
        "parameters": [
            {"type": "text", "text": name},
            {"type": "text", "text": amount},
            {"type": "text", "text": date}
        ]
    })

    return http_requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
        headers={**meta_headers(), "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "type": "template",
              "template": {"name": tname, "language": {"code": lang},
                           "components": components}},
        timeout=30
    )


# =========================================================
# WEBHOOK ENDPOINTS
# =========================================================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    if (request.args.get("hub.mode") == "subscribe"
            and request.args.get("hub.verify_token") == VERIFY_TOKEN):
        return request.args.get("hub.challenge"), 200
    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json()
    try:
        for e in data.get("entry", []):
            for change in e.get("changes", []):
                value = change.get("value", {})
                contacts = value.get("contacts", [])
                contact_name = contacts[0]["profile"]["name"] if contacts else ""

                for msg in value.get("messages", []):
                    from_number = msg.get("from", "")
                    timestamp = msg.get("timestamp", "")
                    msg_type = msg.get("type", "")
                    text_body = ""
                    if msg_type == "text":
                        text_body = msg.get("text", {}).get("body", "")
                    elif msg_type == "button":
                        text_body = msg.get("button", {}).get("text", "")
                    elif msg_type == "interactive":
                        i = msg.get("interactive", {})
                        if "button_reply" in i:
                            text_body = i["button_reply"].get("title", "")
                        elif "list_reply" in i:
                            text_body = i["list_reply"].get("title", "")
                    else:
                        text_body = f"[{msg_type} message received]"

                    readable = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""
                    append_csv(MESSAGES_CSV, MSG_FIELDS, {
                        "timestamp": readable, "direction": "in",
                        "contact_number": from_number, "contact_name": contact_name,
                        "message_type": msg_type, "message_text": text_body,
                    })

                for status in value.get("statuses", []):
                    ts = status.get("timestamp", "")
                    readable = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
                    error_msg = ""
                    if status.get("status") == "failed":
                        errs = status.get("errors", [])
                        if errs:
                            error_msg = errs[0].get("title", "")
                    append_csv(STATUSES_CSV,
                               ["updated_at", "recipient_number", "status", "error"],
                               {"updated_at": readable,
                                "recipient_number": status.get("recipient_id", ""),
                                "status": status.get("status", ""), "error": error_msg})
    except Exception as ex:
        print(f"Webhook error: {ex}")
    return jsonify({"status": "received"}), 200


# =========================================================
# INBOX APIs
# =========================================================
@app.route("/api/conversations")
@requires_auth
def api_conversations():
    msgs = read_all_messages()
    convos = {}
    for m in msgs:
        num = m["contact_number"]
        if num not in convos:
            convos[num] = {"number": num, "name": m["contact_name"] or num,
                           "last_message": "", "last_time": ""}
        if m["contact_name"]:
            convos[num]["name"] = m["contact_name"]
        convos[num]["last_message"] = m["message_text"]
        convos[num]["last_time"] = m["timestamp"]
    return jsonify(sorted(convos.values(), key=lambda c: c["last_time"], reverse=True))


@app.route("/api/messages/<number>")
@requires_auth
def api_messages(number):
    return jsonify([m for m in read_all_messages() if m["contact_number"] == number])


@app.route("/api/send", methods=["POST"])
@requires_auth
def api_send():
    if not WHATSAPP_TOKEN:
        return jsonify({"error": "WHATSAPP_TOKEN not set on server"}), 500
    body = request.get_json()
    to, text = body.get("to", "").strip(), body.get("text", "").strip()
    if not to or not text:
        return jsonify({"error": "Missing to/text"}), 400
    resp = http_requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
        headers={**meta_headers(), "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "type": "text",
              "text": {"body": text}},
        timeout=30)
    if resp.status_code == 200:
        log_outgoing(to, "", "text", text)
        return jsonify({"status": "sent"})
    return jsonify({"error": f"Meta API {resp.status_code}", "detail": resp.text[:400]}), 502


@app.route("/api/send_media", methods=["POST"])
@requires_auth
def api_send_media():
    """Attach image/video in chat: upload file to Meta, then send it."""
    if not WHATSAPP_TOKEN:
        return jsonify({"error": "WHATSAPP_TOKEN not set on server"}), 500
    to = request.form.get("to", "").strip()
    f = request.files.get("file")
    if not to or not f:
        return jsonify({"error": "Missing to/file"}), 400

    mime = f.mimetype or ""
    if mime.startswith("image/"):
        mtype = "image"
    elif mime.startswith("video/"):
        mtype = "video"
    else:
        return jsonify({"error": f"Unsupported file type: {mime}. Use JPG/PNG image or MP4 video."}), 400

    # 1) upload media to Meta
    up = http_requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/media",
        headers=meta_headers(),
        data={"messaging_product": "whatsapp", "type": mime},
        files={"file": (f.filename, f.stream, mime)},
        timeout=120)
    if up.status_code != 200:
        return jsonify({"error": f"Media upload failed {up.status_code}", "detail": up.text[:400]}), 502
    media_id = up.json().get("id")

    # 2) send it
    resp = http_requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
        headers={**meta_headers(), "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "type": mtype,
              mtype: {"id": media_id}},
        timeout=30)
    if resp.status_code == 200:
        log_outgoing(to, "", mtype, f"[{mtype} sent: {f.filename}]")
        return jsonify({"status": "sent"})
    return jsonify({"error": f"Meta API {resp.status_code}", "detail": resp.text[:400]}), 502


@app.route("/api/send_template", methods=["POST"])
@requires_auth
def api_send_template():
    """New Chat: sends the Telugu template to one number."""
    if not WHATSAPP_TOKEN:
        return jsonify({"error": "WHATSAPP_TOKEN not set on server"}), 500
    b = request.get_json()
    to = clean_phone(b.get("to", ""))
    name, amount, date = b.get("name", "").strip(), b.get("amount", "").strip(), b.get("date", "").strip()
    if not all([to, name, amount, date]):
        return jsonify({"error": "Missing to/name/amount/date"}), 400
    resp = send_template(to, name, amount, date)
    if resp.status_code == 200:
        log_outgoing(to, name, "template", f"[Template] ధన్యవాదాలు {name} గారు — ₹{amount} — {date}")
        return jsonify({"status": "sent", "to": to})
    return jsonify({"error": f"Meta API {resp.status_code}", "detail": resp.text[:400]}), 502


# =========================================================
# BULK SEND (background thread + progress)
# =========================================================
bulk_state = {"running": False, "total": 0, "done": 0, "success": 0,
              "failed": 0, "skipped": 0, "log": [], "finished_at": ""}


def bulk_worker(rows, cap, delay, template_name, media_type, media_url, template_lang):
    global bulk_state
    already = load_sent_numbers()
    pending = []
    for r in rows:
        p = clean_phone(r.get("phone", ""))
        if p in already:
            bulk_state["skipped"] += 1
            continue
        r["phone"] = p
        pending.append(r)

    batch = pending[:cap]
    bulk_state["total"] = len(batch)

    for r in batch:
        name, phone = r.get("name", "").strip(), r["phone"]
        amount, date = r.get("amount", "").strip(), r.get("date", "").strip()
        if not all([name, phone, amount, date]):
            bulk_state["failed"] += 1
            bulk_state["log"].append(f"SKIP (missing data): {r}")
        else:
            try:
                resp = send_template(phone, name, amount, date,
                                     template_name=template_name or None,
                                     media_type=media_type or None,
                                     media_url=media_url or None,
                                     language=template_lang or None)
                if resp.status_code == 200:
                    bulk_state["success"] += 1
                    bulk_state["log"].append(f"OK: {name} ({phone})")
                    append_csv(SENT_LOG_CSV, ["name", "phone", "amount", "date", "sent_at"],
                               {"name": name, "phone": phone, "amount": amount,
                                "date": date, "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                    log_outgoing(phone, name, "template", f"[Bulk template] {name} — ₹{amount} — {date}")
                else:
                    bulk_state["failed"] += 1
                    bulk_state["log"].append(f"FAIL {resp.status_code}: {name} ({phone}) {resp.text[:150]}")
            except Exception as ex:
                bulk_state["failed"] += 1
                bulk_state["log"].append(f"ERROR: {name} ({phone}) {ex}")
        bulk_state["done"] += 1
        time.sleep(delay)

    bulk_state["running"] = False
    bulk_state["finished_at"] = datetime.now().strftime("%H:%M:%S")


@app.route("/api/templates")
@requires_auth
def api_templates():
    """Fetch approved templates from Meta so the UI can show a dropdown.
    Auto-detects each template's header type (TEXT / IMAGE / VIDEO)."""
    if not WHATSAPP_TOKEN:
        return jsonify({"error": "WHATSAPP_TOKEN not set on server"}), 500
    resp = http_requests.get(
        f"{GRAPH}/{WABA_ID}/message_templates",
        headers=meta_headers(),
        params={"fields": "name,status,language,components", "limit": 100},
        timeout=30)
    if resp.status_code != 200:
        return jsonify({"error": f"Meta API {resp.status_code}", "detail": resp.text[:300]}), 502

    out = []
    for t in resp.json().get("data", []):
        if t.get("status") != "APPROVED":
            continue
        header_type = "none"
        body_vars = 0
        for c in t.get("components", []):
            if c.get("type") == "HEADER":
                header_type = c.get("format", "TEXT").lower()  # text / image / video / document
            if c.get("type") == "BODY":
                import re as _re
                body_vars = len(set(_re.findall(r"\{\{(\d+)\}\}", c.get("text", ""))))
        out.append({"name": t["name"], "language": t["language"],
                    "header_type": header_type, "body_vars": body_vars})
    return jsonify(out)


@app.route("/api/bulk_start", methods=["POST"])
@requires_auth
def api_bulk_start():
    global bulk_state
    if not WHATSAPP_TOKEN:
        return jsonify({"error": "WHATSAPP_TOKEN not set on server"}), 500
    if bulk_state["running"]:
        return jsonify({"error": "A bulk send is already running"}), 409

    b = request.get_json()
    csv_text = b.get("csv", "").strip()
    cap = int(b.get("cap", 250))
    template_name = b.get("template_name", "").strip()
    template_lang = b.get("template_lang", "").strip()
    media_type = b.get("media_type", "").strip()   # "", "image", or "video"
    media_url = b.get("media_url", "").strip()

    if media_type and not media_url:
        return jsonify({"error": "This template has a media header - a media URL is required"}), 400

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        rows = list(reader)
        required = {"name", "phone", "amount", "date"}
        if not rows or not required.issubset(set(reader.fieldnames or [])):
            return jsonify({"error": "CSV must have header: name,phone,amount,date"}), 400
    except Exception as ex:
        return jsonify({"error": f"CSV parse error: {ex}"}), 400

    bulk_state = {"running": True, "total": 0, "done": 0, "success": 0,
                  "failed": 0, "skipped": 0, "log": [], "finished_at": ""}
    threading.Thread(target=bulk_worker,
                     args=(rows, cap, 1.5, template_name, media_type, media_url, template_lang),
                     daemon=True).start()
    return jsonify({"status": "started", "rows": len(rows)})


@app.route("/api/bulk_status")
@requires_auth
def api_bulk_status():
    s = dict(bulk_state)
    s["log"] = s["log"][-30:]
    return jsonify(s)


# =========================================================
# UI
# =========================================================
PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FLASH Inbox</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system,'Segoe UI',Roboto,sans-serif; height: 100vh; display: flex; flex-direction: column; }
  header { background:#075E54; color:white; padding:10px 20px; display:flex; align-items:center; gap:20px; }
  header h1 { font-size:17px; }
  .tabs { display:flex; gap:4px; }
  .tab { padding:8px 18px; background:rgba(255,255,255,.15); color:white; border:none; border-radius:6px 6px 0 0; cursor:pointer; font-size:14px; }
  .tab.active { background:white; color:#075E54; font-weight:600; }
  .page { flex:1; display:none; overflow:hidden; }
  .page.active { display:flex; }

  /* Inbox */
  .sidebar-wrap { display:flex; flex-direction:column; width:320px; border-right:1px solid #ddd; background:#fff; }
  .newchat-btn { margin:12px; padding:10px; background:#075E54; color:white; border:none; border-radius:8px; font-size:14px; cursor:pointer; }
  .sidebar { flex:1; overflow-y:auto; }
  .convo { padding:13px 16px; border-bottom:1px solid #f0f0f0; cursor:pointer; }
  .convo:hover { background:#f5f5f5; } .convo.active { background:#e8f5e9; }
  .convo .name { font-weight:600; margin-bottom:3px; font-size:14px; }
  .convo .preview { font-size:13px; color:#666; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .convo .time { font-size:11px; color:#999; float:right; }
  .chat { flex:1; display:flex; flex-direction:column; background:#ECE5DD; }
  .chat-header { background:#f7f7f7; padding:12px 20px; border-bottom:1px solid #ddd; font-weight:600; }
  .messages { flex:1; overflow-y:auto; padding:20px; }
  .msg { max-width:65%; padding:8px 12px; border-radius:8px; margin-bottom:8px; font-size:14px; line-height:1.4; clear:both; word-wrap:break-word; }
  .msg.in { background:white; float:left; } .msg.out { background:#DCF8C6; float:right; }
  .msg .t { font-size:10px; color:#999; margin-top:4px; text-align:right; }
  .composer { display:flex; padding:12px; background:#f7f7f7; gap:8px; align-items:center; }
  .composer input[type=text] { flex:1; padding:12px 16px; border:1px solid #ddd; border-radius:24px; font-size:14px; outline:none; }
  .composer button { background:#075E54; color:white; border:none; border-radius:24px; padding:12px 20px; font-size:14px; cursor:pointer; }
  .composer button:disabled { background:#aaa; }
  .attach { background:#eee !important; color:#333 !important; padding:12px 14px !important; }
  .empty { flex:1; display:flex; align-items:center; justify-content:center; color:#999; }
  .note { font-size:11px; color:#888; padding:4px 16px 10px; background:#f7f7f7; }

  /* Bulk */
  .bulk { flex:1; overflow-y:auto; padding:30px; max-width:820px; margin:0 auto; width:100%; }
  .bulk h2 { margin-bottom:6px; }
  .bulk p.sub { color:#666; font-size:13px; margin-bottom:20px; }
  .bulk label { font-size:13px; font-weight:600; display:block; margin:16px 0 5px; }
  .bulk textarea { width:100%; height:180px; font-family:monospace; font-size:13px; padding:12px; border:1px solid #ccc; border-radius:8px; }
  .bulk input, .bulk select { padding:9px 12px; border:1px solid #ccc; border-radius:6px; font-size:14px; width:100%; }
  .row2 { display:flex; gap:14px; } .row2 > div { flex:1; }
  .startbtn { margin-top:20px; background:#075E54; color:white; border:none; border-radius:8px; padding:13px 30px; font-size:15px; cursor:pointer; }
  .startbtn:disabled { background:#aaa; }
  .progress { margin-top:24px; display:none; }
  .bar { height:14px; background:#eee; border-radius:7px; overflow:hidden; }
  .bar > div { height:100%; background:#25D366; width:0%; transition:width .5s; }
  .stats { font-size:13px; color:#444; margin-top:8px; }
  .loglines { margin-top:12px; background:#111; color:#9f9; font-family:monospace; font-size:12px; padding:12px; border-radius:8px; height:200px; overflow-y:auto; white-space:pre-wrap; }
  .mediahint { font-size:12px; color:#888; margin-top:4px; line-height:1.5; }

  /* Modal */
  .modal-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); align-items:center; justify-content:center; z-index:10; }
  .modal { background:white; border-radius:12px; padding:24px; width:340px; }
  .modal h3 { margin-bottom:14px; }
  .modal label { font-size:12px; color:#555; display:block; margin:10px 0 3px; }
  .modal input { width:100%; padding:9px 12px; border:1px solid #ccc; border-radius:6px; font-size:14px; }
  .modal .actions { display:flex; gap:8px; margin-top:18px; }
  .modal .actions button { flex:1; padding:10px; border:none; border-radius:6px; font-size:14px; cursor:pointer; }
  .modal .send { background:#075E54; color:white; } .modal .cancel { background:#eee; }
  .modal .hint { font-size:11px; color:#888; margin-top:10px; line-height:1.4; }
</style>
</head>
<body>
<header>
  <h1>FLASH Charities</h1>
  <div class="tabs">
    <button class="tab active" id="tabInbox" onclick="showTab('inbox')">Inbox</button>
    <button class="tab" id="tabBulk" onclick="showTab('bulk')">Bulk Send</button>
  </div>
</header>

<!-- ============ INBOX PAGE ============ -->
<div class="page active" id="pageInbox">
  <div class="sidebar-wrap">
    <button class="newchat-btn" onclick="openNewChat()">+ New Chat</button>
    <div class="sidebar" id="sidebar"><div class="empty">Loading…</div></div>
  </div>
  <div class="chat">
    <div class="chat-header" id="chatHeader">Select a conversation</div>
    <div class="messages" id="messages"><div class="empty">No conversation selected</div></div>
    <div class="composer">
      <button class="attach" onclick="document.getElementById('fileInput').click()" id="attachBtn" disabled>📎</button>
      <input type="file" id="fileInput" accept="image/*,video/mp4" style="display:none" onchange="sendFile()">
      <input type="text" id="msgInput" placeholder="Type a reply…" disabled onkeydown="if(event.key==='Enter')sendMsg()">
      <button id="sendBtn" onclick="sendMsg()" disabled>Send</button>
    </div>
    <div class="note">Free text & attachments work within 24h of the contact's last message (WhatsApp rule). To start NEW conversations use + New Chat or the Bulk Send tab.</div>
  </div>
</div>

<!-- ============ BULK PAGE ============ -->
<div class="page" id="pageBulk">
  <div class="bulk">
    <h2>Bulk Send</h2>
    <p class="sub">Sends your approved template to every row. Numbers already in the sent log are skipped automatically.</p>

    <label>Contributor rows (CSV — first line must be: name,phone,amount,date)</label>
    <textarea id="bulkCsv" placeholder='name,phone,amount,date
సిరిగినీడి నాగేశ్వరరావు,919848119567,"50,000",12 ఏప్రిల్ 2026
రాజేష్ కుమార్,9123456789,"1,116",29 జూన్ 2026'></textarea>

    <div class="row2">
      <div>
        <label>Max messages this run (your daily limit)</label>
        <input id="bulkCap" type="number" value="250">
      </div>
      <div>
        <label>Template <span style="font-weight:normal;color:#888">(loaded from your WhatsApp Manager)</span></label>
        <select id="bulkTemplate" onchange="templateChanged()">
          <option value="">Loading templates…</option>
        </select>
      </div>
    </div>

    <div id="mediaFields" style="display:none">
      <label>Public media URL (same image/video for everyone)</label>
      <input id="bulkMediaUrl" placeholder="https://.../poster.jpg or video.mp4">
      <div class="mediahint">
        This template has an image/video header, so a publicly downloadable URL is
        required. Video: MP4, under 16MB.
      </div>
    </div>

    <button class="startbtn" id="bulkStart" onclick="startBulk()">Start Bulk Send</button>

    <div class="progress" id="bulkProgress">
      <div class="bar"><div id="bulkBar"></div></div>
      <div class="stats" id="bulkStats"></div>
      <div class="loglines" id="bulkLog"></div>
    </div>
  </div>
</div>

<!-- ============ NEW CHAT MODAL ============ -->
<div class="modal-bg" id="newChatModal">
  <div class="modal">
    <h3>New Chat (sends Telugu template)</h3>
    <label>Phone number (10 digits or with 91)</label>
    <input id="nc_phone" placeholder="9848119567">
    <label>Contributor name</label>
    <input id="nc_name" placeholder="సిరిగినీడి నాగేశ్వరరావు">
    <label>Contribution amount (without ₹)</label>
    <input id="nc_amount" placeholder="1,116">
    <label>Contribution date</label>
    <input id="nc_date" placeholder="03 జూలై 2026">
    <div class="actions">
      <button class="cancel" onclick="closeNewChat()">Cancel</button>
      <button class="send" id="nc_sendBtn" onclick="sendNewChat()">Send Template</button>
    </div>
    <div class="hint">Once they reply, free chat opens for 24 hours.</div>
  </div>
</div>

<script>
let currentNumber = null;

/* ---------- tabs ---------- */
function showTab(which) {
  document.getElementById('pageInbox').classList.toggle('active', which==='inbox');
  document.getElementById('pageBulk').classList.toggle('active', which==='bulk');
  document.getElementById('tabInbox').classList.toggle('active', which==='inbox');
  document.getElementById('tabBulk').classList.toggle('active', which==='bulk');
}

/* ---------- inbox ---------- */
async function loadConversations() {
  const res = await fetch('/api/conversations');
  const convos = await res.json();
  const sb = document.getElementById('sidebar');
  if (!convos.length) { sb.innerHTML = '<div class="empty">No messages yet</div>'; return; }
  sb.innerHTML = convos.map(c => `
    <div class="convo ${c.number===currentNumber?'active':''}" onclick="openChat('${c.number}', '${(c.name||'').replace(/'/g,"\\\\'")}')">
      <div class="name">${c.name} <span class="time">${(c.last_time||'').slice(5,16)}</span></div>
      <div class="preview">${c.last_message||''}</div>
    </div>`).join('');
}

async function openChat(number, name) {
  currentNumber = number;
  document.getElementById('chatHeader').textContent = name + '  (' + number + ')';
  ['msgInput','sendBtn','attachBtn'].forEach(id => document.getElementById(id).disabled = false);
  await loadMessages();
  loadConversations();
}

async function loadMessages() {
  if (!currentNumber) return;
  const res = await fetch('/api/messages/' + currentNumber);
  const msgs = await res.json();
  const el = document.getElementById('messages');
  el.innerHTML = msgs.map(m => `
    <div class="msg ${m.direction}">${m.message_text}<div class="t">${m.timestamp}</div></div>`).join('')
    + '<div style="clear:both"></div>';
  el.scrollTop = el.scrollHeight;
}

async function sendMsg() {
  const input = document.getElementById('msgInput');
  const text = input.value.trim();
  if (!text || !currentNumber) return;
  input.value = '';
  const res = await fetch('/api/send', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({to: currentNumber, text})});
  if (!res.ok) { const e = await res.json();
    alert('Send failed: ' + (e.detail || e.error) + '\\n\\n(If it mentions re-engagement, the 24h window is closed — use a template.)'); }
  await loadMessages(); loadConversations();
}

async function sendFile() {
  const fi = document.getElementById('fileInput');
  if (!fi.files.length || !currentNumber) return;
  const fd = new FormData();
  fd.append('to', currentNumber);
  fd.append('file', fi.files[0]);
  fi.value = '';
  const res = await fetch('/api/send_media', {method:'POST', body: fd});
  if (!res.ok) { const e = await res.json(); alert('Media send failed: ' + (e.detail || e.error)); }
  await loadMessages();
}

/* ---------- new chat ---------- */
function openNewChat(){ document.getElementById('newChatModal').style.display='flex'; }
function closeNewChat(){ document.getElementById('newChatModal').style.display='none'; }
async function sendNewChat() {
  const g = id => document.getElementById(id).value.trim();
  const phone=g('nc_phone'), name=g('nc_name'), amount=g('nc_amount'), date=g('nc_date');
  if (!phone||!name||!amount||!date) { alert('Fill in all four fields.'); return; }
  const btn = document.getElementById('nc_sendBtn');
  btn.disabled=true; btn.textContent='Sending…';
  const res = await fetch('/api/send_template', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({to:phone, name, amount, date})});
  btn.disabled=false; btn.textContent='Send Template';
  if (res.ok) { const d = await res.json(); closeNewChat();
    ['nc_phone','nc_name','nc_amount','nc_date'].forEach(id=>document.getElementById(id).value='');
    await loadConversations(); openChat(d.to, name);
  } else { const e = await res.json(); alert('Send failed: ' + (e.detail || e.error)); }
}

/* ---------- bulk ---------- */
let templates = [];

async function loadTemplates() {
  const sel = document.getElementById('bulkTemplate');
  try {
    const res = await fetch('/api/templates');
    if (!res.ok) { sel.innerHTML = '<option value="">Could not load templates</option>'; return; }
    templates = await res.json();
    if (!templates.length) { sel.innerHTML = '<option value="">No approved templates found</option>'; return; }
    sel.innerHTML = templates.map((t,i) =>
      `<option value="${i}">${t.name} (${t.language}${t.header_type!=='text'&&t.header_type!=='none' ? ', ' + t.header_type + ' header' : ''})</option>`
    ).join('');
    templateChanged();
  } catch(e) { sel.innerHTML = '<option value="">Error loading templates</option>'; }
}

function templateChanged() {
  const sel = document.getElementById('bulkTemplate');
  const t = templates[parseInt(sel.value)];
  const needsMedia = t && (t.header_type === 'image' || t.header_type === 'video');
  document.getElementById('mediaFields').style.display = needsMedia ? 'block' : 'none';
}

let bulkPolling = null;
async function startBulk() {
  const csv = document.getElementById('bulkCsv').value.trim();
  const cap = parseInt(document.getElementById('bulkCap').value) || 250;
  const sel = document.getElementById('bulkTemplate');
  const t = templates[parseInt(sel.value)];
  if (!t) { alert('Select a template first.'); return; }
  const needsMedia = (t.header_type === 'image' || t.header_type === 'video');
  const mediaUrl = needsMedia ? document.getElementById('bulkMediaUrl').value.trim() : '';
  if (!csv) { alert('Paste your CSV rows first.'); return; }
  if (needsMedia && !mediaUrl) { alert('This template needs a media URL.'); return; }
  if (!confirm('Send "' + t.name + '" to up to ' + cap + ' contributors?')) return;

  const res = await fetch('/api/bulk_start', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({csv, cap, template_name: t.name, template_lang: t.language,
                          media_type: needsMedia ? t.header_type : '', media_url: mediaUrl})});
  if (!res.ok) { const e = await res.json(); alert('Could not start: ' + e.error); return; }

  document.getElementById('bulkStart').disabled = true;
  document.getElementById('bulkProgress').style.display = 'block';
  bulkPolling = setInterval(pollBulk, 2000);
}

async function pollBulk() {
  const res = await fetch('/api/bulk_status');
  const s = await res.json();
  const pct = s.total ? Math.round(100*s.done/s.total) : 0;
  document.getElementById('bulkBar').style.width = pct + '%';
  document.getElementById('bulkStats').textContent =
    `${s.done}/${s.total} processed — ${s.success} sent, ${s.failed} failed, ${s.skipped} skipped (already sent earlier)` +
    (s.finished_at ? ` — finished at ${s.finished_at}` : '');
  document.getElementById('bulkLog').textContent = (s.log||[]).join('\\n');
  const lg = document.getElementById('bulkLog'); lg.scrollTop = lg.scrollHeight;
  if (!s.running && s.done > 0) {
    clearInterval(bulkPolling);
    document.getElementById('bulkStart').disabled = false;
  }
}

/* ---------- refresh loop ---------- */
loadConversations();
loadTemplates();
setInterval(() => { loadConversations(); loadMessages(); }, 5000);
</script>
</body>
</html>
"""

@app.route("/")
@requires_auth
def inbox():
    return PAGE_HTML


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
