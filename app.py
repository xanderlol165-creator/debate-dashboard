"""Read-only dashboard for the debate bot's Turso database."""
import os
import re
import threading
import time

import libsql_experimental as libsql
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

load_dotenv()

TURSO_URL = os.getenv("TURSO_URL")
TURSO_TOKEN = os.getenv("TURSO_TOKEN")
REPLICA_PATH = os.getenv("REPLICA_PATH", "dashboard-replica.db")  # local read cache
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "30"))
FINISHED = "finished"  # debates.status value counted as a finished debate

app = Flask(__name__)
_lock = threading.Lock()
_conn = None
_last_sync = 0.0

DEBATE_COLS = ["debate_number", "notion", "affirmative_id", "affirmative_name",
               "negative_id", "negative_name", "winner_id", "result",
               "start_link", "conclusion_link"]
DEBATE_SELECT = ", ".join(DEBATE_COLS)
USER_COLS = ["user_id", "username", "wins", "losses", "draws"]


def query(sql, params=(), cols=None):
    """Run a SELECT (the only kind this app ever issues) and return dict rows."""
    global _conn, _last_sync
    with _lock:
        if _conn is None:
            if not TURSO_URL or not TURSO_TOKEN:
                raise RuntimeError("TURSO_URL and TURSO_TOKEN must be set in .env")
            _conn = libsql.connect(REPLICA_PATH, sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
            _conn.sync()
            _last_sync = time.monotonic()
        elif time.monotonic() - _last_sync > SYNC_INTERVAL:
            try:
                _conn.sync()
            except Exception:
                app.logger.exception("Turso sync failed; serving cached data")
            _last_sync = time.monotonic()
        rows = _conn.execute(sql, params).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def safe_url(u):
    return u if isinstance(u, str) and u.startswith(("https://", "http://")) else None


def player(uid, name):
    # Discord IDs exceed JS's safe integer range, so send them as strings.
    return {"id": str(uid) if uid is not None else None, "name": name or f"User {uid}"}


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/leaderboard")
def api_leaderboard():
    rows = query(
        """SELECT user_id, username, wins, losses, draws FROM users
           WHERE wins + losses + draws > 0
           ORDER BY wins DESC, losses ASC, username COLLATE NOCASE LIMIT 200""",
        cols=USER_COLS)
    out = []
    for i, r in enumerate(rows, 1):
        games = r["wins"] + r["losses"] + r["draws"]
        out.append({"rank": i, **player(r["user_id"], r["username"]),
                    "wins": r["wins"], "losses": r["losses"], "draws": r["draws"],
                    "win_rate": round(100 * r["wins"] / games) if games else 0})
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
    debates, outcomes = [], []
    for d in rows:
        item = serialize_debate(d)
        is_aff = d["affirmative_id"] == uid
        res = item["result"]
        outcome = None if res is None else "D" if res == "draw" else \
            "W" if (res == "aff") == is_aff else "L"
        item.update(side="aff" if is_aff else "neg", outcome=outcome,
                    opponent=item["neg"] if is_aff else item["aff"])
        debates.append(item)
        if outcome:
            outcomes.append(outcome)
    streak = 0
    for o in outcomes:
        if o != outcomes[0]:
            break
        streak += 1
    games = user["wins"] + user["losses"] + user["draws"]
    return {**player(uid, user["username"]), "wins": user["wins"],
            "losses": user["losses"], "draws": user["draws"],
            "win_rate": round(100 * user["wins"] / games) if games else 0,
            "streak": {"kind": outcomes[0], "length": streak} if outcomes else None,
            "debates": debates}


@app.route("/api/user")
def api_user():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="Enter a username or user ID."), 400
    base = "SELECT user_id, username, wins, losses, draws FROM users WHERE "
    matches = []
    if re.fullmatch(r"\d{1,18}", q, re.ASCII):
        matches = query(base + "user_id = ?", (int(q),), USER_COLS)
    if not matches:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matches = query(base + """username LIKE ? ESCAPE '\\'
                        ORDER BY wins DESC, username COLLATE NOCASE LIMIT 10""",
                        (like,), USER_COLS)
    if not matches:
        return jsonify(error=f"No debater found for \u201c{q}\u201d."), 404
    exact = [m for m in matches if (m["username"] or "").lower() == q.lower()]
    if len(matches) == 1 or len(exact) == 1:
        return jsonify(user=profile(exact[0] if exact else matches[0]))
    return jsonify(matches=[player(m["user_id"], m["username"]) for m in matches])


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Request failed")
    return jsonify(error="Couldn't load data from the database. Check the server log."), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 5000)), debug=True)
