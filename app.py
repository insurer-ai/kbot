from flask import Flask, render_template, request, redirect, session, jsonify, send_from_directory
import threading, json, os, time, hashlib, base64, secrets, subprocess, glob, gzip
from urllib.parse import urlencode
import urllib.request, ssl
from datetime import datetime

app = Flask(__name__)
app.secret_key = "clipbot_secret_2024_xK9mP"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30

KICK_CLIENT_ID     = "01KNFT27H9FKB3KYPN7AWBYKK4"
KICK_CLIENT_SECRET = "4b83a5d95ca99bbc6fa1f8d9630dce2c8b5caf3682c1fc1395a0ae0fd721c0f9"
REDIRECT_URI       = "https://kbot-u8we.onrender.com/callback"
SCOPES             = "user:read channel:read events:subscribe chat:write"
WEBHOOK_URL        = "https://kbot-u8we.onrender.com/webhook/kick"

SUPABASE_URL = "https://ciifjrpwvjtzamskwufu.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImNpaWZqcnB3dmp0emFtc2t3dWZ1Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3NTQ3MTUxNCwiZXhwIjoyMDkxMDQ3NTE0fQ.KnlTGTXaXnIs9wHg-nB43dv6QqH4WWRB23xw6hCr8ow"

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR = os.path.join(BASE_DIR, "static", "clips")
os.makedirs(CLIPS_DIR, exist_ok=True)

# ── Supabase helpers ──────────────────────────────────────
def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def sb_get(table, filters=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    req = urllib.request.Request(url, headers=sb_headers())
    with urllib.request.urlopen(req, timeout=10, context=make_ctx()) as r:
        return json.loads(r.read())

def sb_upsert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(data).encode()
    hdrs = {**sb_headers(), "Prefer": "resolution=merge-duplicates,return=representation"}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=10, context=make_ctx()) as r:
        return json.loads(r.read())

def sb_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=sb_headers(), method="POST")
    with urllib.request.urlopen(req, timeout=10, context=make_ctx()) as r:
        return json.loads(r.read())

def sb_upload(bucket, path, data, content_type="video/mp4"):
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}"
    hdrs = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
    }
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120, context=make_ctx()) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def sb_public_url(bucket, path):
    return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{path}"

def load_user(uid):
    try:
        rows = sb_get("users", f"uid=eq.{uid}")
        return rows[0] if rows else None
    except: return None

def save_user(data):
    try: sb_upsert("users", data)
    except Exception as e: print(f"save_user error: {e}")

def get_user_clips(uid, limit=30):
    try:
        rows = sb_get("clips", f"uid=eq.{uid}&order=created_at.desc&limit={limit}")
        return rows
    except: return []

def save_clip_record(uid, filename, triggered_by, public_url):
    try:
        sb_insert("clips", {
            "uid": uid,
            "filename": filename,
            "triggered_by": triggered_by,
            "url": public_url,
            "created_at": datetime.utcnow().isoformat()
        })
    except Exception as e:
        print(f"save_clip error: {e}")

def make_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def fetch(url, token=None, method="GET", body=None, content_type=None):
    hdrs = {
        "User-Agent": "Mozilla/5.0 Chrome/122",
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
    except: return {}

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

def record_and_upload(uid, channel, token, manual_url, duration, triggered_by):
    hls = manual_url or ""
    if not hls:
        bot_log(uid, "Stream URL yok — Manuel URL gir!")
        return None

    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_f   = os.path.join(CLIPS_DIR, f"raw_{ts}.mp4")
    final_f = os.path.join(CLIPS_DIR, f"clip_{ts}.mp4")
    safe_ch = channel.replace("'","").replace(":","").replace("\\","")

    try:
        bot_log(uid, f"Kaydediliyor ({duration}s)...")
        subprocess.run([
            "ffmpeg", "-y", "-i", hls,
            "-t", str(duration),
            "-vf", "scale=640:360",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "96k", "-r", "30",
            raw_f
        ], timeout=duration+120, capture_output=True)

        if not os.path.exists(raw_f) or os.path.getsize(raw_f) < 10000:
            bot_log(uid, "Ham kayit basarisiz")
            return None

        bot_log(uid, "Dikey formata cevriliyor (9:16)...")

        fc = (
            "[0:v]scale=720:1280:force_original_aspect_ratio=increase,"
            "crop=720:1280,boxblur=20:5[bg];"
            "[0:v]scale=720:720:force_original_aspect_ratio=decrease,"
            "pad=720:720:(ow-iw)/2:(oh-ih)/2:black@0[game];"
            "[bg][game]overlay=0:0[base];"
            "[base]"
            "drawbox=x=0:y=420:w=720:h=380:color=black@0.75:t=fill,"
            "drawbox=x=0:y=420:w=720:h=3:color=0x53FC18:t=fill,"
            "drawbox=x=0:y=797:w=720:h=3:color=0x53FC18:t=fill,"
            f"drawtext=text='K':fontsize=140:fontcolor=0x53FC18:"
            f"x=(w-text_w)/2:y=450:shadowcolor=black:shadowx=4:shadowy=4,"
            f"drawtext=text='{safe_ch}':fontsize=52:fontcolor=white:"
            f"x=(w-text_w)/2:y=610:shadowcolor=black@0.9:shadowx=2:shadowy=2,"
            f"drawtext=text='kick.com/{safe_ch}':fontsize=30:fontcolor=0x53FC18:"
            f"x=(w-text_w)/2:y=680:shadowcolor=black@0.8:shadowx=1:shadowy=1,"
            "drawbox=x=0:y=1240:w=720:h=40:color=black@0.8:t=fill,"
            f"drawtext=text='@{safe_ch}  kick.com/{safe_ch}':"
            f"fontsize=18:fontcolor=white@0.8:x=(w-text_w)/2:y=1250"
            "[out]"
        )

        subprocess.run([
            "ffmpeg", "-y", "-i", raw_f,
            "-filter_complex", fc,
            "-map", "[out]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-r", "30", "-movflags", "+faststart",
            final_f
        ], timeout=300, capture_output=True)

        if os.path.exists(raw_f): os.remove(raw_f)

        if not os.path.exists(final_f) or os.path.getsize(final_f) < 10000:
            bot_log(uid, "Dikey montaj basarisiz")
            return None

        # Supabase Storage'a yukle
        bot_log(uid, "Supabase'e yukleniyor...")
        fname = f"clip_{ts}.mp4"
        with open(final_f, "rb") as f:
            video_data = f.read()

        result = sb_upload("clips", f"{uid}/{fname}", video_data)
        if os.path.exists(final_f): os.remove(final_f)

        if "error" in result:
            bot_log(uid, f"Yukleme hatasi: {result['error']}")
            return None

        public_url = sb_public_url("clips", f"{uid}/{fname}")
        save_clip_record(uid, fname, triggered_by, public_url)
        bot_log(uid, f"✅ Klip hazir!")

        # Active bots cache guncelle
        if uid in active_bots:
            active_bots[uid]["clips"].insert(0, {
                "triggered_by": triggered_by,
                "url": public_url,
                "created_at": datetime.now().strftime("%d.%m %H:%M"),
            })
            active_bots[uid]["clips"] = active_bots[uid]["clips"][:30]

        return public_url

    except Exception as e:
        bot_log(uid, f"Kayit hatasi: {str(e)[:80]}")
        for f in [raw_f, final_f]:
            if os.path.exists(f): os.remove(f)
        return None

def run_bot(uid, user_data):
    token          = user_data.get("access_token", "")
    broadcaster_id = user_data.get("broadcaster_id", "")
    channel        = user_data.get("channel", "")

    bot_log(uid, f"Bot basladi → kick.com/{channel}")

    if broadcaster_id:
        subscribe_events(uid, token, broadcaster_id)
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
    broadcaster   = data.get("broadcaster") or {}
    sender_info   = data.get("sender") or data.get("chatter") or {}
    content_text  = (data.get("content") or data.get("message") or "").strip()
    broadcaster_id = str(broadcaster.get("user_id") or "")
    msg_user      = sender_info.get("username") or "?"

    # Broadcaster'a ait kullaniciyi bul
    target_uid = None
    try:
        rows = sb_get("users", f"broadcaster_id=eq.{broadcaster_id}")
        if rows: target_uid = rows[0]["uid"]
    except: pass

    if not target_uid:
        for uid2 in active_bots:
            if active_bots[uid2].get("running"):
                target_uid = uid2
                break

    if target_uid:
        bot_log(target_uid, f"Chat: {msg_user}: {content_text[:40]}")

    if content_text.lower() == "!clip" and target_uid and active_bots.get(target_uid, {}).get("running"):
        user_data = load_user(target_uid) or {}
        bot_log(target_uid, f"🎮 !clip → {msg_user}")

        def do_clip(cu=msg_user, tuid=target_uid, ud=user_data):
            record_and_upload(
                tuid, ud.get("channel",""), ud.get("access_token",""),
                ud.get("manual_hls_url",""), int(ud.get("clip_duration",180)), cu
            )
        threading.Thread(target=do_clip, daemon=True).start()

    return {"status": "ok"}, 200

# ── Routes ────────────────────────────────────────────────
@app.route("/")
def index():
    uid  = session.get("uid")
    user = load_user(uid) if uid else None
    bot  = active_bots.get(uid, {})
    clips = bot.get("clips") or get_user_clips(uid) if uid else []
    # Tarihe gore grupla
    from collections import defaultdict
    grouped = defaultdict(list)
    for c in clips:
        day = (c.get("created_at") or "")[:10]
        grouped[day].append(c)
    return render_template("index.html",
        user=user, running=bot.get("running", False),
        clips=clips, grouped_clips=dict(grouped),
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
        tokens  = exchange_code(code, session.pop("pkce_verifier",""))
        token   = tokens.get("access_token")
        refresh = tokens.get("refresh_token","")
        info    = get_user_info(token)
        uid     = str(info.get("user_id") or info.get("id") or secrets.token_hex(8))
        username = info.get("username") or uid
        slug     = info.get("slug") or username.lower().replace("_","-")

        ch = get_channel_info(slug, token)
        broadcaster_id = str(ch.get("broadcaster_user_id") or info.get("user_id") or "")
        chatroom_id    = str(ch.get("chatroom_id") or "")

        existing = load_user(uid) or {}
        save_user({
            "uid": uid, "username": username, "channel": slug,
            "chatroom_id": chatroom_id, "broadcaster_id": broadcaster_id,
            "access_token": token, "refresh_token": refresh,
            "clip_duration": existing.get("clip_duration", 180),
            "cooldown": existing.get("cooldown", 30),
            "manual_hls_url": existing.get("manual_hls_url", ""),
        })
        session.permanent = True
        session["uid"] = uid
    except Exception as e:
        return f"Giris hatasi: {e}", 500
    return redirect("/")

@app.route("/auto-login", methods=["POST"])
def auto_login():
    uid = request.json.get("uid","").strip()
    if uid and load_user(uid):
        session.permanent = True
        session["uid"] = uid
        return {"ok": True}
    return {"ok": False}, 401

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
    user = load_user(uid) or {}
    user.update({
        "uid": uid,
        "channel":       request.form.get("channel","").strip(),
        "chatroom_id":   request.form.get("chatroom_id","").strip(),
        "clip_duration": int(request.form.get("clip_duration",180)),
        "cooldown":      int(request.form.get("cooldown",30)),
        "manual_hls_url": request.form.get("manual_hls_url","").strip(),
    })
    save_user(user)
    return redirect("/")

@app.route("/bot/start")
def bot_start():
    uid = session.get("uid")
    if not uid: return redirect("/")
    user = load_user(uid) or {}
    if uid not in active_bots:
        active_bots[uid] = {"running": False, "clips": get_user_clips(uid), "log": []}
    if not active_bots[uid].get("running"):
        active_bots[uid]["running"] = True
        threading.Thread(target=run_bot, args=(uid, user), daemon=True).start()
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
