"""Read-only dashboard for the debate bot's Turso database, now with Discord OAuth & Custom Profiles."""
import os
import re
import threading
import time
import requests

import libsql_experimental as libsql
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, redirect, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

TURSO_URL = os.environ.get("TURSO_URL")
TURSO_TOKEN = os.environ.get("TURSO_TOKEN")
REPLICA_PATH = os.environ.get("REPLICA_PATH", "dashboard-replica.db")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "30"))
FINISHED = "finished"

# Discord OAuth2 Variables
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "1356528530718902386")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "https://debate-leaderboards.onrender.com/callback")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1504171463849021450")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "xander_debate_arena_secure_session_key_7734")

# Proxy and Cookie Fixes for Render HTTPS
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config["SESSION_COOKIE_NAME"] = "debate_arena_auth_session"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

@app.after_request
def prevent_caching(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

_lock = threading.Lock()
_conn = None
_last_sync = 0.0

DEBATE_COLS = ["debate_number", "notion", "affirmative_id", "affirmative_name",
               "negative_id", "negative_name", "winner_id", "result",
               "start_link", "conclusion_link"]
DEBATE_SELECT = ", ".join(DEBATE_COLS)

USER_COLS = ["user_id", "username", "avatar_url", "wins", "losses", "draws", "banner_url", "embed_color"]
USER_SELECT = ", ".join(USER_COLS)


def get_db():
    global _conn, _last_sync
    if _conn is None:
        if not TURSO_URL or not TURSO_TOKEN:
            raise RuntimeError("TURSO_URL and TURSO_TOKEN must be set in environment variables")
        _conn = libsql.connect(REPLICA_PATH, sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
        _conn.sync()
        _last_sync = time.monotonic()
        
        # Auto-upgrade database to ensure all required columns exist (fixes the 500 crash)
        try:
            cols = [r[1] for r in _conn.execute("PRAGMA table_info(users)").fetchall()]
            if "banner_url" not in cols: _conn.execute("ALTER TABLE users ADD COLUMN banner_url TEXT")
            if "embed_color" not in cols: _conn.execute("ALTER TABLE users ADD COLUMN embed_color TEXT")
            if "wins" not in cols: _conn.execute("ALTER TABLE users ADD COLUMN wins INTEGER DEFAULT 0")
            if "losses" not in cols: _conn.execute("ALTER TABLE users ADD COLUMN losses INTEGER DEFAULT 0")
            if "draws" not in cols: _conn.execute("ALTER TABLE users ADD COLUMN draws INTEGER DEFAULT 0")
            _conn.commit()
        except Exception as e:
            app.logger.error(f"Migration error: {e}")
            
    elif time.monotonic() - _last_sync > SYNC_INTERVAL:
        try:
            _conn.sync()
        except Exception:
            app.logger.exception("Turso sync failed; serving cached data")
        _last_sync = time.monotonic()
    return _conn


def query(sql, params=(), cols=None):
    with _lock:
        conn = get_db()
        rows = conn.execute(sql, params).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def execute_write(sql, params=()):
    with _lock:
        conn = get_db()
        conn.execute(sql, params)
        conn.commit()


def safe_url(u):
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else None


def player(uid, name, avatar_url=None):
    return {
        "id": str(uid) if uid is not None else None, 
        "name": name or f"User {uid}",
        "avatar": safe_url(avatar_url)
    }


def normalize_result(d):
    if d["result"] in ("aff", "neg", "draw"):
        return d["result"]
    if d["winner_id"] is not None:
        if d["winner_id"] == d["affirmative_id"]:
            return "aff"
        if d["winner_id"] == d["negative_id"]:
            return "neg"
    return None


def serialize_debate(d):
    return {
        "number": d["debate_number"],
        "notion": d["notion"] or "(no notion recorded)",
        "aff": player(d["affirmative_id"], d["affirmative_name"]),
        "neg": player(d["negative_id"], d["negative_name"]),
        "result": normalize_result(d),
        "start_link": safe_url(d["start_link"]),
        "conclusion_link": safe_url(d["conclusion_link"]),
    }


# --- ROUTES ---

@app.route("/")
@app.route("/u/<path:identifier>")
def index(identifier=None):
    og = None
    if identifier:
        try:
            if identifier.isdigit():
                rows = query(f"SELECT {USER_SELECT} FROM users WHERE user_id = ?", (int(identifier),), USER_COLS)
            else:
                rows = query(f"SELECT {USER_SELECT} FROM users WHERE username COLLATE NOCASE = ?", (identifier,), USER_COLS)
                
            if rows:
                user = rows[0]
                color_hex = user.get("embed_color") or "#a855f7"
                image_url = user.get("banner_url") or user.get("avatar_url")
                wins = user.get("wins") or 0
                
                og = {
                    "title": f"🏆 {wins} Wins | {user['username']}'s Record",
                    "description": f"View {user['username']}'s full debate history on the leaderboards.",
                    "image": image_url,
                    "color": color_hex
                }
        except Exception:
            pass
    return render_template("index.html", og=og)


# --- DISCORD OAUTH2 ---

@app.route("/login")
def login():
    if not DISCORD_CLIENT_SECRET:
        return "DISCORD_CLIENT_SECRET is missing in Render environment", 500
    url = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify%20guilds"
    return redirect(url)


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")
    
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
    if r.status_code != 200:
        return f"Failed to authenticate with Discord: {r.text}", 400
    token = r.json()["access_token"]
    
    user_r = requests.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"})
    if user_r.status_code != 200:
        return "Failed to fetch user data", 400
    user_data = user_r.json()
    
    uid = int(user_data["id"])
    username = user_data.get("global_name") or user_data.get("username")
    avatar_hash = user_data.get("avatar")
    avatar_url = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png" if avatar_hash else None

    # Check if user is in your server
    guilds_r = requests.get("https://discord.com/api/users/@me/guilds", headers={"Authorization": f"Bearer {token}"})
    in_server = False
    if guilds_r.status_code == 200:
        guilds = guilds_r.json()
        in_server = any(str(g["id"]) == DISCORD_GUILD_ID for g in guilds)
    
    execute_write(
        """INSERT INTO users (user_id, username, avatar_url, wins, losses, draws) 
           VALUES (?, ?, ?, 0, 0, 0)
           ON CONFLICT(user_id) DO UPDATE SET 
           username = excluded.username, 
           avatar_url = COALESCE(excluded.avatar_url, users.avatar_url)""",
        (uid, username, avatar_url)
    )
    
    session["user_id"] = uid
    session["username"] = username
    session["avatar_url"] = avatar_url
    session["in_server"] = in_server
    
    return redirect("/")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/api/auth/status")
def auth_status():
    if "user_id" in session:
        return jsonify({
            "logged_in": True, 
            "in_server": session.get("in_server", False),
            "user": {
                "id": str(session["user_id"]), 
                "name": session["username"], 
                "avatar": session["avatar_url"]
            }
        })
    return jsonify({"logged_in": False})


@app.route("/api/user/customize", methods=["POST"])
def customize_profile():
    if "user_id" not in session:
        return jsonify(error="Unauthorized"), 401
    
    data = request.json
    banner = data.get("banner_url")
    color = data.get("embed_color")
    
    if banner and not banner.startswith(("http://", "https://")):
        banner = None
        
    if color and not re.match(r"^#(?:[0-9a-fA-F]{3}){1,2}$", color):
        color = None
        
    execute_write(
        "UPDATE users SET banner_url = ?, embed_color = ? WHERE user_id = ?",
        (banner, color, session["user_id"])
    )
    return jsonify(success=True)


# --- DATA APIS ---

@app.route("/api/leaderboard")
def api_leaderboard():
    rows = query(
        f"""SELECT {USER_SELECT} FROM users
           WHERE wins + losses + draws > 0
           ORDER BY wins DESC, losses ASC, username COLLATE NOCASE LIMIT 200""",
        cols=USER_COLS)
    out = []
    for i, r in enumerate(rows, 1):
        wins = r.get("wins") or 0
        losses = r.get("losses") or 0
        draws = r.get("draws") or 0
        games = wins + losses + draws
        out.append({
            "rank": i, 
            **player(r["user_id"], r["username"], r["avatar_url"]),
            "wins": wins, "losses": losses, "draws": draws,
            "win_rate": round(100 * wins / games) if games else 0
        })
    return jsonify({"rows": out})


@app.route("/api/debates")
def api_debates():
    order = "ASC" if request.args.get("sort") == "oldest" else "DESC"
    per_page = min(max(request.args.get("per_page", 12, type=int), 5), 50)
    page = max(request.args.get("page", 1, type=int), 1)
    total = query("SELECT COUNT(*) FROM debates WHERE status = ?", (FINISHED,), ["n"])[0]["n"]
    rows = query(
        f"""SELECT {DEBATE_SELECT} FROM debates WHERE status = ?
            ORDER BY debate_number {order} LIMIT ? OFFSET ?""",
        (FINISHED, per_page, (page - 1) * per_page), DEBATE_COLS)
    return jsonify({"total": total, "page": page, "per_page": per_page,
                    "pages": max(1, -(-total // per_page)),
                    "debates": [serialize_debate(d) for d in rows]})


def profile(user):
    uid = user["user_id"]
    rows = query(
        f"""SELECT {DEBATE_SELECT} FROM debates
            WHERE (affirmative_id = ? OR negative_id = ?) AND status = ?
            ORDER BY debate_number DESC""", (uid, uid, FINISHED), DEBATE_COLS)
            
    fallacies = []
    try:
        f_rows = query(
            "SELECT debate_number, fallacy_name, reasoning_link FROM fallacies WHERE user_id = ? ORDER BY id DESC",
            (uid,), ["debate_number", "fallacy_name", "reasoning_link"]
        )
        fallacies = [{"debate_number": r["debate_number"], "fallacy_name": r["fallacy_name"], "reasoning_link": safe_url(r["reasoning_link"])} for r in f_rows]
    except Exception:
        pass

    debates, outcomes = [], []
    for d in rows:
        item = serialize_debate(d)
        is_aff = d["affirmative_id"] == uid
        res = item["result"]
        outcome = None if res is None else "D" if res == "draw" else "W" if (res == "aff") == is_aff else "L"
        item.update(side="aff" if is_aff else "neg", outcome=outcome, opponent=item["neg"] if is_aff else item["aff"])
        debates.append(item)
        if outcome: outcomes.append(outcome)
            
    streak = 0
    for o in outcomes:
        if o != outcomes[0]: break
        streak += 1
        
    wins = user.get("wins") or 0
    losses = user.get("losses") or 0
    draws = user.get("draws") or 0
    games = wins + losses + draws
    
    return {
        **player(uid, user["username"], user["avatar_url"]),
        "banner_url": safe_url(user.get("banner_url")),
        "embed_color": user.get("embed_color"),
        "wins": wins, "losses": losses, "draws": draws,
        "win_rate": round(100 * wins / games) if games else 0,
        "streak": {"kind": outcomes[0], "length": streak} if outcomes else None,
        "debates": debates,
        "fallacies": fallacies
    }


@app.route("/api/user")
def api_user():
    q = (request.args.get("q") or "").strip()
    if not q: return jsonify(error="Enter a username or user ID."), 400
    base = f"SELECT {USER_SELECT} FROM users WHERE "
    matches = []
    
    if re.fullmatch(r"\d{1,20}", q, re.ASCII):
        matches = query(base + "user_id = ?", (int(q),), USER_COLS)
    if not matches:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matches = query(base + """username LIKE ? ESCAPE '\\' ORDER BY wins DESC, username COLLATE NOCASE LIMIT 10""", (like,), USER_COLS)
    if not matches:
        return jsonify(error=f"No debater found for \u201c{q}\u201d."), 404
    exact = [m for m in matches if (m["username"] or "").lower() == q.lower()]
    if len(matches) == 1 or len(exact) == 1:
        return jsonify(user=profile(exact[0] if exact else matches[0]))
    return jsonify(matches=[player(m["user_id"], m["username"], m["avatar_url"]) for m in matches])


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException): return e
    app.logger.exception("Request failed")
    return jsonify(error="Couldn't load data from the database. Check the server log."), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=True)
