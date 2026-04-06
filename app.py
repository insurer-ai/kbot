from flask import Flask, render_template, request, redirect, session, jsonify, url_for, send_from_directory
import threading, json, os, time, hashlib, base64, secrets, subprocess, glob, re
from urllib.parse import urlencode, parse_qs, urlparse
import urllib.request, ssl, gzip
from datetime import datetime

app = Flask(__name__)
app.secret_key = "clipbot_secret_key_change_this"

# ── Kick OAuth ────────────────────────────────────────────
KICK_CLIENT_ID     = "01KNFT27H9FKB3KYPN7AWBYKK4"
KICK_CLIENT_SECRET = "4b83a5d95ca99bbc6fa1f8d9630dce2c8b5caf3682c1fc1395a0ae0fd721c0f9"
REDIRECT_URI       = "https://kbot-u8we.onrender.com/callbackk"
SCOPES             = "user:read channel:read events:subscribe"

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR = os.path.join(BASE_DIR, "static", "clips")
DATA_FILE = os.path.join(BASE_DIR, "users.json")
os.makedirs(CLIPS_DIR, exist_ok=True)

# ── Veri yönetimi ─────────────────────────────────────────
def load_data():
    try:
        with open(DATA_FILE) as f: return json.load(f)
    except: return {}

def save_data(d):
    with open(DATA_FILE, "w") as f: json.dump(d, f, indent=2)

# ── SSL context ───────────────────────────────────────────
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

# ── OAuth helpers ─────────────────────────────────────────
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

# ── Bot per kullanıcı ─────────────────────────────────────
active_bots = {}  # user_id -> {"running": bool, "thread": Thread, "clips": [], "log": []}

def bot_log(uid, msg):
    if uid in active_bots:
        active_bots[uid]["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        active_bots[uid]["log"] = active_bots[uid]["log"][-100:]

def get_hls_url(channel, token):
    try:
        resp = fetch(f"https://api.kick.com/public/v1/channels?slug={channel}", token=token)
        d = json.loads(resp)
        items = d.get("data") or []
        if not items: return None
        buid = items[0].get("broadcaster_user_id")
        if not buid: return None
        resp2 = fetch(f"https://api.kick.com/public/v1/livestreams?broadcaster_user_id={buid}", token=token)
        d2 = json.loads(resp2)
        items2 = d2.get("data") or []
        if not items2: return None
        item = items2[0]
        return item.get("playback_url") or (item.get("stream") or {}).get("playback_url")
    except Exception as e:
        return None

def record_clip(uid, channel, token, manual_url, duration, clips_dir):
    hls = manual_url or get_hls_url(channel, token)
    if not hls:
        bot_log(uid, "Stream URL alinamadi!")
        return None
    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"clip_{ts}_{uid}.mp4"
    fpath = os.path.join(clips_dir, fname)
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", hls, "-t", str(duration),
            "-c", "copy", "-movflags", "+faststart", fpath
        ], timeout=duration+30, capture_output=True)
        if os.path.exists(fpath) and os.path.getsize(fpath) > 1000:
            bot_log(uid, f"Klip kaydedildi: {fname}")
            return fname
    except Exception as e:
        bot_log(uid, f"FFmpeg hatasi: {e}")
    return None

def subscribe_kick_events(uid, token, channel_name, broadcaster_id):
    """Kick Event Subscription API ile chat mesajlarini dinler."""
    import urllib.request, ssl, json, gzip

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    hdrs = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 Chrome/122",
    }

    # Webhook endpoint - kendi sitemiz
    webhook_url = f"https://kbot-u8we.onrender.com/webhook/{uid}"

    # Event subscription olustur
    payload = json.dumps({
        "events": [
            {
                "name": "chat.message.sent",
                "version": 1,
                "broadcaster_user_id": int(broadcaster_id)
            }
        ],
        "method": "webhook",
        "webhook_url": webhook_url
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.kick.com/public/v1/events/subscriptions",
            data=payload, headers=hdrs, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            raw = r.read()
        if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
        result = json.loads(raw)
        bot_log(uid, f"Event subscription olusturuldu: {result}")
        return result.get("data", {}).get("id")
    except Exception as e:
        bot_log(uid, f"Event subscription hatasi: {e}")
        return None

def get_broadcaster_id(channel, token):
    """Kanal slug'undan broadcaster_user_id alir."""
    import urllib.request, ssl, gzip
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(
            f"https://api.kick.com/public/v1/channels?slug={channel}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 Chrome/122",
            }
        )
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            raw = r.read()
        if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
        d = json.loads(raw)
        items = d.get("data") or []
        if items:
            return items[0].get("broadcaster_user_id")
    except Exception as e:
        pass
    return None

def poll_chat(uid, settings):
    """Bot ana dongusu - once Event Subscription dener, calismassa polling."""
    channel     = settings.get("channel", "").strip().lower()
    chatroom_id = settings.get("chatroom_id", "").strip()
    token       = settings.get("access_token", "")
    manual_url  = settings.get("manual_hls_url", "")
    duration    = int(settings.get("clip_duration", 180))
    cooldown    = int(settings.get("cooldown", 30))

    bot_log(uid, f"Bot basladi → kick.com/{channel}")

    # Broadcaster ID al
    broadcaster_id = get_broadcaster_id(channel, token)
    bot_log(uid, f"Broadcaster ID: {broadcaster_id}")

    if broadcaster_id:
        # Event subscription ile bagli kal
        sub_id = subscribe_kick_events(uid, token, channel, broadcaster_id)
        if sub_id:
            bot_log(uid, f"✅ Webhook aktif! Chat mesajlari gelecek.")
            # Sadece bekle - mesajlar /webhook/{uid} endpoint'ine gelecek
            while active_bots.get(uid, {}).get("running"):
                time.sleep(5)
            bot_log(uid, "Bot durduruldu.")
            return

    # Fallback: polling (lokal test icin)
    bot_log(uid, "⚠️ Event subscription basarisiz, polling deneniyor...")
    cooldowns = {}
    seen_ids  = set()
    clip_in_progress = False

    while active_bots.get(uid, {}).get("running"):
        try:
            import urllib.request, ssl, gzip
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            hdrs = {
                "User-Agent": "Mozilla/5.0 Chrome/122",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}" if token else "",
            }
            msgs = []
            for ep in [
                f"https://kick.com/api/v2/channels/{channel}/messages",
                f"https://kick.com/api/v1/chatrooms/{chatroom_id}/messages",
            ]:
                try:
                    req = urllib.request.Request(ep, headers=hdrs)
                    with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                        raw = r.read()
                    if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
                    d = json.loads(raw.decode("utf-8", errors="ignore"))
                    msgs = (d.get("data") or {}).get("messages") or d.get("messages") or []
                    if msgs: break
                except: continue

            for m in (msgs or []):
                mid = m.get("id")
                if not mid or mid in seen_ids: continue
                seen_ids.add(mid)
                if len(seen_ids) > 500: seen_ids = set(list(seen_ids)[-200:])
                msg_content = (m.get("content") or "").strip().lower()
                sender = m.get("sender") or {}
                user = sender.get("username") or "?"
                if msg_content == "!clip" and not clip_in_progress:
                    now = time.time()
                    if now - cooldowns.get(user, 0) < cooldown:
                        continue
                    cooldowns[user] = now
                    clip_in_progress = True
                    cu = user
                    def do_clip(cu=cu):
                        nonlocal clip_in_progress
                        fname = record_clip(uid, channel, token, manual_url, duration, CLIPS_DIR)
                        if fname:
                            active_bots[uid]["clips"].insert(0, {"file": fname, "user": cu,
                                "ts": datetime.now().strftime("%d.%m %H:%M"), "url": f"/clips/{fname}"})
                            active_bots[uid]["clips"] = active_bots[uid]["clips"][:30]
                            data = load_data()
                            if uid in data:
                                data[uid]["clips"] = active_bots[uid]["clips"]
                                save_data(data)
                        clip_in_progress = False
                    threading.Thread(target=do_clip, daemon=True).start()
        except Exception as e:
            bot_log(uid, f"Hata: {str(e)[:60]}")
        time.sleep(2)

    bot_log(uid, "Bot durduruldu.")

# ── Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    uid = session.get("uid")
    data = load_data()
    user = data.get(uid) if uid else None
    bot_info = active_bots.get(uid, {})
    return render_template("index.html",
        user=user,
        running=bot_info.get("running", False),
        clips=bot_info.get("clips", (user or {}).get("clips", [])),
        log=bot_info.get("log", [])[-20:],
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
        tokens   = exchange_code(code, session.pop("pkce_verifier",""))
        token    = tokens.get("access_token")
        refresh  = tokens.get("refresh_token","")
        info     = get_user_info(token)
        uid      = str(info.get("user_id") or info.get("id") or secrets.token_hex(8))
        username = info.get("username") or info.get("name") or uid

        data = load_data()
        if uid not in data:
            data[uid] = {"username": username, "channel": username, "chatroom_id": "",
                         "clip_duration": 180, "cooldown": 30, "manual_hls_url": "", "clips": []}
        data[uid]["access_token"]  = token
        data[uid]["refresh_token"] = refresh
        data[uid]["username"]      = username
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
def settings():
    uid = session.get("uid")
    if not uid: return redirect("/")
    data = load_data()
    if uid in data:
        data[uid].update({
            "channel":       request.form.get("channel","").strip(),
            "chatroom_id":   request.form.get("chatroom_id","").strip(),
            "clip_duration": int(request.form.get("clip_duration", 180)),
            "cooldown":      int(request.form.get("cooldown", 30)),
            "manual_hls_url": request.form.get("manual_hls_url","").strip(),
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
        active_bots[uid] = {"running": False, "clips": user.get("clips",[]), "log": []}
    if not active_bots[uid].get("running"):
        active_bots[uid]["running"] = True
        t = threading.Thread(target=poll_chat, args=(uid, user), daemon=True)
        t.start()
        active_bots[uid]["thread"] = t
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

@app.route("/webhook/<uid>", methods=["POST"])
def webhook_receiver(uid):
    """Kick Event Subscription webhook alici."""
    from flask import request as freq
    import hmac, hashlib

    data = freq.get_json(force=True) or {}
    event = data.get("event") or {}
    event_type = event.get("type", "") or data.get("type", "")

    # chat.message.sent eventi
    if "chat" in event_type.lower() or "message" in event_type.lower():
        msg_data = event.get("data") or data.get("data") or {}
        content_text = (msg_data.get("content") or "").strip().lower()
        sender = msg_data.get("sender") or {}
        user = sender.get("username") or sender.get("slug") or "?"

        bot_log(uid, f"Webhook mesaj: {user}: {content_text[:30]}")

        if content_text == "!clip" and uid in active_bots and active_bots[uid].get("running"):
            settings = load_data().get(uid, {})
            channel   = settings.get("channel", "")
            token     = settings.get("access_token", "")
            manual_url = settings.get("manual_hls_url", "")
            duration  = int(settings.get("clip_duration", 180))

            bot_log(uid, f"✅ !clip alindi → {user}")
            active_bots[uid]["clip_in_progress"] = True

            def do_clip(cu=user):
                fname = record_clip(uid, channel, token, manual_url, duration, CLIPS_DIR)
                if fname:
                    active_bots[uid]["clips"].insert(0, {
                        "file": fname, "user": cu,
                        "ts": datetime.now().strftime("%d.%m %H:%M"),
                        "url": f"/clips/{fname}"
                    })
                    active_bots[uid]["clips"] = active_bots[uid]["clips"][:30]
                    d2 = load_data()
                    if uid in d2:
                        d2[uid]["clips"] = active_bots[uid]["clips"]
                        save_data(d2)
                active_bots[uid]["clip_in_progress"] = False

            threading.Thread(target=do_clip, daemon=True).start()

    return {"status": "ok"}, 200

@app.route("/test")
def test_kick():
    """Kick API erişimini test eder."""
    import urllib.request, ssl, gzip
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    results = {}
    test_urls = [
        "https://kick.com/api/v2/channels/catikkas-gozluk/messages",
        "https://kick.com/api/v1/chatrooms/79537432/messages",
        "https://kick.com/api/v2/channels/catikkas-gozluk",
    ]
    for url in test_urls:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                raw = r.read()
            if raw[:2] == b'\x1f\x8b': raw = gzip.decompress(raw)
            results[url] = f"OK ({len(raw)} bytes): {raw[:100].decode(errors='ignore')}"
        except Exception as e:
            results[url] = f"HATA: {str(e)}"
    return "<br><br>".join([f"<b>{k}</b><br>{v}" for k,v in results.items()])

if __name__ == "__main__":
    app.run(debug=True)
