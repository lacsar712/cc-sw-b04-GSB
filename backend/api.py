import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, get, post
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from passlib.context import CryptContext
from psycopg.rows import dict_row
from pydantic import BaseModel

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
SECRET = os.environ.get("JWT_SECRET", "spectrum-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
USERS = {
    "calibrator": {"role": "writer", "password_hash": pwd.hash("calib123456")},
    "inspector": {"role": "reader", "password_hash": pwd.hash("insp123456")},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id serial PRIMARY KEY,
    lamp text NOT NULL,
    nominal_nm double precision NOT NULL,
    measured_nm double precision NOT NULL,
    status text NOT NULL,
    verdict text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS filter_schemes (
    id serial PRIMARY KEY,
    name text NOT NULL,
    prefix text NOT NULL DEFAULT '',
    note text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    updated_by text NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (name)
);
CREATE TABLE IF NOT EXISTS filter_history (
    id serial PRIMARY KEY,
    scheme_id integer,
    scheme_name text NOT NULL,
    action text NOT NULL,
    prefix text NOT NULL DEFAULT '',
    note text NOT NULL DEFAULT '',
    actor text NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filter_history_sid ON filter_history(scheme_id);
"""

# LIKE 中需要转义的特殊字符，避免用户前缀里的 % / _ / \ 被当通配符
LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": r"\%", "_": r"\_"})


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    lamp: str
    nominal_nm: float
    measured_nm: float


class SchemeIn(BaseModel):
    name: str
    prefix: str = ""
    note: str = ""


def user_from_request(request: Request) -> dict:
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = jwt.decode(auth[7:], SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌") from exc
    if payload.get("sub") not in USERS:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌")
    return {"username": payload["sub"], "role": payload.get("role")}


@get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "spectrum-wavelength-desk"}


@post("/api/login")
async def login(data: LoginIn) -> dict:
    u = USERS.get(data.username)
    if not u or not pwd.verify(data.password, u["password_hash"]):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    token = jwt.encode(
        {
            "sub": data.username,
            "role": u["role"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=12),
        },
        SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "role": u["role"], "username": data.username}


@get("/api/jobs")
async def list_jobs(request: Request, prefix: str = "") -> list:
    user_from_request(request)
    prefix = (prefix or "").strip()
    with connect() as conn:
        if prefix:
            like = prefix.translate(LIKE_ESCAPE) + "%"
            rows = conn.execute(
                """
                SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by
                FROM jobs
                WHERE lamp LIKE %s ESCAPE '\\'
                ORDER BY id DESC
                """,
                (like,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs ORDER BY id DESC"
            ).fetchall()
        return list(rows)


@get("/api/jobs/{job_id:int}")
async def get_job(request: Request, job_id: int) -> dict:
    user_from_request(request)
    with connect() as conn:
        row = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        return dict(row)


@post("/api/jobs")
async def create_job(request: Request, data: JobIn) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可提交")
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
            VALUES (%s,%s,%s,'pending','','',%s,%s) RETURNING id
            """,
            (data.lamp.strip(), data.nominal_nm, data.measured_nm, user["username"], datetime.now(timezone.utc)),
        ).fetchone()
        conn.commit()
        return {"id": row["id"], "status": "pending"}


def log_history(conn, *, scheme_id, scheme_name, action, prefix, note, actor) -> None:
    conn.execute(
        """
        INSERT INTO filter_history(scheme_id, scheme_name, action, prefix, note, actor, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (scheme_id, scheme_name, action, prefix, note, actor, datetime.now(timezone.utc)),
    )


def can_delete(user: dict, scheme: dict) -> bool:
    # 方案创建者本人可删；校准员（可写角色）可删任意方案；巡检不可删他人方案
    return scheme["created_by"] == user["username"] or user["role"] == "writer"


def _jsonable(rows):
    # 显式把 datetime 转为 ISO 字符串，保证返回可被 JSON 序列化
    out = []
    for row in rows:
        item = {}
        for k, v in dict(row).items():
            item[k] = v.isoformat() if isinstance(v, datetime) else v
        out.append(item)
    return out


@get("/api/filter-schemes")
async def list_schemes(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, prefix, note, created_by, updated_by, created_at, updated_at
            FROM filter_schemes ORDER BY id DESC
            """
        ).fetchall()
        return _jsonable(rows)


@post("/api/filter-schemes")
async def create_scheme(request: Request, data: SchemeIn) -> dict:
    user = user_from_request(request)
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="方案名不能为空")
    prefix = data.prefix.strip()
    note = data.note.strip()
    now = datetime.now(timezone.utc)
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM filter_schemes WHERE name=%s",
            (name,),
        ).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="同名方案已存在")
        row = conn.execute(
            """
            INSERT INTO filter_schemes(name, prefix, note, created_by, updated_by, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (name, prefix, note, user["username"], user["username"], now, now),
        ).fetchone()
        log_history(
            conn,
            scheme_id=row["id"],
            scheme_name=name,
            action="create",
            prefix=prefix,
            note=note,
            actor=user["username"],
        )
        conn.commit()
        return {"id": row["id"], "name": name, "prefix": prefix, "note": note}


@post("/api/filter-schemes/{scheme_id:int}")
async def update_scheme(request: Request, scheme_id: int, data: SchemeIn) -> dict:
    user = user_from_request(request)
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="方案名不能为空")
    prefix = data.prefix.strip()
    note = data.note.strip()
    now = datetime.now(timezone.utc)
    with connect() as conn:
        scheme = conn.execute(
            "SELECT id, name, created_by FROM filter_schemes WHERE id=%s", (scheme_id,)
        ).fetchone()
        if not scheme:
            raise HTTPException(status_code=404, detail="方案不存在")
        # 校准员与巡检均可保存变动（仅删除对巡检有归属限制）
        dup = conn.execute(
            "SELECT created_by FROM filter_schemes WHERE name=%s AND id<>%s",
            (name, scheme_id),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=409, detail="同名方案已存在")
        conn.execute(
            "UPDATE filter_schemes SET name=%s, prefix=%s, note=%s, updated_by=%s, updated_at=%s WHERE id=%s",
            (name, prefix, note, user["username"], now, scheme_id),
        )
        log_history(
            conn,
            scheme_id=scheme_id,
            scheme_name=name,
            action="update",
            prefix=prefix,
            note=note,
            actor=user["username"],
        )
        conn.commit()
        return {"id": scheme_id, "name": name, "prefix": prefix, "note": note}


@post("/api/filter-schemes/{scheme_id:int}/delete")
async def delete_scheme(request: Request, scheme_id: int) -> dict:
    user = user_from_request(request)
    with connect() as conn:
        scheme = conn.execute(
            "SELECT id, name, prefix, note, created_by FROM filter_schemes WHERE id=%s", (scheme_id,)
        ).fetchone()
        if not scheme:
            raise HTTPException(status_code=404, detail="方案不存在")
        if not can_delete(user, scheme):
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="巡检不可删除他人方案")
        conn.execute("DELETE FROM filter_schemes WHERE id=%s", (scheme_id,))
        # 删除后仍在履历留痕
        log_history(
            conn,
            scheme_id=scheme_id,
            scheme_name=scheme["name"],
            action="delete",
            prefix=scheme["prefix"],
            note=scheme["note"],
            actor=user["username"],
        )
        conn.commit()
        return {"deleted": scheme_id}


@get("/api/filter-history")
async def list_history(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, scheme_id, scheme_name, action, prefix, note, actor, created_at
            FROM filter_history ORDER BY id DESC
            """
        ).fetchall()
        return _jsonable(rows)


def on_startup() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            conn.execute(
                """
                INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
                VALUES
                ('氦灯-587', 587.56, 587.50, 'done', '合格', '偏差 0.0600 nm 在允差内', 'seed', %s),
                ('汞灯-546', 546.07, 546.30, 'done', '超差', '偏差 0.2300 nm 超过允差 0.08', 'seed', %s)
                """,
                (now, now),
            )
        conn.commit()


app = Litestar(
    route_handlers=[
        health,
        login,
        list_jobs,
        get_job,
        create_job,
        list_schemes,
        create_scheme,
        update_scheme,
        delete_scheme,
        list_history,
    ],
    on_startup=[on_startup],
)
