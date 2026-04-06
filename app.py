from flask import Flask, render_template, request, redirect, session, jsonify, send_from_directory
import threading, json, os, time, hashlib, base64, secrets, subprocess, glob, gzip
from urllib.parse import urlencode
import urllib.request, ssl
from datetime import datetime

app = Flask(__name__)
app.secret_key = "clipbot_secret_2024_xK9mP"

KICK_CLIENT_ID     = "01KNFT27H9FKB3KYPN7AWBYKK4"
KICK_CLIENT_SECRET = "4b83a5d95ca99bbc6fa1f8d9630dce2c8b5caf3682c1fc1395a0ae0fd721c0f9"
REDIRECT_URI       = "https://kbot-u8we.onrender.com/callback"
SCOPES             = "user:read channel:read events:subscribe chat:write"
WEBHOOK_URL        = "https://kbot-u8we.onrender.com/webhook/kick"

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR = os.path.join(BASE_DIR, "static", "clips")
DATA_FILE = os.path.join(BASE_DIR, "users.json")
os.makedirs(CLIPS_DIR, exist_ok=True)

def load_data():
    try:
        with open(DATA_FILE) as f: return json.load(f)
    except: return {}

def save_data(d):
    with open(DATA_FILE, "w") as f: json.dump(d, f, indent=2)

def make_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def fetch(url, token=None, method="GET", body=None, content_type=None):
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    if token: hdrs["Authorization"] = f"Bearer {token}"
    if content_type: hdrs["Content-Type"] = content_type
    req = urllib.request.Request(url, headers=hdrs, data=body, method=method)
    with urllib.request.urlopen(req, timeout=15, context=make_ctx()) as r:
        raw = r.read()
    if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="ignore")

# ── OAuth ─────────────────────────────────────────────────
def make_auth_url():
    verifier  = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_hex(16)
    params = urlencode({
        "response_type": "code", "client_id": KICK_CLIENT_ID,
        "redirect_uri": REDIRECT_URI, "scope": SCOPES,
        "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"https://id.kick.com/oauth/authorize?{params}", verifier, state

def exchange_code(code, verifier):
    body = urlencode({
        "grant_type": "authorization_code", "client_id": KICK_CLIENT_ID,
        "client_secret": KICK_CLIENT_SECRET, "code": code,
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier,
    }).encode()
    resp = fetch("https://id.kick.com/oauth/token", method="POST",
                 body=body, content_type="application/x-www-form-urlencoded")
    return json.loads(resp)

def get_user_info(token):
    resp = fetch("https://api.kick.com/public/v1/users", token=token)
    d = json.loads(resp)
    items = d.get("data") or []
    return items[0] if items else {}

def get_channel_info(slug, token):
    try:
        resp = fetch(f"https://api.kick.com/public/v1/channels?slug={slug}", token=token)
        d = json.loads(resp)
        items = d.get("data") or []
        return items[0] if items else {}
    except:
        return {}

# ── Bot ───────────────────────────────────────────────────
active_bots = {}

def bot_log(uid, msg):
    if uid in active_bots:
        ts = datetime.now().strftime("%H:%M:%S")
        active_bots[uid]["log"].append(f"[{ts}] {msg}")
        active_bots[uid]["log"] = active_bots[uid]["log"][-100:]

def subscribe_events(uid, token, broadcaster_id):
    try:
        payload = json.dumps({
            "events": [{"name": "chat.message.sent", "version": 1,
                        "broadcaster_user_id": int(broadcaster_id)}],
            "method": "webhook",
            "webhook_url": WEBHOOK_URL
        }).encode()
        resp = fetch(
            "https://api.kick.com/public/v1/events/subscriptions",
            token=token, method="POST", body=payload,
            content_type="application/json"
        )
        result = json.loads(resp)
        bot_log(uid, f"✅ Webhook aktif!")
        return True
    except Exception as e:
        bot_log(uid, f"Webhook hatasi: {e}")
        return False

def record_clip(uid, channel, token, manual_url, duration):
    hls = manual_url or ""
    if not hls:
        bot_log(uid, "Stream URL yok — Manuel URL gir!")
        return None
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"clip_{ts}.mp4"
    fpath = os.path.join(CLIPS_DIR, fname)
    try:
        bot_log(uid, f"Kaydediliyor ({duration}s)...")
        subprocess.run([
            "ffmpeg", "-y", "-i", hls, "-t", str(duration),
            "-c", "copy", "-movflags", "+faststart", fpath
        ], timeout=duration+60, capture_output=True)
        if os.path.exists(fpath) and os.path.getsize(fpath) > 10000:
            bot_log(uid, f"✅ Klip hazir: {fname}")
            return fname
        bot_log(uid, "Klip kaydedilemedi (dosya bos)")
    except Exception as e:
        bot_log(uid, f"FFmpeg hatasi: {e}")
    return None

def run_bot(uid, settings):
    token          = settings.get("access_token", "")
    broadcaster_id = settings.get("broadcaster_id", "")
    channel        = settings.get("channel", "")
    manual_url     = settings.get("manual_hls_url", "")
    duration       = int(settings.get("clip_duration", 180))

    bot_log(uid, f"Bot basladi → kick.com/{channel}")

    if broadcaster_id:
        subscribe_events(uid, token, broadcaster_id)
        # Webhook gelene kadar bekle
        while active_bots.get(uid, {}).get("running"):
            time.sleep(5)
    else:
        bot_log(uid, "❌ Broadcaster ID yok! Token Yenile butonuna bas.")

    bot_log(uid, "Bot durduruldu.")

# ── Webhook ───────────────────────────────────────────────
@app.route("/webhook/kick", methods=["GET", "POST"])
def webhook_kick():
    if request.method == "GET":
        return request.args.get("challenge", "ok"), 200

    data = request.get_json(force=True) or {}

    # Kick format: {broadcaster:{user_id, username}, sender:{user_id, username}, content:...}
    broadcaster   = data.get("broadcaster") or {}
    sender_info   = data.get("sender") or data.get("chatter") or {}
    content_text  = (data.get("content") or data.get("message") or "").strip()
    broadcaster_id = str(broadcaster.get("user_id") or "")
    msg_user      = sender_info.get("username") or sender_info.get("slug") or "?"

    # Hangi kullanicinin botu bu broadcaster'a ait?
    all_data = load_data()
    target_uid = None
    for uid2, udata in all_data.items():
        if str(udata.get("broadcaster_id", "")) == broadcaster_id:
            target_uid = uid2
            break

    if not target_uid:
        for uid2 in active_bots:
            if active_bots[uid2].get("running"):
                target_uid = uid2
                break

    if target_uid:
        bot_log(target_uid, f"Chat: {msg_user}: {content_text[:40]}")

    if content_text.strip().lower() == "!clip" and target_uid and active_bots.get(target_uid, {}).get("running"):
        settings = all_data.get(target_uid, {})
        bot_log(target_uid, f"🎮 !clip → {msg_user}")

        def do_clip(cu=msg_user, tuid=target_uid):
            fname = record_clip(tuid, settings.get("channel",""),
                                settings.get("access_token",""),
                                settings.get("manual_hls_url",""),
                                int(settings.get("clip_duration", 180)))
            if fname:
                active_bots[tuid]["clips"].insert(0, {
                    "file": fname, "user": cu,
                    "ts": datetime.now().strftime("%d.%m %H:%M"),
                    "url": f"/clips/{fname}"
                })
                active_bots[tuid]["clips"] = active_bots[tuid]["clips"][:30]
                d2 = load_data()
                if tuid in d2:
                    d2[tuid]["clips"] = active_bots[tuid]["clips"]
                    save_data(d2)

        threading.Thread(target=do_clip, daemon=True).start()

    return {"status": "ok"}, 200

# ── Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    uid  = session.get("uid")
    data = load_data()
    user = data.get(uid) if uid else None
    bot  = active_bots.get(uid, {})
    return render_template("index.html",
        user=user,
        running=bot.get("running", False),
        clips=bot.get("clips", (user or {}).get("clips", [])),
        log=bot.get("log", [])[-20:],
    )

@app.route("/login")
def login():
    auth_url, verifier, state = make_auth_url()
    session["pkce_verifier"] = verifier
    session["oauth_state"]   = state
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    if state != session.get("oauth_state"):
        return "State mismatch!", 400
    try:
        tokens  = exchange_code(code, session.pop("pkce_verifier", ""))
        token   = tokens.get("access_token")
        refresh = tokens.get("refresh_token", "")
        info    = get_user_info(token)
        uid     = str(info.get("user_id") or info.get("id") or secrets.token_hex(8))
        username = info.get("username") or uid
        slug     = info.get("slug") or username.lower().replace("_", "-")

        # Kanal bilgilerini cek
        ch = get_channel_info(slug, token)
        broadcaster_id = str(ch.get("broadcaster_user_id") or info.get("user_id") or "")
        chatroom_id    = str(ch.get("chatroom_id") or "")

        data = load_data()
        if uid not in data:
            data[uid] = {"clips": [], "clip_duration": 180, "cooldown": 30, "manual_hls_url": ""}
        data[uid].update({
            "username": username, "channel": slug,
            "chatroom_id": chatroom_id, "broadcaster_id": broadcaster_id,
            "access_token": token, "refresh_token": refresh,
        })
        save_data(data)
        session["uid"] = uid
    except Exception as e:
        return f"Giris hatasi: {e}", 500
    return redirect("/")

@app.route("/logout")
def logout():
    uid = session.pop("uid", None)
    if uid and uid in active_bots:
        active_bots[uid]["running"] = False
    return redirect("/")

@app.route("/settings", methods=["POST"])
def settings_save():
    uid = session.get("uid")
    if not uid: return redirect("/")
    data = load_data()
    if uid in data:
        data[uid].update({
            "channel":       request.form.get("channel", "").strip(),
            "chatroom_id":   request.form.get("chatroom_id", "").strip(),
            "clip_duration": int(request.form.get("clip_duration", 180)),
            "cooldown":      int(request.form.get("cooldown", 30)),
            "manual_hls_url": request.form.get("manual_hls_url", "").strip(),
        })
        save_data(data)
    return redirect("/")

@app.route("/bot/start")
def bot_start():
    uid = session.get("uid")
    if not uid: return redirect("/")
    data = load_data()
    user = data.get(uid, {})
    if uid not in active_bots:
        active_bots[uid] = {"running": False, "clips": user.get("clips", []), "log": []}
    if not active_bots[uid].get("running"):
        active_bots[uid]["running"] = True
        t = threading.Thread(target=run_bot, args=(uid, user), daemon=True)
        t.start()
    return redirect("/")

@app.route("/bot/stop")
def bot_stop():
    uid = session.get("uid")
    if uid and uid in active_bots:
        active_bots[uid]["running"] = False
    return redirect("/")

@app.route("/api/status")
def api_status():
    uid = session.get("uid")
    if not uid: return jsonify({"running": False, "log": [], "clips": []})
    bot = active_bots.get(uid, {})
    return jsonify({
        "running": bot.get("running", False),
        "log":     bot.get("log", [])[-20:],
        "clips":   bot.get("clips", [])[:10],
    })

@app.route("/clips/<path:filename>")
def serve_clip(filename):
    return send_from_directory(CLIPS_DIR, filename)

if __name__ == "__main__":
    app.run(debug=True)
