"""
语言与学习技能评估系统 - 后端
Flask + SQLite
提供：多用户账号体系（注册/登录/鉴权）、被评估者档案管理、评估记录、逐项评分、自动计分 API
"""

import io
import json
import math
import os
import sqlite3
from datetime import date, datetime
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, send_file, g, Response
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "ablls_data.json")
AGE_FILE = os.path.join(BASE_DIR, "age_map.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Vercel Serverless 的文件系统只读，未配置 DATABASE_URL 时把 SQLite 放到 /tmp
if os.environ.get("VERCEL") and not os.environ.get("DATABASE_URL"):
    DB_FILE = "/tmp/ablls.db"
else:
    DB_FILE = os.path.join(BASE_DIR, "ablls.db")

app = Flask(__name__, static_folder=STATIC_DIR)

# 让 jsonify 能序列化 Postgres 返回的 datetime（created_at / updated_at 等字段）
from flask.json.provider import DefaultJSONProvider
class _JSONProvider(DefaultJSONProvider):
    def default(self, o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        return super().default(o)
app.json = _JSONProvider(app)

# ---------- 密钥与 Token ----------
# 生产环境请通过环境变量 SECRET_KEY 覆盖，避免 token 被伪造。
SECRET_KEY = os.environ.get("SECRET_KEY", "local-dev-secret-change-in-prod-9f3a2c7e")
_auth_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="eval-auth")
TOKEN_MAX_AGE = 60 * 60 * 24 * 7  # token 有效期 7 天

# 初始管理员账号（首次启动自动创建；请尽快在后台修改密码）
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PWD = "admin@2026"

# ---------- CORS（允许前端从预览面板 / 局域网等不同源访问 API） ----------
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp


# ---------- 鉴权工具 ----------
def generate_token(uid):
    return _auth_serializer.dumps(str(uid))


def get_current_user():
    """从 Header / URL ?token= / Cookie 中取 token，校验后返回用户 dict；无效或禁用返回 None。"""
    auth = request.headers.get("Authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.args.get("token") or request.cookies.get("token")
    if not token:
        return None
    try:
        uid = int(_auth_serializer.loads(token, max_age=TOKEN_MAX_AGE))
    except (BadSignature, SignatureExpired, ValueError):
        return None
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        return None
    if row["status"] != "active":
        return None
    return dict(row)


def login_required(f):
    @wraps(f)
    def deco(*a, **k):
        u = get_current_user()
        if not u:
            return jsonify({"error": "未登录或登录已过期，请重新登录"}), 401
        g.current_user = u
        return f(*a, **k)
    return deco


def admin_required(f):
    @wraps(f)
    def deco(*a, **k):
        u = get_current_user()
        if not u:
            return jsonify({"error": "未登录或登录已过期，请重新登录"}), 401
        if u["role"] != "admin":
            return jsonify({"error": "需要管理员权限"}), 403
        g.current_user = u
        return f(*a, **k)
    return deco


def get_owned_client(cid):
    """返回当前用户拥有（或管理员可见）的被评估者；无权限返回 None。"""
    u = g.current_user
    db = get_db()
    c = db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not c:
        return None
    if u["role"] == "admin":
        return c
    if c["user_id"] == u["id"]:
        return c
    return None


def get_owned_assessment(aid):
    """返回当前用户拥有（或管理员可见）的评估；无权限返回 None。"""
    u = g.current_user
    db = get_db()
    a = db.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
    if not a:
        return None
    c = db.execute("SELECT * FROM clients WHERE id=?", (a["client_id"],)).fetchone()
    if not c:
        return None
    if u["role"] != "admin" and c["user_id"] != u["id"]:
        return None
    return a


def log_action(action, target_type=None, target_id=None, detail=None):
    """记录操作日志（失败不影响主流程）。"""
    u = getattr(g, "current_user", None)
    uid = u["id"] if u else None
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    try:
        db = get_db()
        db.execute(
            "INSERT INTO operation_logs (user_id, action, target_type, target_id, detail, ip, created_at) "
            "VALUES (?,?,?,?,?,?, datetime('now','localtime'))",
            (uid, action, target_type, target_id, detail, ip),
        )
        db.commit()
    except Exception:
        pass


# ---------- 量表数据 ----------
with open(DATA_FILE, encoding="utf-8") as f:
    SCALE = json.load(f)

# 每个技能项的估算适用月龄（里程碑年龄，参考值，可编辑 age_map.json）
try:
    with open(AGE_FILE, encoding="utf-8") as f:
        AGE_MAP = json.load(f)
except FileNotFoundError:
    AGE_MAP = {}

# 显示名归一化：PDF 中换行被替换为空格，去掉这些伪空格
def norm(s):
    return "".join(s.split())

# 月龄 -> 岁标签（用于展示）
def age_label(months):
    if months is None:
        return "—"
    y = months // 12
    m = months % 12
    if y == 0:
        return f"{m}个月"
    if m == 0:
        return f"{y}岁"
    return f"{y}岁{m}个月"


def months_between(birth_str, ref_str):
    """计算 birth_str 到 ref_str 之间的整月月龄（ref < birth 返回 None）"""
    try:
        b = datetime.strptime(birth_str, "%Y-%m-%d").date()
    except Exception:
        return None
    try:
        r = datetime.strptime(ref_str, "%Y-%m-%d").date()
    except Exception:
        r = date.today()
    if r < b:
        return None
    months = (r.year - b.year) * 12 + (r.month - b.month)
    if r.day < b.day:
        months -= 1
    return months

SCALE_ITEMS = {}  # code -> item
for domain, items in SCALE["domains"].items():
    for it in items:
        it["domain"] = norm(domain)
        it["age"] = AGE_MAP.get(it["code"])  # 估算适用月龄，可能为 None
        SCALE_ITEMS[it["code"]] = it

MAX_TOTAL = SCALE["meta"]["max_total"]

# 主评估 / 拓展评估 判定：与前端 computeAdaptCutoff 保持一致。
# 主评估 = 项月龄 <= 适配月龄(adaptMonths)；超出的为拓展评估，不计入得分率。
ALL_AGES = sorted({it["age"] for it in SCALE_ITEMS.values() if it.get("age") is not None})


def compute_adapt_cutoff(cm, ages=None):
    """以 3 个月为一个跨度，把实际月龄向上取整到最近的「有项目的适配阶段」。"""
    if cm is None:
        return None
    if ages is None:
        ages = ALL_AGES
    stage = math.ceil(cm / 3) * 3  # 下一个 3 月边界，如 2岁2月 -> 2岁3月
    if not ages:
        return stage
    max_age = ages[-1]
    s = stage
    while s <= max_age + 3:
        # 该 3 月阶段区间 (s-3, s] 内是否存在项目
        if any(a <= s and a > s - 3 for a in ages):
            return s
        s += 3
    return s  # 超出最大年龄，全部视为主评估


def get_adapt_months(assessment_id):
    """返回该评估对应的主评估适配月龄；无出生日期则 None（此时全部视为主评估）。"""
    db = get_db()
    a = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    if not a:
        return None
    client = db.execute("SELECT * FROM clients WHERE id=?", (a["client_id"],)).fetchone()
    if not client:
        return None
    cm = months_between(client["birth_date"], a["date"])
    return compute_adapt_cutoff(cm)


def is_main_item(age, adapt):
    """项月龄为空、或适配月龄为空、或项月龄 <= 适配月龄 => 主评估。"""
    return (age is None) or (adapt is None) or (age <= adapt)


# ---------- 数据库（本地 SQLite / 云端 PostgreSQL 自动切换） ----------
try:
    import psycopg2
    from psycopg2.extras import DictCursor
    _HAS_PG = True
except ImportError:
    psycopg2 = None
    DictCursor = None
    _HAS_PG = False

DATABASE_URL = os.environ.get("DATABASE_URL")


class _PGCursor:
    """兼容 sqlite3 游标：支持 row[列名] / row[0] 与 lastrowid。"""
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = None
    def fetchone(self):
        return self._cur.fetchone()
    def fetchall(self):
        return self._cur.fetchall()
    def __iter__(self):
        return iter(self._cur)
    def close(self):
        return self._cur.close()


class _PGConn:
    """包装 psycopg2 连接，使业务代码无需改动即可在 Postgres 上运行。"""
    def __init__(self, conn):
        self._conn = conn
        self._conn.autocommit = True
        self._conn.cursor_factory = DictCursor
    @staticmethod
    def _pg_sql(sql):
        sql = sql.replace("?", "%s")
        sql = sql.replace("datetime('now','localtime','-7 days')", "NOW() - INTERVAL '7 days'")
        sql = sql.replace("datetime('now','localtime')", "NOW()")
        return sql
    def execute(self, sql, params=None):
        sql = self._pg_sql(sql)
        cur = self._conn.cursor()
        s = sql.strip().upper()
        do_return = s.startswith("INSERT") and " RETURNING " not in s and "ON CONFLICT" not in s
        if do_return:
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
        cur.execute(sql, params or ())
        rc = _PGCursor(cur)
        if do_return:
            try:
                row = cur.fetchone()
                rc.lastrowid = row[0] if row else None
            except Exception:
                rc.lastrowid = None
        return rc
    def executescript(self, script):
        cur = self._conn.cursor()
        for stmt in script.split(";"):
            if stmt.strip():
                cur.execute(stmt)
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()
    def close(self):
        self._conn.close()


def _seed_db(db):
    """默认目录标签、系统配置、种子管理员（SQLite / Postgres 通用）。"""
    row = db.execute("SELECT value FROM settings WHERE key='client_tags'").fetchone()
    if not row:
        default_tags = ["在读", "离校", "意向"]
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('client_tags', ?)",
            (json.dumps(default_tags, ensure_ascii=False),),
        )
    for key, val in [("site_name", "语言与学习技能评估系统"), ("allow_self_register", "1")]:
        if not db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone():
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))
    cnt = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if cnt == 0:
        db.execute(
            "INSERT INTO users (username, password_hash, role, status) VALUES (?,?,?,?)",
            (DEFAULT_ADMIN_USER, generate_password_hash(DEFAULT_ADMIN_PWD), "admin", "active"),
        )
        admin_id = db.execute("SELECT id FROM users WHERE username=?", (DEFAULT_ADMIN_USER,)).fetchone()[0]
        db.execute("UPDATE clients SET user_id=? WHERE user_id IS NULL", (admin_id,))


def get_db():
    if "db" not in g:
        if DATABASE_URL:
            if not _HAS_PG:
                raise RuntimeError("未安装 psycopg2，无法连接 Postgres，请先 pip install psycopg2-binary")
            conn = psycopg2.connect(DATABASE_URL)
            g.db = _PGConn(conn)
        else:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


_PG_DDL = [
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMP DEFAULT NOW(),
        updated_at TIMESTAMP DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS clients (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        name TEXT NOT NULL,
        gender TEXT,
        birth_date TEXT,
        note TEXT,
        status TEXT,
        is_archived INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS assessments (
        id SERIAL PRIMARY KEY,
        client_id INTEGER NOT NULL,
        title TEXT,
        assessor TEXT,
        date TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY(client_id) REFERENCES clients(id)
    )""",
    """CREATE TABLE IF NOT EXISTS scores (
        id SERIAL PRIMARY KEY,
        assessment_id INTEGER NOT NULL,
        item_code TEXT NOT NULL,
        score INTEGER NOT NULL DEFAULT 0,
        UNIQUE(assessment_id, item_code),
        FOREIGN KEY(assessment_id) REFERENCES assessments(id)
    )""",
    """CREATE TABLE IF NOT EXISTS operation_logs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        action TEXT,
        target_type TEXT,
        target_id INTEGER,
        detail TEXT,
        ip TEXT,
        created_at TIMESTAMP DEFAULT NOW(),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        id SERIAL PRIMARY KEY,
        key TEXT UNIQUE NOT NULL,
        value TEXT
    )""",
]


def init_db():
    if DATABASE_URL:
        if not _HAS_PG:
            raise RuntimeError("未安装 psycopg2-binary，无法初始化 Postgres 数据库")
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        for stmt in _PG_DDL:
            conn.cursor().execute(stmt)
        _seed_db(_PGConn(conn))
        conn.close()
        return
    # ---- 以下为本地 SQLite 路径（保持原逻辑） ----
    db = sqlite3.connect(DB_FILE)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            gender TEXT,
            birth_date TEXT,
            note TEXT,
            status TEXT,
            is_archived INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS assessments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            title TEXT,
            assessor TEXT,
            date TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(client_id) REFERENCES clients(id)
        );
        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assessment_id INTEGER NOT NULL,
            item_code TEXT NOT NULL,
            score INTEGER NOT NULL DEFAULT 0,
            UNIQUE(assessment_id, item_code),
            FOREIGN KEY(assessment_id) REFERENCES assessments(id)
        );
        CREATE TABLE IF NOT EXISTS operation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT,
            target_type TEXT,
            target_id INTEGER,
            detail TEXT,
            ip TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    # 迁移：clients 增加 user_id 列（幂等）
    cols = [r[1] for r in db.execute("PRAGMA table_info(clients)").fetchall()]
    if "user_id" not in cols:
        db.execute("ALTER TABLE clients ADD COLUMN user_id INTEGER")

    # 初始化：默认目录标签（如未设置）
    row = db.execute("SELECT value FROM settings WHERE key='client_tags'").fetchone()
    if not row:
        default_tags = ["在读", "离校", "意向"]
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('client_tags', ?)",
            (json.dumps(default_tags, ensure_ascii=False),),
        )
    # 系统配置默认值
    for key, val in [("site_name", "语言与学习技能评估系统"), ("allow_self_register", "1")]:
        if not db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone():
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, val))

    # 种子管理员：首次启动且无任何用户时创建
    cnt = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if cnt == 0:
        db.execute(
            "INSERT INTO users (username, password_hash, role, status) VALUES (?,?,?,?)",
            (DEFAULT_ADMIN_USER, generate_password_hash(DEFAULT_ADMIN_PWD), "admin", "active"),
        )
        admin_id = db.execute("SELECT id FROM users WHERE username=?", (DEFAULT_ADMIN_USER,)).fetchone()[0]
        # 将历史被评估者归属到管理员（数据迁移，避免丢失）
        db.execute("UPDATE clients SET user_id=? WHERE user_id IS NULL", (admin_id,))

    db.commit()
    db.close()


def get_client_tags():
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key='client_tags'").fetchone()
    if not row:
        return ["在读", "离校", "意向"]
    try:
        return json.loads(row["value"])
    except Exception:
        return ["在读", "离校", "意向"]


def set_client_tags(tags):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('client_tags', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(tags, ensure_ascii=False),),
    )
    db.commit()


def get_setting(key, default=None):
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


init_db()


# ---------- 工具 ----------
def compute_domain_summary(assessment_id):
    """按领域统计：满分、实际分、得分项、未评项（仅主评估计入得分率，拓展评估排除）"""
    db = get_db()
    rows = db.execute(
        "SELECT item_code, score FROM scores WHERE assessment_id=?",
        (assessment_id,),
    ).fetchall()
    scored = {r["item_code"]: r["score"] for r in rows}
    adapt = get_adapt_months(assessment_id)

    domains = []
    for domain, items in SCALE["domains"].items():
        dname = norm(domain)
        max_sum = 0
        got_sum = 0
        done = 0
        main_cnt = 0
        for it in items:
            code = it["code"]
            age = it.get("age")
            if not is_main_item(age, adapt):
                continue  # 拓展评估不计入得分率
            mx = it.get("max_score") or 0
            max_sum += mx
            main_cnt += 1
            sc = scored.get(code)
            if sc is not None:
                got_sum += sc
                done += 1
        domains.append(
            {
                "domain": dname,
                "item_count": main_cnt,
                "max_score": max_sum,
                "score": got_sum,
                "rated": done,
                "rate": round(got_sum / max_sum * 100, 1) if max_sum else 0,
            }
        )
    return domains


def compute_total(assessment_id):
    """总分概览（仅主评估计入得分率，拓展评估排除）"""
    db = get_db()
    rows = db.execute(
        "SELECT item_code, score FROM scores WHERE assessment_id=?",
        (assessment_id,),
    ).fetchall()
    scored = {r["item_code"]: r["score"] for r in rows}
    adapt = get_adapt_months(assessment_id)
    main_codes = [c for c, it in SCALE_ITEMS.items() if is_main_item(it.get("age"), adapt)]
    got = sum(scored.get(c, 0) for c in main_codes)
    rated = sum(1 for c in main_codes if c in scored)
    main_max = sum((SCALE_ITEMS[c].get("max_score") or 0) for c in main_codes)
    return {
        "score": got,
        "max_total": main_max,
        "rated": rated,
        "total_items": len(main_codes),
        "rate": round(got / main_max * 100, 1) if main_max else 0,
    }


# 基本掌握阈值：得分率 ≥ 该值视为该里程碑已达成（分级判定的核心参数）
ACHIEVE_THRESHOLD = 0.8


def compute_ability_ages(assessment_id):
    """按维度估算评估者的实际能力年龄（分级判定，参考大模型临床经验）。

    方法要点：
    - 得分率 ≥ ACHIEVE_THRESHOLD(80%) 记为"基本掌握"；0<得分率<80% 为"临界/部分掌握"。
    - 沿月龄轴取"连续基本掌握"的最远点，对紧随其后的临界项按得分率线性插值得到连续能力年龄。
    - 未评分项忽略；与实际年龄相差 ±3 个月内视为"符合年龄"，超出即"超前/滞后"。
    """
    db = get_db()
    a = db.execute("SELECT * FROM assessments WHERE id=?", (assessment_id,)).fetchone()
    client = db.execute("SELECT * FROM clients WHERE id=?", (a["client_id"],)).fetchone()
    rows = db.execute(
        "SELECT item_code, score FROM scores WHERE assessment_id=?",
        (assessment_id,),
    ).fetchall()
    scored = {r["item_code"]: r["score"] for r in rows}
    chron = months_between(client["birth_date"], a["date"])

    result = []
    for domain, items in SCALE["domains"].items():
        dname = norm(domain)
        # 收集「有估算月龄且已评分」的项目
        aged = []
        for it in items:
            code = it["code"]
            age = it.get("age")
            if age is None:
                continue
            sc = scored.get(code)
            if sc is None:
                continue  # 未评项忽略
            mx = it.get("max_score") or 0
            if mx <= 0:
                continue
            aged.append((age, mx, sc))

        if not aged:
            result.append({
                "domain": dname, "ability_age": None, "ability_label": "—",
                "scored_with_age": 0, "achieved": 0,
                "chronological": chron, "chronological_label": age_label(chron),
                "gap": None, "status": "未定（无定龄评分项）", "note": "",
            })
            continue

        aged.sort(key=lambda x: x[0])

        def frac(t):
            return t[2] / t[1] if t[1] > 0 else 0.0

        # 连续基本掌握（得分率≥阈值）的最远点
        last_achieved_age = None
        frontier_index = None
        for i, t in enumerate(aged):
            if frac(t) >= ACHIEVE_THRESHOLD:
                last_achieved_age = t[0]
                frontier_index = i
            else:
                break  # 出现未基本掌握项，连续段中断

        ability = None
        note = ""
        if frontier_index is not None:
            ability = float(last_achieved_age)
            # 对紧随其后的临界项做线性插值，得到连续月龄
            if frontier_index + 1 < len(aged):
                nxt = aged[frontier_index + 1]
                fn = frac(nxt)
                if fn > 0:
                    ability = last_achieved_age + (nxt[0] - last_achieved_age) * fn
                    note = "线性插值"
                else:
                    note = "止于已掌握项"
        else:
            # 连最早里程碑都未基本掌握
            f0 = frac(aged[0])
            if f0 > 0:
                ability = max(0.0, aged[0][0] * f0)
                note = "首项估算"
            else:
                ability = 0.0
                note = "零分"

        # 不连续缺口检测：中断之后又出现基本掌握项
        break_start = (frontier_index + 1) if frontier_index is not None else 0
        if any(frac(aged[j]) >= ACHIEVE_THRESHOLD for j in range(break_start, len(aged))):
            note = (note + "，有缺口").strip("，")

        achieved_count = sum(1 for t in aged if frac(t) >= ACHIEVE_THRESHOLD)

        if ability is None:
            status = "未达标"
            gap = None
        else:
            if chron is None:
                status = "已评定（缺出生日期）"
                gap = None
            else:
                diff = ability - chron
                if abs(diff) <= 3:
                    status = "符合年龄"
                    gap = round(diff, 1)
                elif diff > 3:
                    status = f"超前 {round(diff)} 个月"
                    gap = round(diff, 1)
                else:
                    status = f"滞后 {round(-diff)} 个月"
                    gap = round(diff, 1)

        result.append({
            "domain": dname,
            "ability_age": round(ability, 1) if ability is not None else None,
            "ability_label": age_label(round(ability) if ability is not None else None),
            "scored_with_age": len(aged),
            "achieved": achieved_count,
            "chronological": chron,
            "chronological_label": age_label(chron),
            "gap": gap,
            "status": status,
            "note": note,
        })

    # 综合（取各维度最低能力年龄为下限）
    valid = [r["ability_age"] for r in result if r["ability_age"] is not None]
    overall = min(valid) if valid else None
    return {
        "chronological": chron,
        "chronological_label": age_label(chron),
        "overall_ability": overall,
        "overall_label": age_label(overall),
        "achieve_threshold": ACHIEVE_THRESHOLD,
        "method": ("得分率≥阈值记为基本掌握；取连续掌握最远点，临界项线性插值定龄；±3 个月为符合年龄容差。"),
        "domains": result,
    }


# ---------- API: 认证 ----------
@app.route("/api/register", methods=["POST"])
def api_register():
    # 自助注册开关（系统配置）
    if get_setting("allow_self_register", "1") != "1":
        return jsonify({"error": "当前已关闭自助注册，请联系管理员开通账号"}), 403
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 3:
        return jsonify({"error": "用户名至少 3 个字符"}), 400
    if not username.replace("_", "").isalnum():
        return jsonify({"error": "用户名只能包含字母、数字、下划线"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码至少 6 位"}), 400
    db = get_db()
    if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
        return jsonify({"error": "该用户名已被注册"}), 400
    cur = db.execute(
        "INSERT INTO users (username, password_hash, role, status) VALUES (?,?,?,?)",
        (username, generate_password_hash(password), "user", "active"),
    )
    db.commit()
    uid = cur.lastrowid
    log_action("register", "user", uid, f"自助注册账号 {username}")
    token = generate_token(uid)
    return jsonify({
        "token": token,
        "user": {"id": uid, "username": username, "role": "user", "status": "active"},
    }), 201


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "用户名或密码错误"}), 401
    if row["status"] != "active":
        return jsonify({"error": "账号已被禁用，请联系管理员"}), 403
    log_action("login", "user", row["id"], f"登录 {username}")
    token = generate_token(row["id"])
    return jsonify({
        "token": token,
        "user": {"id": row["id"], "username": row["username"], "role": row["role"], "status": row["status"]},
    })


@app.route("/api/me", methods=["GET"])
@login_required
def api_me():
    u = g.current_user
    return jsonify({"id": u["id"], "username": u["username"], "role": u["role"], "status": u["status"]})


@app.route("/api/logout", methods=["POST"])
@login_required
def api_logout():
    log_action("logout", "user", g.current_user["id"])
    return jsonify({"ok": True})


@app.route("/api/me/password", methods=["POST"])
@login_required
def api_change_password():
    data = request.get_json(force=True)
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    if len(new) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (g.current_user["id"],)).fetchone()
    if not check_password_hash(u["password_hash"], old):
        return jsonify({"error": "原密码错误"}), 400
    db.execute(
        "UPDATE users SET password_hash=?, updated_at=datetime('now','localtime') WHERE id=?",
        (generate_password_hash(new), g.current_user["id"]),
    )
    db.commit()
    log_action("change_password", "user", g.current_user["id"])
    return jsonify({"ok": True})


# ---------- API: 公开配置（无需登录，供登录页判断是否开放注册） ----------
@app.route("/api/public-config", methods=["GET"])
def api_public_config():
    return jsonify({
        "site_name": get_setting("site_name", "语言与学习技能评估系统"),
        "allow_self_register": get_setting("allow_self_register", "1"),
    })


# ---------- API: 量表 ----------
@app.route("/api/scale")
@login_required
def api_scale():
    """返回整张量表（领域 -> 技能项 + 评分标准）"""
    out = {
        "meta": SCALE["meta"],
        "domains": [
            {
                "domain": norm(domain),
                "items": [
                    {
                        "code": it["code"],
                        "skill_point": norm(it.get("skill_point", "")),
                        "task_name": norm(it.get("task_name", "")),
                        "max_score": it.get("max_score"),
                        "goal": norm(it.get("goal", "")),
                        "question": norm(it.get("question", "")),
                        "example": norm(it.get("example", "")),
                        "note": norm(it.get("note", "")),
                        "age": it.get("age"),
                        "criteria": {str(k): v for k, v in it.get("criteria", {}).items()},
                    }
                    for it in items
                ],
            }
            for domain, items in SCALE["domains"].items()
        ],
    }
    return jsonify(out)


# ---------- API: 目录标签（被评估者状态分类；修改需管理员） ----------
@app.route("/api/client-tags", methods=["GET", "POST", "PUT", "DELETE"])
@login_required
def api_client_tags():
    db = get_db()
    if request.method == "GET":
        return jsonify(get_client_tags())
    # 以下写操作仅管理员
    if g.current_user["role"] != "admin":
        return jsonify({"error": "需要管理员权限"}), 403
    if request.method == "POST":
        data = request.get_json(force=True)
        label = (data.get("label") or "").strip()
        if not label:
            return jsonify({"error": "标签不能为空"}), 400
        tags = get_client_tags()
        if label in tags:
            return jsonify({"error": "标签已存在"}), 400
        tags.append(label)
        set_client_tags(tags)
        log_action("tag_add", "system", None, label)
        return jsonify(tags), 201
    if request.method == "PUT":
        data = request.get_json(force=True)
        old = (data.get("old") or "").strip()
        new = (data.get("new") or "").strip()
        if not old or not new:
            return jsonify({"error": "old/new 均不能为空"}), 400
        tags = get_client_tags()
        if old not in tags:
            return jsonify({"error": "原标签不存在"}), 404
        if new != old and new in tags:
            return jsonify({"error": "新标签已存在"}), 400
        tags = [new if t == old else t for t in tags]
        set_client_tags(tags)
        db.execute("UPDATE clients SET status=? WHERE status=?", (new, old))
        db.commit()
        log_action("tag_rename", "system", None, f"{old}->{new}")
        return jsonify(tags)
    # DELETE：删除标签并清除相关被评估者的 status
    label = request.args.get("label") or (request.get_json(force=True) or {}).get("label") or ""
    label = label.strip()
    tags = get_client_tags()
    if label not in tags:
        return jsonify({"error": "标签不存在"}), 404
    tags = [t for t in tags if t != label]
    set_client_tags(tags)
    db.execute("UPDATE clients SET status=NULL WHERE status=?", (label,))
    db.commit()
    log_action("tag_delete", "system", None, label)
    return jsonify(tags)


# ---------- API: 被评估者 ----------
@app.route("/api/clients", methods=["GET", "POST"])
@login_required
def api_clients():
    db = get_db()
    uid = g.current_user["id"]
    if request.method == "POST":
        data = request.get_json(force=True)
        cur = db.execute(
            "INSERT INTO clients (user_id, name, gender, birth_date, note, status) VALUES (?,?,?,?,?,?)",
            (uid, data.get("name"), data.get("gender"), data.get("birth_date"),
             data.get("note"), data.get("status") or None),
        )
        db.commit()
        cid = cur.lastrowid
        log_action("client_create", "client", cid, data.get("name"))
        return jsonify({"id": cid}), 201
    status = request.args.get("status")
    show_archived = request.args.get("archived") == "1"
    if show_archived:
        rows = db.execute(
            "SELECT * FROM clients WHERE user_id=? AND is_archived=1 ORDER BY id DESC", (uid,)
        ).fetchall()
    elif status:
        rows = db.execute(
            "SELECT * FROM clients WHERE user_id=? AND status=? AND (is_archived IS NULL OR is_archived=0) ORDER BY id DESC",
            (uid, status),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM clients WHERE user_id=? AND (is_archived IS NULL OR is_archived=0) ORDER BY id DESC",
            (uid,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/clients/<int:cid>", methods=["GET", "PUT"])
@login_required
def api_client_detail(cid):
    db = get_db()
    owner = get_owned_client(cid)
    if not owner:
        return jsonify({"error": "not found"}), 404
    if request.method == "PUT":
        data = request.get_json(force=True)
        existing = db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        row = dict(existing)
        for field in ("name", "gender", "birth_date", "note", "status"):
            if field in data:
                row[field] = data.get(field) if data.get(field) is not None else None
        db.execute(
            "UPDATE clients SET name=?, gender=?, birth_date=?, note=?, status=? WHERE id=?",
            (row["name"], row["gender"], row["birth_date"], row["note"], row["status"], cid),
        )
        db.commit()
        log_action("client_update", "client", cid, row["name"])
        c = db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        return jsonify(dict(c))
    c = db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    return jsonify(dict(c))


@app.route("/api/clients/<int:cid>", methods=["DELETE"])
@login_required
def api_archive_client(cid):
    """软归档（删除）客户：数据保留，列表默认不显示，可恢复"""
    db = get_db()
    owner = get_owned_client(cid)
    if not owner:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE clients SET is_archived=1 WHERE id=?", (cid,))
    db.commit()
    log_action("client_archive", "client", cid, owner["name"])
    return jsonify({"archived": True, "id": cid})


@app.route("/api/clients/<int:cid>/restore", methods=["POST"])
@login_required
def api_restore_client(cid):
    """从归档中恢复客户"""
    db = get_db()
    owner = get_owned_client(cid)
    if not owner:
        return jsonify({"error": "not found"}), 404
    db.execute("UPDATE clients SET is_archived=0 WHERE id=?", (cid,))
    db.commit()
    log_action("client_restore", "client", cid, owner["name"])
    return jsonify(dict(db.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()))


# ---------- API: 评估 ----------
@app.route("/api/clients/<int:cid>/assessments", methods=["GET", "POST"])
@login_required
def api_assessments(cid):
    db = get_db()
    owner = get_owned_client(cid)
    if not owner:
        return jsonify({"error": "not found"}), 404
    if request.method == "POST":
        data = request.get_json(force=True)
        today = date.today().isoformat()
        cur = db.execute(
            "INSERT INTO assessments (client_id, title, assessor, date) VALUES (?,?,?,?)",
            (cid, data.get("title", "评估"), data.get("assessor", ""), data.get("date", today)),
        )
        db.commit()
        aid = cur.lastrowid
        log_action("assessment_create", "assessment", aid, data.get("title", "评估"))
        return jsonify({"id": aid}), 201
    rows = db.execute(
        "SELECT * FROM assessments WHERE client_id=? ORDER BY id DESC", (cid,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["summary"] = compute_total(r["id"])
        out.append(d)
    return jsonify(out)


@app.route("/api/assessments/<int:aid>")
@login_required
def api_assessment_detail(aid):
    if not get_owned_assessment(aid):
        return jsonify({"error": "not found"}), 404
    db = get_db()
    a = db.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
    rows = db.execute(
        "SELECT item_code, score FROM scores WHERE assessment_id=?", (aid,)
    ).fetchall()
    scores = {r["item_code"]: r["score"] for r in rows}
    return jsonify(
        {
            "assessment": dict(a),
            "scores": scores,
            "total": compute_total(aid),
            "domains": compute_domain_summary(aid),
        }
    )


@app.route("/api/assessments/<int:aid>/score", methods=["POST"])
@login_required
def api_set_score(aid):
    if not get_owned_assessment(aid):
        return jsonify({"error": "not found"}), 404
    db = get_db()
    data = request.get_json(force=True)
    code = data.get("item_code")
    score = int(data.get("score", 0))
    if code not in SCALE_ITEMS:
        return jsonify({"error": "invalid item"}), 400
    mx = SCALE_ITEMS[code].get("max_score") or 0
    if score < 0 or score > mx:
        return jsonify({"error": f"score must be 0..{mx}"}), 400
    db.execute(
        """INSERT INTO scores (assessment_id, item_code, score) VALUES (?,?,?)
           ON CONFLICT(assessment_id, item_code) DO UPDATE SET score=excluded.score""",
        (aid, code, score),
    )
    db.commit()
    return jsonify({"ok": True, "total": compute_total(aid)})


@app.route("/api/assessments/<int:aid>/scores", methods=["POST"])
@login_required
def api_set_scores_bulk(aid):
    """批量保存多个项目分数 {code: score, ...}"""
    if not get_owned_assessment(aid):
        return jsonify({"error": "not found"}), 404
    db = get_db()
    data = request.get_json(force=True)
    scores = data.get("scores", {})
    for code, score in scores.items():
        if code not in SCALE_ITEMS:
            continue
        mx = SCALE_ITEMS[code].get("max_score") or 0
        score = max(0, min(int(score), mx))
        db.execute(
            """INSERT INTO scores (assessment_id, item_code, score) VALUES (?,?,?)
               ON CONFLICT(assessment_id, item_code) DO UPDATE SET score=excluded.score""",
            (aid, code, score),
        )
    db.commit()
    return jsonify({"ok": True, "total": compute_total(aid)})


# ---------- API: 报告导出 ----------
@app.route("/api/assessments/<int:aid>/report")
@login_required
def api_report(aid):
    if not get_owned_assessment(aid):
        return jsonify({"error": "not found"}), 404
    from reports import (make_bar_chart, make_band_chart, generate_docx,
                         generate_pdf, generate_html, build_suggestions,
                         build_chart_analysis)
    db = get_db()
    a = db.execute("SELECT * FROM assessments WHERE id=?", (aid,)).fetchone()
    client = db.execute("SELECT * FROM clients WHERE id=?", (a["client_id"],)).fetchone()
    detail = api_assessment_detail(aid).get_json()
    total = detail["total"]
    domains = detail["domains"]
    charts = [make_bar_chart(domains), make_band_chart(domains)]
    analysis = build_chart_analysis(domains, total)
    ability = compute_ability_ages(aid)
    rows = db.execute(
        "SELECT item_code, score FROM scores WHERE assessment_id=?", (aid,)
    ).fetchall()
    scored = {r["item_code"]: r["score"] for r in rows}
    suggestions = build_suggestions(domains, total, ability, SCALE_ITEMS, scored,
                                     adapt_months=get_adapt_months(aid))
    fmt = request.args.get("fmt", "docx").lower()

    cdict = dict(client)
    adict = dict(a)
    if fmt == "html":
        html = generate_html(adict, cdict, total, domains, charts, analysis, ability, suggestions)
        log_action("report_export", "assessment", aid, "html")
        return Response(html, mimetype="text/html")
    if fmt == "pdf":
        data = generate_pdf(adict, cdict, total, domains, charts, analysis, ability, suggestions)
        mimetype = "application/pdf"
        ext = "pdf"
        log_action("report_export", "assessment", aid, "pdf")
    else:
        data = generate_docx(adict, cdict, total, domains, charts, analysis, ability, suggestions)
        mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ext = "docx"
        log_action("report_export", "assessment", aid, "docx")
    fname = f"评估报告_{cdict.get('name','')}_{adict.get('date','')}.{ext}"
    return send_file(
        io.BytesIO(data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=fname,
    )


@app.route("/api/assessments/<int:aid>/ability")
@login_required
def api_ability(aid):
    if not get_owned_assessment(aid):
        return jsonify({"error": "not found"}), 404
    return jsonify(compute_ability_ages(aid))


@app.route("/api/assessments/<int:aid>/suggestions")
@login_required
def api_suggestions(aid):
    """干预建议结构化数据，供网页端「干预建议」面板使用（与导出报告内容一致）。"""
    if not get_owned_assessment(aid):
        return jsonify({"error": "not found"}), 404
    from reports import build_suggestions
    detail = api_assessment_detail(aid).get_json()
    total = detail["total"]
    domains = detail["domains"]
    ability = compute_ability_ages(aid)
    rows = get_db().execute(
        "SELECT item_code, score FROM scores WHERE assessment_id=?", (aid,)
    ).fetchall()
    scored = {r["item_code"]: r["score"] for r in rows}
    suggestions = build_suggestions(domains, total, ability, SCALE_ITEMS, scored,
                                     adapt_months=get_adapt_months(aid))
    return jsonify({
        "suggestions": suggestions,
        "achieve_threshold": ACHIEVE_THRESHOLD,
        "total": total,
    })


# ---------- API: 多次评估成长曲线 ----------
@app.route("/api/clients/<int:cid>/progress")
@login_required
def api_progress(cid):
    if not get_owned_client(cid):
        return jsonify({"error": "not found"}), 404
    db = get_db()
    rows = db.execute(
        "SELECT * FROM assessments WHERE client_id=? ORDER BY date ASC, id ASC",
        (cid,),
    ).fetchall()
    series = []
    for r in rows:
        aid = r["id"]
        d = api_assessment_detail(aid).get_json()
        series.append(
            {
                "id": aid,
                "title": r["title"],
                "date": r["date"],
                "total": d["total"],
                "domains": d["domains"],
            }
        )
    domain_names = [norm(d) for d in SCALE["domains"].keys()]
    domain_series = {}
    for name in domain_names:
        domain_series[name] = [
            next((x["rate"] for x in s["domains"] if x["domain"] == name), None)
            for s in series
        ]
    total_series = [s["total"]["rate"] for s in series]
    labels = [f"{s['date']}\n{s['title']}" for s in series]
    return jsonify(
        {
            "labels": labels,
            "total_series": total_series,
            "domain_series": domain_series,
            "domain_names": domain_names,
            "count": len(series),
        }
    )


# ---------- API: 管理员后台 ----------
@app.route("/api/admin/stats", methods=["GET"])
@admin_required
def api_admin_stats():
    db = get_db()
    stats = {}
    stats["users"] = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    stats["users_active"] = db.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
    stats["clients"] = db.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    stats["assessments"] = db.execute("SELECT COUNT(*) FROM assessments").fetchone()[0]
    stats["scores"] = db.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    stats["logs"] = db.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
    # 近 7 天注册数（含今天）
    stats["recent_registrations"] = db.execute(
        "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now','localtime','-7 days')"
    ).fetchone()[0]
    return jsonify(stats)


@app.route("/api/admin/users", methods=["GET", "POST"])
@admin_required
def api_admin_users():
    db = get_db()
    if request.method == "POST":
        data = request.get_json(force=True)
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        role = data.get("role") or "user"
        if role not in ("user", "admin"):
            role = "user"
        if len(username) < 3:
            return jsonify({"error": "用户名至少 3 个字符"}), 400
        if not username.replace("_", "").isalnum():
            return jsonify({"error": "用户名只能包含字母、数字、下划线"}), 400
        if len(password) < 6:
            return jsonify({"error": "密码至少 6 位"}), 400
        if db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone():
            return jsonify({"error": "该用户名已存在"}), 400
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, status) VALUES (?,?,?,?)",
            (username, generate_password_hash(password), role, "active"),
        )
        db.commit()
        uid = cur.lastrowid
        log_action("admin_user_create", "user", uid, f"管理员创建账号 {username} 角色 {role}")
        return jsonify({"id": uid, "username": username, "role": role, "status": "active"}), 201
    rows = db.execute(
        "SELECT u.id, u.username, u.role, u.status, u.created_at, "
        "(SELECT COUNT(*) FROM clients c WHERE c.user_id=u.id) AS client_count "
        "FROM users u ORDER BY u.id"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/users/<int:uid>", methods=["PUT", "DELETE"])
@admin_required
def api_admin_user_detail(uid):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return jsonify({"error": "not found"}), 404
    if request.method == "PUT":
        data = request.get_json(force=True)
        role = data.get("role")
        status = data.get("status")
        new_pwd = data.get("password")
        # 安全护栏：禁止管理员对自己禁用或降级，避免锁死后台
        if uid == g.current_user["id"]:
            if status == "disabled":
                return jsonify({"error": "不能禁用自己的账号"}), 400
            if role == "user":
                return jsonify({"error": "不能将自身降级为普通用户"}), 400
        sets, params = [], []
        if role in ("user", "admin"):
            sets.append("role=?")
            params.append(role)
        if status in ("active", "disabled"):
            sets.append("status=?")
            params.append(status)
        if new_pwd:
            if len(new_pwd) < 6:
                return jsonify({"error": "密码至少 6 位"}), 400
            sets.append("password_hash=?")
            params.append(generate_password_hash(new_pwd))
        sets.append("updated_at=datetime('now','localtime')")
        params.append(uid)
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", params)
        db.commit()
        log_action("admin_user_update", "user", uid, f"角色 {role or '-'} 状态 {status or '-'} 改密 {'是' if new_pwd else '否'}")
        return jsonify({"ok": True})
    # DELETE：级联删除该用户的所有被评估者（含其评估与评分）
    if uid == g.current_user["id"]:
        return jsonify({"error": "不能删除自己的账号"}), 400
    if u["role"] == "admin":
        admin_cnt = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]
        if admin_cnt <= 1:
            return jsonify({"error": "不能删除最后一个管理员账号"}), 400
    db.execute("DELETE FROM scores WHERE assessment_id IN (SELECT id FROM assessments WHERE client_id IN (SELECT id FROM clients WHERE user_id=?))", (uid,))
    db.execute("DELETE FROM assessments WHERE client_id IN (SELECT id FROM clients WHERE user_id=?)", (uid,))
    db.execute("DELETE FROM clients WHERE user_id=?", (uid,))
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    log_action("admin_user_delete", "user", uid, f"删除账号 {u['username']} 及其全部数据")
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>/clients", methods=["GET"])
@admin_required
def api_admin_user_clients(uid):
    """管理员查看某用户的被评估者（用于支持/排查）"""
    db = get_db()
    u = db.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        return jsonify({"error": "not found"}), 404
    rows = db.execute(
        "SELECT * FROM clients WHERE user_id=? ORDER BY id DESC", (uid,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/logs", methods=["GET"])
@admin_required
def api_admin_logs():
    db = get_db()
    limit = int(request.args.get("limit", 100))
    offset = int(request.args.get("offset", 0))
    rows = db.execute(
        "SELECT l.id, l.user_id, u.username, l.action, l.target_type, l.target_id, l.detail, l.ip, l.created_at "
        "FROM operation_logs l LEFT JOIN users u ON u.id=l.user_id "
        "ORDER BY l.id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    total = db.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
    return jsonify({"total": total, "logs": [dict(r) for r in rows]})


@app.route("/api/admin/config", methods=["GET", "PUT"])
@admin_required
def api_admin_config():
    if request.method == "GET":
        return jsonify({
            "site_name": get_setting("site_name", "语言与学习技能评估系统"),
            "allow_self_register": get_setting("allow_self_register", "1"),
            "client_tags": get_client_tags(),
        })
    data = request.get_json(force=True)
    if "site_name" in data:
        set_setting("site_name", str(data["site_name"])[:60])
    if "allow_self_register" in data:
        set_setting("allow_self_register", "1" if str(data["allow_self_register"]) == "1" else "0")
    if "client_tags" in data and isinstance(data["client_tags"], list):
        set_client_tags([str(t) for t in data["client_tags"]])
    log_action("admin_config_update", "system", None, "更新系统配置")
    return jsonify({"ok": True})


# ---------- 前端 ----------
@app.route("/")
def index():
    # 关闭缓存，确保前端更新后立即生效（避免浏览器长期缓存旧 index.html）
    resp = send_from_directory(STATIC_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/admin")
def admin_page():
    resp = send_from_directory(STATIC_DIR, "admin.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    app.logger.error(f"Unhandled exception: {e}\n{traceback.format_exc()}")
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


# --- Vercel Serverless 路径修复 ---
# Vercel rewrite 到 /api/index 后，会把原始路径放在 query string 的 path 参数里，
# 但 Flask 收到的 PATH_INFO 是 /api/index，导致全站 404。
# 这里在 WSGI 层把 PATH_INFO 还原为真实路径。
if os.environ.get("VERCEL"):
    import urllib.parse

    _flask_app_local = app

    def _vercel_wsgi_app(environ, start_response):
        path_info = environ.get("PATH_INFO", "")
        if path_info == "/api/index":
            qs = environ.get("QUERY_STRING", "")
            params = urllib.parse.parse_qs(qs)
            real_path = params.get("path", ["/"])
            environ["PATH_INFO"] = real_path[0] if isinstance(real_path, list) else real_path
            # 保留其它 query 参数
            rest = {k: v for k, v in params.items() if k != "path"}
            environ["QUERY_STRING"] = "&".join(
                f"{k}={v[0]}" for k, v in rest.items()
            )
        return _flask_app_local(environ, start_response)

    app = _vercel_wsgi_app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # debug=False：关闭自动重载器，避免重载子进程在沙箱只读命名空间下无法写入数据库
    app.run(host="0.0.0.0", port=port, debug=False)
