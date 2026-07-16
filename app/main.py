import base64
import email
import email.header
import email.policy
import email.utils
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from html.parser import HTMLParser
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import redis
except ImportError:
    redis = None


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "pickup.db"
DOMAIN_NAMES = [item.strip().lower() for item in os.environ.get("MAIL_DOMAINS", "bbbnn.me").split(",") if item.strip()]
BASE_URL = os.environ.get("BASE_URL", "https://mail.bbbnn.me").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
INGEST_SECRET = os.environ.get("INGEST_SECRET", "")
DEFAULT_MESSAGE_LIMIT = int(os.environ.get("DEFAULT_MESSAGE_LIMIT", "100"))
MESSAGE_RETENTION_SECONDS = 60 * 60
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "60"))
REDIS_URL = os.environ.get("REDIS_URL", "")
BODY_LIMIT = 50000
PER_PAGE = 20
BATCH_QUEUE_ALL_KEY = "pickup:mailbox_queue:all"
BATCH_QUEUE_OWNER_PREFIX = "pickup:mailbox_queue:owner:"
OLD_BATCH_CODE_REGEX = r"\b(\d{4,6}|[A-Za-z]{4,6}|[A-Za-z0-9]{4,6})\b"
BATCH_CODE_REGEX = r"\b(\d{4,8}|[A-Z0-9]{2,8}(?:-[A-Z0-9]{2,8})+|(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,8})\b"


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_urlsafe(32))
# The container port is bound to localhost, so forwarded headers can only come
# from the host-side reverse proxy managed by 1Panel.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=BASE_URL.startswith("https://"),
)
_db_init_lock = threading.Lock()
_db_initialized = False
_cleanup_lock = threading.Lock()
_cleanup_started = False
_cleanup_stop = threading.Event()
_redis_client = None


CSS_PATH = Path(__file__).parent / "static" / "app.css"


def css_version():
    try:
        digest = hashlib.md5(CSS_PATH.read_bytes()).hexdigest()[:8]
    except OSError:
        digest = "dev"
    return digest


@app.context_processor
def inject_globals():
    return {"css_version": css_version()}


@app.template_filter("safe_email")
def safe_email(value):
    return Markup(str(escape(value or "")).replace("@", "&#64;"))


TZ_CST = timezone(timedelta(hours=8))


@app.template_filter("fmt_time")
def format_time(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return str(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(TZ_CST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def extract_code(text, pattern):
    if not pattern:
        return ""
    try:
        rx = re.compile(pattern)
    except re.error:
        return ""
    match = rx.search(text or "")
    if not match:
        return ""
    return match.group(1) if match.groups() else match.group(0)


def batch_code_regex():
    pattern = session.get("batch_regex")
    if not pattern or pattern == OLD_BATCH_CODE_REGEX:
        return BATCH_CODE_REGEX
    return pattern


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def retention_cutoff():
    return (datetime.now(timezone.utc) - timedelta(seconds=MESSAGE_RETENTION_SECONDS)).isoformat(timespec="seconds")


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def redis_conn():
    global _redis_client
    if not REDIS_URL or redis is None:
        return None
    if _redis_client is None:
        try:
            _redis_client = redis.Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1, decode_responses=True)
            _redis_client.ping()
        except Exception:
            app.logger.exception("Redis queue cache is unavailable; falling back to SQLite ordering")
            _redis_client = False
    return _redis_client or None


def owner_queue_key(owner_id):
    return f"{BATCH_QUEUE_OWNER_PREFIX}{owner_id or 0}"


def queue_score(last_received_at, latest_message_id, link_id):
    if not last_received_at:
        return float(link_id or 0)
    try:
        dt = datetime.fromisoformat(last_received_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return float(link_id or 0)
    return dt.timestamp() * 1000000 + int(latest_message_id or 0)


def queue_upsert_link(link):
    client = redis_conn()
    if not client:
        return
    score = queue_score(link["last_received_at"], link["latest_message_id"], link["id"])
    member = link["email"]
    try:
        client.zadd(BATCH_QUEUE_ALL_KEY, {member: score})
        if link["owner_id"] is not None:
            client.zadd(owner_queue_key(link["owner_id"]), {member: score})
    except Exception:
        app.logger.exception("Failed to update Redis mailbox queue")


def queue_remove_link(email, owner_id=None):
    client = redis_conn()
    if not client:
        return
    try:
        client.zrem(BATCH_QUEUE_ALL_KEY, email)
        if owner_id is not None:
            client.zrem(owner_queue_key(owner_id), email)
    except Exception:
        app.logger.exception("Failed to remove mailbox from Redis queue")


def rebuild_redis_queue(conn):
    client = redis_conn()
    if not client:
        return
    try:
        client.delete(BATCH_QUEUE_ALL_KEY)
        owner_ids = [row["owner_id"] for row in conn.execute("SELECT DISTINCT owner_id FROM pickup_link WHERE owner_id IS NOT NULL")]
        if owner_ids:
            client.delete(*[owner_queue_key(owner_id) for owner_id in owner_ids])
        for link in conn.execute("SELECT id, email, owner_id, last_received_at, latest_message_id FROM pickup_link"):
            queue_upsert_link(link)
    except Exception:
        app.logger.exception("Failed to rebuild Redis mailbox queue")


def db():
    if "db" not in g:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect_db()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pickup_link (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          email TEXT NOT NULL UNIQUE,
          token TEXT NOT NULL UNIQUE,
          enabled INTEGER NOT NULL DEFAULT 1,
          message_limit INTEGER NOT NULL DEFAULT 100,
          created_at TEXT NOT NULL,
          owner_id INTEGER,
          last_received_at TEXT,
          latest_message_id INTEGER,
          message_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_pickup_link_token ON pickup_link(token);
        CREATE INDEX IF NOT EXISTS idx_pickup_link_email ON pickup_link(email);

        CREATE TABLE IF NOT EXISTS message (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          recipient TEXT NOT NULL,
          sender TEXT NOT NULL DEFAULT '',
          subject TEXT NOT NULL DEFAULT '',
          received_at TEXT NOT NULL,
          mailbox TEXT NOT NULL DEFAULT 'INBOX',
          preview TEXT NOT NULL DEFAULT '',
          text_body TEXT NOT NULL DEFAULT '',
          html_body TEXT NOT NULL DEFAULT '',
          raw BLOB NOT NULL,
          size INTEGER NOT NULL DEFAULT 0,
          unread INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_message_recipient ON message(recipient);
        CREATE INDEX IF NOT EXISTS idx_message_recipient_time ON message(recipient, received_at, id);

        CREATE TABLE IF NOT EXISTS app_user (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          quota INTEGER NOT NULL DEFAULT 0,
          created_count INTEGER NOT NULL DEFAULT 0,
          is_super INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(pickup_link)")]
    if "owner_id" not in cols:
        conn.execute("ALTER TABLE pickup_link ADD COLUMN owner_id INTEGER")
    if "last_received_at" not in cols:
        conn.execute("ALTER TABLE pickup_link ADD COLUMN last_received_at TEXT")
    if "latest_message_id" not in cols:
        conn.execute("ALTER TABLE pickup_link ADD COLUMN latest_message_id INTEGER")
    if "message_count" not in cols:
        conn.execute("ALTER TABLE pickup_link ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_pickup_link_queue ON pickup_link(owner_id, last_received_at, latest_message_id, id);
        UPDATE pickup_link
        SET
          message_count = (
            SELECT COUNT(*) FROM message WHERE message.recipient = pickup_link.email
          ),
          last_received_at = (
            SELECT received_at FROM message
            WHERE message.recipient = pickup_link.email
            ORDER BY received_at DESC, id DESC
            LIMIT 1
          ),
          latest_message_id = (
            SELECT id FROM message
            WHERE message.recipient = pickup_link.email
            ORDER BY received_at DESC, id DESC
            LIMIT 1
          );
        """
    )
    if not conn.execute("SELECT 1 FROM app_user WHERE username = ?", (ADMIN_USER,)).fetchone():
        conn.execute(
            "INSERT INTO app_user (username, password_hash, quota, is_super, created_at) VALUES (?, ?, -1, 1, ?)",
            (ADMIN_USER, generate_password_hash(ADMIN_PASSWORD), utc_now()),
        )
    prune_old_messages_with_conn(conn)
    rebuild_redis_queue(conn)
    conn.commit()
    conn.close()


def init_db_once():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if _db_initialized:
            return
        init_db()
        _db_initialized = True


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return func(*args, **kwargs)
    return wrapper


def super_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        if not session.get("is_super"):
            flash("需要管理员权限", "error")
            return redirect(url_for("admin_pickup_list"))
        return func(*args, **kwargs)
    return wrapper


def current_user_id():
    return session.get("user_id")


def is_super_user():
    return bool(session.get("is_super"))


def generate_temp_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def decode_header(value):
    if not value:
        return ""
    return str(email.header.make_header(email.header.decode_header(value)))


class HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {"address", "article", "aside", "blockquote", "br", "div", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "li", "p", "section", "table", "tr"}
    SKIP_TAGS = {"script", "style", "head", "title", "meta", "link"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, _attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def get_text(self):
        text = "".join(self.parts)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(value):
    if not value:
        return ""
    parser = HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return " ".join(re.sub(r"<[^>]+>", " ", value).split())
    return parser.get_text()[:BODY_LIMIT]


def message_part(message, content_type):
    candidates = []
    if message.is_multipart():
        candidates = [
            part for part in message.walk()
            if part.get_content_type() == content_type
            and part.get_content_disposition() != "attachment"
        ]
    elif message.get_content_type() == content_type:
        candidates = [message]
    if not candidates:
        return ""
    try:
        body = candidates[0].get_content()
    except Exception:
        payload = candidates[0].get_payload(decode=True) or b""
        charset = candidates[0].get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace")
    return body[:BODY_LIMIT]


def parse_message(raw_bytes):
    parsed = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw_bytes)
    text_body = message_part(parsed, "text/plain")
    html_body = message_part(parsed, "text/html")
    plain_body = text_body or html_to_text(html_body)
    preview = " ".join(plain_body.split())[:180]
    return {
        "sender": decode_header(parsed.get("from")) or "",
        "subject": decode_header(parsed.get("subject")) or "(no subject)",
        "text_body": plain_body,
        "html_body": html_body,
        "preview": preview,
    }


def normalize_email(value):
    return (value or "").strip().lower()


LOCALPART_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$", re.IGNORECASE)


def parse_requested_mailboxes(text, default_domain):
    addresses = []
    rejected = []
    seen = set()
    for raw_line in (text or "").splitlines():
        value = raw_line.strip()
        if not value:
            continue
        address = normalize_email(value if "@" in value else f"{value}@{default_domain}")
        if address.count("@") != 1:
            rejected.append(f"{value}（格式错误）")
            continue
        localpart, domain = address.rsplit("@", 1)
        if domain not in DOMAIN_NAMES:
            rejected.append(f"{value}（不支持的域名）")
            continue
        if not LOCALPART_RE.fullmatch(localpart) or localpart.startswith(".") or localpart.endswith(".") or ".." in localpart:
            rejected.append(f"{value}（邮箱名称无效）")
            continue
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return addresses, rejected


def new_token(length=48):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


CHARSETS = {
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "digits": string.digits,
    "alnum": string.ascii_lowercase + string.digits,
}


def new_localpart(prefix, domain, length=10, charset="alnum"):
    alphabet = CHARSETS.get(charset, CHARSETS["alnum"])
    clean = "".join(ch for ch in (prefix or "").lower() if ch in (string.ascii_lowercase + string.digits))
    for _ in range(300):
        localpart = clean + "".join(secrets.choice(alphabet) for _ in range(length))
        if not db().execute("SELECT 1 FROM pickup_link WHERE email = ?", (f"{localpart}@{domain}",)).fetchone():
            return localpart
    raise RuntimeError("Could not generate an unused mailbox name")


def pickup_url(token):
    return f"{BASE_URL}{url_for('pickup_view', token=token)}"


def enforce_message_limit(recipient):
    link = db().execute("SELECT message_limit FROM pickup_link WHERE email = ?", (recipient,)).fetchone()
    limit = int(link["message_limit"]) if link else DEFAULT_MESSAGE_LIMIT
    overflow = db().execute("SELECT COUNT(*) AS count FROM message WHERE recipient = ?", (recipient,)).fetchone()["count"] - limit
    if overflow <= 0:
        return 0
    rows = db().execute(
        "SELECT id FROM message WHERE recipient = ? ORDER BY received_at ASC, id ASC LIMIT ?",
        (recipient, overflow),
    ).fetchall()
    ids = [row["id"] for row in rows]
    if ids:
        db().executemany("DELETE FROM message WHERE id = ?", [(item,) for item in ids])
        refresh_link_state(recipient)
    return len(ids)


def refresh_link_state_for_conn(conn, recipient):
    latest = conn.execute(
        "SELECT id, received_at FROM message WHERE recipient = ? ORDER BY received_at DESC, id DESC LIMIT 1",
        (recipient,),
    ).fetchone()
    count = conn.execute("SELECT COUNT(*) AS count FROM message WHERE recipient = ?", (recipient,)).fetchone()["count"]
    conn.execute(
        """
        UPDATE pickup_link
        SET message_count = ?, last_received_at = ?, latest_message_id = ?
        WHERE email = ?
        """,
        (
            count,
            latest["received_at"] if latest else None,
            latest["id"] if latest else None,
            recipient,
        ),
    )
    link = conn.execute(
        "SELECT id, email, owner_id, last_received_at, latest_message_id FROM pickup_link WHERE email = ?",
        (recipient,),
    ).fetchone()
    if link:
        queue_upsert_link(link)


def refresh_link_state(recipient):
    refresh_link_state_for_conn(db(), recipient)


def prune_old_messages_with_conn(conn):
    cutoff = retention_cutoff()
    recipients = [
        row["recipient"]
        for row in conn.execute("SELECT DISTINCT recipient FROM message WHERE received_at < ?", (cutoff,))
    ]
    cursor = conn.execute("DELETE FROM message WHERE received_at < ?", (cutoff,))
    for recipient in recipients:
        refresh_link_state_for_conn(conn, recipient)
    return cursor.rowcount


def prune_old_messages():
    return prune_old_messages_with_conn(db())


def verify_ingest_signature(raw_body):
    if not INGEST_SECRET:
        abort(500)
    timestamp = request.headers.get("X-Pickup-Timestamp", "")
    signature = request.headers.get("X-Pickup-Signature", "")
    try:
        ts = int(timestamp)
    except ValueError:
        abort(401)
    if abs(int(time.time()) - ts) > 300:
        abort(401)
    expected = hmac.new(
        INGEST_SECRET.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        abort(401)


def cleanup_loop():
    while not _cleanup_stop.is_set():
        conn = None
        try:
            conn = connect_db()
            deleted = prune_old_messages_with_conn(conn)
            if deleted:
                conn.commit()
        except Exception:
            app.logger.exception("Failed to prune old messages")
        finally:
            if conn is not None:
                conn.close()
        _cleanup_stop.wait(CLEANUP_INTERVAL_SECONDS)


def start_cleanup_worker():
    global _cleanup_started
    if _cleanup_started:
        return
    with _cleanup_lock:
        if _cleanup_started:
            return
        thread = threading.Thread(target=cleanup_loop, name="message-retention-cleanup", daemon=True)
        thread.start()
        _cleanup_started = True


@app.before_request
def ensure_db():
    init_db_once()
    start_cleanup_worker()


@app.get("/")
def index():
    return redirect(url_for("admin_pickup_list"))


@app.get("/admin")
@app.get("/admin/")
def admin_index():
    return redirect(url_for("admin_pickup_list"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = db().execute("SELECT * FROM app_user WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["admin"] = True
            session["is_super"] = bool(user["is_super"])
            session["username"] = user["username"]
            session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("admin_pickup_list"))
        flash("用户名或密码错误", "error")
    return render_template("login.html")


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/admin/users")
@super_required
def admin_users_list():
    users = db().execute("SELECT * FROM app_user ORDER BY is_super DESC, id ASC").fetchall()
    return render_template("admin_users.html", users=users)


@app.post("/admin/users/create")
@super_required
def admin_users_create():
    username = (request.form.get("username") or "").strip()
    try:
        quota = int(request.form.get("quota") or "0")
    except ValueError:
        quota = 0
    if not username:
        flash("用户名不能为空", "error")
        return redirect(url_for("admin_users_list"))
    if db().execute("SELECT 1 FROM app_user WHERE username = ?", (username,)).fetchone():
        flash("用户名已存在", "error")
        return redirect(url_for("admin_users_list"))
    plain = generate_temp_password()
    db().execute(
        "INSERT INTO app_user (username, password_hash, quota, is_super, created_at) VALUES (?, ?, ?, 0, ?)",
        (username, generate_password_hash(plain), quota, utc_now()),
    )
    db().commit()
    users = db().execute("SELECT * FROM app_user ORDER BY is_super DESC, id ASC").fetchall()
    return render_template("admin_users.html", users=users, new_password=plain, new_username=username)


@app.post("/admin/users/<int:user_id>/quota")
@super_required
def admin_users_quota(user_id):
    try:
        quota = int(request.form.get("quota") or "0")
    except ValueError:
        quota = 0
    db().execute("UPDATE app_user SET quota = ? WHERE id = ?", (quota, user_id))
    db().commit()
    flash("配额已更新")
    return redirect(url_for("admin_users_list"))


@app.post("/admin/users/<int:user_id>/reset")
@super_required
def admin_users_reset(user_id):
    user = db().execute("SELECT * FROM app_user WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("用户不存在", "error")
        return redirect(url_for("admin_users_list"))
    plain = generate_temp_password()
    db().execute("UPDATE app_user SET password_hash = ? WHERE id = ?", (generate_password_hash(plain), user_id))
    db().commit()
    users = db().execute("SELECT * FROM app_user ORDER BY is_super DESC, id ASC").fetchall()
    return render_template("admin_users.html", users=users, new_password=plain, new_username=user["username"])


@app.post("/admin/users/<int:user_id>/delete")
@super_required
def admin_users_delete(user_id):
    user = db().execute("SELECT * FROM app_user WHERE id = ?", (user_id,)).fetchone()
    if not user:
        flash("用户不存在", "error")
        return redirect(url_for("admin_users_list"))
    if user["is_super"]:
        flash("不能删除超级管理员", "error")
        return redirect(url_for("admin_users_list"))
    if user["id"] == session.get("user_id"):
        flash("不能删除当前登录的用户", "error")
        return redirect(url_for("admin_users_list"))
    db().execute("DELETE FROM app_user WHERE id = ?", (user_id,))
    db().commit()
    flash("已删除用户")
    return redirect(url_for("admin_users_list"))


@app.route("/admin/account/password", methods=["GET", "POST"])
@login_required
def admin_account_password():
    if request.method == "POST":
        old = request.form.get("old_password") or ""
        new = request.form.get("new_password") or ""
        user = db().execute("SELECT * FROM app_user WHERE id = ?", (session["user_id"],)).fetchone()
        if not user or not check_password_hash(user["password_hash"], old):
            flash("原密码错误", "error")
        elif len(new) < 6:
            flash("新密码至少需要 6 位", "error")
        else:
            db().execute("UPDATE app_user SET password_hash = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
            db().commit()
            flash("密码已修改")
            return redirect(url_for("admin_pickup_list"))
    return render_template("admin_account.html")


@app.get("/admin/pickup")
@login_required
def admin_pickup_list():
    selected_domain = request.args.get("domain") or DOMAIN_NAMES[0]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    where = "p.email LIKE ?"
    wargs = [f"%@{selected_domain}"]
    if not is_super_user():
        where += " AND p.owner_id = ?"
        wargs.append(current_user_id())
    total = db().execute("SELECT COUNT(*) AS c FROM pickup_link p WHERE " + where, wargs).fetchone()["c"]
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, pages))
    rows = []
    for link in db().execute(
        """
        SELECT p.*
        FROM pickup_link p
        WHERE """ + where + """
        ORDER BY p.last_received_at IS NULL ASC, p.last_received_at DESC, p.latest_message_id DESC, p.id DESC
        LIMIT ? OFFSET ?
        """,
        wargs + [PER_PAGE, (page - 1) * PER_PAGE],
    ):
        rows.append({"link": link, "url": pickup_url(link["token"]), "message_count": link["message_count"]})
    return render_template("admin_list.html", rows=rows, domains=DOMAIN_NAMES, domain=selected_domain, page=page, pages=pages, total=total)


def _owned_links():
    query = """
        SELECT p.*
        FROM pickup_link p
    """
    args = []
    if not is_super_user():
        query += " WHERE p.owner_id = ?"
        args.append(current_user_id())
    query += """
        ORDER BY p.last_received_at IS NULL ASC, p.last_received_at DESC, p.latest_message_id DESC, p.id DESC
    """
    return db().execute(query, args).fetchall()


def _extract_email(sender):
    if not sender:
        return ""
    _, addr = email.utils.parseaddr(sender)
    return addr or sender


def _latest_entry(link, pattern):
    latest = None
    if link["latest_message_id"]:
        latest = db().execute("SELECT * FROM message WHERE id = ?", (link["latest_message_id"],)).fetchone()
    if not latest:
        return {"email": link["email"], "token": link["token"], "latest": None}
    body_text = latest["text_body"] or html_to_text(latest["html_body"])
    code_source = "\n".join([latest["subject"] or "", body_text or ""])
    return {
        "email": link["email"],
        "token": link["token"],
        "latest": {
            "id": latest["id"],
            "subject": latest["subject"],
            "sender": latest["sender"],
            "sender_email": _extract_email(latest["sender"]),
            "recipient": latest["recipient"],
            "time_text": format_time(latest["received_at"]),
            "code": extract_code(code_source, pattern),
        },
    }


def _batch_items(pattern, q=None):
    links = _owned_links()
    items = [_latest_entry(link, pattern) for link in links]
    if q:
        q = q.strip().lower()
        kept = []
        for item in items:
            email = item["email"].lower()
            subject = (item["latest"]["subject"] if item["latest"] else "").lower()
            if q in email or q in subject:
                kept.append(item)
        items = kept
    return items


def _owner_where(alias="p"):
    if is_super_user():
        return "", []
    return f" WHERE {alias}.owner_id = ?", [current_user_id()]


def _batch_version(q=None):
    if q:
        return ""
    where, args = _owner_where("p")
    row = db().execute(
        f"""
        SELECT COUNT(*) AS total, MAX(p.last_received_at) AS last_received_at, MAX(p.latest_message_id) AS latest_message_id
        FROM pickup_link p
        {where}
        """,
        args,
    ).fetchone()
    return f"{row['total']}:{row['last_received_at'] or ''}:{row['latest_message_id'] or 0}"


def _queued_links_from_redis(page, per_page):
    client = redis_conn()
    if not client:
        return None
    key = BATCH_QUEUE_ALL_KEY if is_super_user() else owner_queue_key(current_user_id())
    try:
        total = client.zcard(key)
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        end = start + per_page - 1
        emails = client.zrevrange(key, start, end)
    except Exception:
        app.logger.exception("Failed to read Redis mailbox queue")
        return None
    if not emails and total:
        return None
    if not emails:
        return [], total, page
    placeholders = ",".join("?" for _ in emails)
    owner_filter = "" if is_super_user() else " AND owner_id = ?"
    args = emails + ([] if is_super_user() else [current_user_id()])
    rows = db().execute(
        f"SELECT * FROM pickup_link WHERE email IN ({placeholders}){owner_filter}",
        args,
    ).fetchall()
    by_email = {row["email"]: row for row in rows}
    ordered = [by_email[email] for email in emails if email in by_email]
    return (ordered, total, page) if len(ordered) == len(emails) else None


def _batch_page(pattern, q=None, page=1, per_page=PER_PAGE):
    q = (q or "").strip()
    version = _batch_version(q)
    if not q:
        queued = _queued_links_from_redis(page, per_page)
        if queued is not None:
            links, total, page = queued
        else:
            where, args = _owner_where("p")
            total = db().execute(f"SELECT COUNT(*) AS c FROM pickup_link p{where}", args).fetchone()["c"]
            pages = max(1, (total + per_page - 1) // per_page)
            page = max(1, min(page, pages))
            links = db().execute(
                f"""
                SELECT p.*
                FROM pickup_link p
                {where}
                ORDER BY p.last_received_at IS NULL ASC, p.last_received_at DESC, p.latest_message_id DESC, p.id DESC
                LIMIT ? OFFSET ?
                """,
                args + [per_page, (page - 1) * per_page],
            ).fetchall()
    else:
        where, args = _owner_where("p")
        qlike = f"%{q}%"
        if where:
            where += " AND (p.email LIKE ? OR m.subject LIKE ?)"
        else:
            where = " WHERE (p.email LIKE ? OR m.subject LIKE ?)"
        args += [qlike, qlike]
        total = db().execute(
            f"""
            SELECT COUNT(*) AS c
            FROM pickup_link p
            LEFT JOIN message m ON m.id = p.latest_message_id
            {where}
            """,
            args,
        ).fetchone()["c"]
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        links = db().execute(
            f"""
            SELECT p.*
            FROM pickup_link p
            LEFT JOIN message m ON m.id = p.latest_message_id
            {where}
            ORDER BY p.last_received_at IS NULL ASC, p.last_received_at DESC, p.latest_message_id DESC, p.id DESC
            LIMIT ? OFFSET ?
            """,
            args + [per_page, (page - 1) * per_page],
        ).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    items = [_latest_entry(link, pattern) for link in links]
    return {"items": items, "page": page, "pages": pages, "total": total, "version": version}


@app.get("/admin/batch")
@login_required
def admin_batch():
    pattern = batch_code_regex()
    q = request.args.get("q") or ""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    data = _batch_page(pattern, q, page)
    total = data["total"]
    pages = data["pages"]
    page = data["page"]
    start = (page - 1) * PER_PAGE
    start_item = start + 1 if total > 0 else 0
    end_item = min(page * PER_PAGE, total) if total > 0 else 0
    return render_template(
        "admin_batch.html",
        items=data["items"],
        pattern=pattern,
        q=q,
        page=page,
        pages=pages,
        total=total,
        start_item=start_item,
        end_item=end_item,
        version=data["version"],
    )


@app.post("/admin/batch/regex")
@login_required
def admin_batch_regex():
    session["batch_regex"] = request.form.get("regex") or ""
    return redirect(url_for("admin_batch"))


@app.get("/admin/batch/data")
@login_required
def admin_batch_data():
    pattern = batch_code_regex()
    q = request.args.get("q") or ""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    data = _batch_page(pattern, q, page)
    if not q and request.args.get("version") == data["version"]:
        return {
            "unchanged": True,
            "page": data["page"],
            "pages": data["pages"],
            "total": data["total"],
            "version": data["version"],
        }
    return data


@app.route("/admin/pickup/create", methods=["GET", "POST"])
@login_required
def admin_pickup_create():
    generated = []
    skipped = []
    selected_domain = request.form.get("domain") or request.args.get("domain") or DOMAIN_NAMES[0]
    if selected_domain not in DOMAIN_NAMES:
        selected_domain = DOMAIN_NAMES[0]
    user_row = db().execute("SELECT * FROM app_user WHERE id = ?", (current_user_id(),)).fetchone()
    quota = int(user_row["quota"]) if user_row else -1
    if request.method == "POST":
        mode = request.form.get("mode") or "random"
        requested_addresses = []
        if mode == "specified":
            source_text = request.form.get("mailboxes") or ""
            upload = request.files.get("mailbox_file")
            if upload and upload.filename:
                if not upload.filename.lower().endswith(".txt"):
                    skipped.append(f"{upload.filename}（仅支持 TXT 文件）")
                else:
                    raw = upload.stream.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        skipped.append(f"{upload.filename}（文件不能超过 1 MB）")
                    else:
                        try:
                            source_text += "\n" + raw.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            skipped.append(f"{upload.filename}（请使用 UTF-8 编码）")
            requested_addresses, rejected = parse_requested_mailboxes(source_text, selected_domain)
            skipped.extend(rejected)
            if len(requested_addresses) > 1000:
                skipped.extend(f"{item}（超过单次 1000 个限制）" for item in requested_addresses[1000:])
                requested_addresses = requested_addresses[:1000]
        else:
            prefix = request.form.get("prefix") or ""
            try:
                length = max(1, min(int(request.form.get("length") or "10"), 64))
            except ValueError:
                length = 10
            charset = request.form.get("charset") or "alnum"
            try:
                count = max(1, min(int(request.form.get("count") or "1"), 1000))
            except ValueError:
                count = 1
            requested_addresses = [
                f"{new_localpart(prefix, selected_domain, length, charset)}@{selected_domain}"
                for _ in range(count)
            ]

        existing = {
            row["email"]
            for row in db().execute(
                f"SELECT email FROM pickup_link WHERE email IN ({','.join('?' for _ in requested_addresses)})",
                requested_addresses,
            ).fetchall()
        } if requested_addresses else set()
        for address in requested_addresses:
            if address in existing:
                skipped.append(f"{address}（已存在）")
        requested_addresses = [address for address in requested_addresses if address not in existing]

        if not is_super_user() and quota >= 0:
            remaining = quota - int(user_row["created_count"])
            if remaining <= 0:
                flash("已达到邮箱创建配额上限", "error")
                return redirect(url_for("admin_pickup_create", domain=selected_domain))
            if len(requested_addresses) > remaining:
                skipped.extend(f"{item}（超出剩余配额）" for item in requested_addresses[remaining:])
                requested_addresses = requested_addresses[:remaining]
                flash(f"已按剩余配额生成 {remaining} 个", "error")
        message_limit = DEFAULT_MESSAGE_LIMIT
        for email_address in requested_addresses:
            token = new_token()
            cursor = db().execute(
                "INSERT INTO pickup_link (email, token, enabled, message_limit, created_at, owner_id) VALUES (?, ?, 1, ?, ?, ?)",
                (email_address, token, message_limit, utc_now(), current_user_id()),
            )
            link = db().execute(
                "SELECT id, email, owner_id, last_received_at, latest_message_id FROM pickup_link WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
            if link:
                queue_upsert_link(link)
            generated.append({"email": email_address, "url": pickup_url(token)})
        if generated:
            db().execute("UPDATE app_user SET created_count = created_count + ? WHERE id = ?", (len(generated), current_user_id()))
            db().commit()
            flash(f"已生成 {len(generated)} 个取件邮箱")
        elif mode == "specified" and not skipped:
            flash("请输入至少一个邮箱，或上传 TXT 文件", "error")
    remaining = None
    if not is_super_user() and quota >= 0:
        current_count = db().execute("SELECT created_count FROM app_user WHERE id = ?", (current_user_id(),)).fetchone()
        remaining = max(0, quota - int(current_count["created_count"]))
    return render_template(
        "admin_create.html",
        domains=DOMAIN_NAMES,
        domain=selected_domain,
        generated=generated,
        skipped=skipped,
        quota=quota,
        remaining=remaining,
    )


@app.post("/admin/pickup/batch")
@login_required
def admin_pickup_batch():
    action = request.form.get("batch_action")
    ids = [int(item) for item in request.form.getlist("link_id") if item.isdigit()]
    if not ids:
        flash("未选择取件邮箱", "error")
        return redirect(url_for("admin_pickup_list"))
    placeholders = ",".join("?" for _ in ids)
    owner_filter = "" if is_super_user() else " AND owner_id = ?"
    owner_args = [] if is_super_user() else [current_user_id()]
    if action in {"enable", "disable"}:
        enabled = 1 if action == "enable" else 0
        db().executemany(
            f"UPDATE pickup_link SET enabled = ? WHERE id = ?{owner_filter}",
            [(enabled, item) + tuple(owner_args) for item in ids],
        )
        flash("批量操作已完成")
    elif action == "delete":
        rows = db().execute(
            f"SELECT email, owner_id FROM pickup_link WHERE id IN ({placeholders}){owner_filter}",
            ids + owner_args,
        ).fetchall()
        emails = [row["email"] for row in rows]
        db().executemany("DELETE FROM message WHERE recipient = ?", [(item,) for item in emails])
        for row in rows:
            queue_remove_link(row["email"], row["owner_id"])
        db().executemany(
            f"DELETE FROM pickup_link WHERE id = ?{owner_filter}",
            [(item,) + tuple(owner_args) for item in ids],
        )
        flash("已删除选中的取件邮箱")
    else:
        flash("未知的批量操作", "error")
    db().commit()
    batch_domain = request.form.get("domain") or DOMAIN_NAMES[0]
    batch_page = request.form.get("page")
    target = url_for("admin_pickup_list", domain=batch_domain)
    if batch_page and batch_page.isdigit():
        target = url_for("admin_pickup_list", domain=batch_domain, page=int(batch_page))
    return redirect(target)


@app.post("/api/cloudflare-email")
def cloudflare_email():
    raw_body = request.get_data()
    verify_ingest_signature(raw_body)
    payload = request.get_json(force=True)
    recipient = normalize_email(payload.get("to"))
    raw_base64 = payload.get("raw_base64") or ""
    link = db().execute("SELECT * FROM pickup_link WHERE email = ? AND enabled = 1", (recipient,)).fetchone()
    if not link:
        return {"ok": False, "reason": "unknown_recipient"}, 404
    raw_bytes = base64.b64decode(raw_base64)
    parsed = parse_message(raw_bytes)
    mailbox = "Junk" if payload.get("mailbox") == "Junk" else "INBOX"
    received_at = utc_now()
    cursor = db().execute(
        """
        INSERT INTO message
        (recipient, sender, subject, received_at, mailbox, preview, text_body, html_body, raw, size, unread)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            recipient,
            parsed["sender"],
            parsed["subject"],
            received_at,
            mailbox,
            parsed["preview"],
            parsed["text_body"],
            parsed["html_body"],
            raw_bytes,
            len(raw_bytes),
        ),
    )
    message_id = cursor.lastrowid
    db().execute(
        """
        UPDATE pickup_link
        SET last_received_at = ?, latest_message_id = ?, message_count = message_count + 1
        WHERE email = ?
        """,
        (received_at, message_id, recipient),
    )
    link = db().execute(
        "SELECT id, email, owner_id, last_received_at, latest_message_id FROM pickup_link WHERE email = ?",
        (recipient,),
    ).fetchone()
    if link:
        queue_upsert_link(link)
    deleted = enforce_message_limit(recipient)
    db().commit()
    return {"ok": True, "deleted_old_messages": deleted}


def message_to_dict(row):
    data = dict(row)
    data.pop("raw", None)
    data["time_text"] = format_time(data.get("received_at"))
    return data


def get_messages_page(email, page, per_page=PER_PAGE):
    total = db().execute("SELECT COUNT(*) AS c FROM message WHERE recipient = ?", (email,)).fetchone()["c"]
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    rows = db().execute(
        "SELECT * FROM message WHERE recipient = ? ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
        (email, per_page, (page - 1) * per_page),
    ).fetchall()
    unread = db().execute("SELECT COUNT(*) AS c FROM message WHERE recipient = ? AND unread = 1", (email,)).fetchone()["c"]
    return {"messages": [message_to_dict(r) for r in rows], "total": total, "pages": pages, "page": page, "unread": unread}


@app.get("/admin/batch/message/<int:message_id>")
@login_required
def admin_batch_message(message_id):
    msg = db().execute("SELECT * FROM message WHERE id = ?", (message_id,)).fetchone()
    if not msg:
        abort(404)
    if not is_super_user():
        allowed = db().execute(
            "SELECT 1 FROM pickup_link WHERE email = ? AND owner_id = ?",
            (msg["recipient"], current_user_id()),
        ).fetchone()
        if not allowed:
            abort(403)
    return {
        "id": msg["id"],
        "subject": msg["subject"],
        "sender": msg["sender"],
        "sender_email": _extract_email(msg["sender"]),
        "recipient": msg["recipient"],
        "received_at": msg["received_at"],
        "time_text": format_time(msg["received_at"]),
        "text_body": msg["text_body"],
        "html_body": msg["html_body"],
    }


@app.get("/pickup/<token>")
def pickup_view(token):
    link = db().execute("SELECT * FROM pickup_link WHERE token = ? AND enabled = 1", (token,)).fetchone() or abort(404)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    data = get_messages_page(link["email"], page)
    initial = {
        "page": data["page"],
        "pages": data["pages"],
        "total": data["total"],
        "unread": data["unread"],
        "token": token,
    }
    return render_template(
        "pickup.html",
        link=link,
        messages=data["messages"],
        page=data["page"],
        pages=data["pages"],
        total=data["total"],
        unread=data["unread"],
        initial_json=initial,
    )


@app.get("/pickup/<token>/messages")
def pickup_messages(token):
    link = db().execute("SELECT * FROM pickup_link WHERE token = ? AND enabled = 1", (token,)).fetchone() or abort(404)
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    data = get_messages_page(link["email"], page)
    return {
        "messages": data["messages"],
        "page": data["page"],
        "pages": data["pages"],
        "total": data["total"],
        "unread": data["unread"],
    }


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8000)
