import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, delete, get, post, put
from litestar.exceptions import HTTPException
from litestar.status_codes import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
)
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

SCHEMA_STATEMENTS = [
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filter_schemes (
        id serial PRIMARY KEY,
        name text NOT NULL,
        prefix text NOT NULL DEFAULT '',
        description text NOT NULL DEFAULT '',
        created_by text NOT NULL,
        created_at timestamptz NOT NULL,
        updated_at timestamptz NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filter_scheme_events (
        id serial PRIMARY KEY,
        scheme_id integer,
        scheme_name text NOT NULL,
        action text NOT NULL,
        detail text NOT NULL DEFAULT '',
        actor text NOT NULL,
        created_at timestamptz NOT NULL
    )
    """,
]


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
    description: str = ""


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


JOB_COLS = "id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by"


def like_prefix_pattern(prefix: str) -> str:
    """Escape LIKE metacharacters so the prefix matches literally."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@get("/api/jobs")
async def list_jobs(request: Request, prefix: str = "") -> list:
    user_from_request(request)
    prefix = prefix.strip()
    with connect() as conn:
        if prefix:
            rows = conn.execute(
                f"SELECT {JOB_COLS} FROM jobs WHERE lamp LIKE %s ESCAPE '\\' ORDER BY id DESC",
                (like_prefix_pattern(prefix),),
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT {JOB_COLS} FROM jobs ORDER BY id DESC").fetchall()
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


def log_scheme_event(conn, scheme_id, scheme_name, action, detail, actor):
    conn.execute(
        """
        INSERT INTO filter_scheme_events(scheme_id, scheme_name, action, detail, actor, created_at)
        VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (scheme_id, scheme_name, action, detail, actor, datetime.now(timezone.utc)),
    )


@get("/api/filter/schemes")
async def list_schemes(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, name, prefix, description, created_by,
                   to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at,
                   to_char(updated_at, 'YYYY-MM-DD HH24:MI:SS') AS updated_at
            FROM filter_schemes ORDER BY id
            """
        ).fetchall()
        return list(rows)


@post("/api/filter/schemes")
async def create_scheme(request: Request, data: SchemeIn) -> dict:
    user = user_from_request(request)
    name = data.name.strip()
    prefix = data.prefix.strip()
    description = data.description.strip()
    if not name:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="方案名不能为空")
    now = datetime.now(timezone.utc)
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO filter_schemes(name, prefix, description, created_by, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
            """,
            (name, prefix, description, user["username"], now, now),
        ).fetchone()
        log_scheme_event(conn, row["id"], name, "created", f"新建方案，前缀「{prefix}」", user["username"])
        conn.commit()
        return {"id": row["id"], "name": name}


@put("/api/filter/schemes/{scheme_id:int}")
async def update_scheme(request: Request, scheme_id: int, data: SchemeIn) -> dict:
    user = user_from_request(request)
    name = data.name.strip()
    prefix = data.prefix.strip()
    description = data.description.strip()
    if not name:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="方案名不能为空")
    with connect() as conn:
        old = conn.execute(
            "SELECT id, name, prefix, description FROM filter_schemes WHERE id = %s", (scheme_id,)
        ).fetchone()
        if not old:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="方案不存在")
        changes = []
        if old["name"] != name:
            changes.append(f"名称「{old['name']}」→「{name}」")
        if old["prefix"] != prefix:
            changes.append(f"前缀「{old['prefix']}」→「{prefix}」")
        if old["description"] != description:
            changes.append("说明已更新")
        conn.execute(
            "UPDATE filter_schemes SET name=%s, prefix=%s, description=%s, updated_at=%s WHERE id=%s",
            (name, prefix, description, datetime.now(timezone.utc), scheme_id),
        )
        log_scheme_event(conn, scheme_id, name, "updated", "；".join(changes) if changes else "无变化", user["username"])
        conn.commit()
        return {"id": scheme_id, "name": name}


@delete("/api/filter/schemes/{scheme_id:int}", status_code=200)
async def delete_scheme(request: Request, scheme_id: int) -> dict:
    user = user_from_request(request)
    with connect() as conn:
        old = conn.execute(
            "SELECT id, name, prefix, created_by FROM filter_schemes WHERE id = %s", (scheme_id,)
        ).fetchone()
        if not old:
            raise HTTPException(status_code=HTTP_404_NOT_FOUND, detail="方案不存在")
        if user["role"] != "writer" and old["created_by"] != user["username"]:
            raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="巡检不可删他人方案")
        conn.execute("DELETE FROM filter_schemes WHERE id = %s", (scheme_id,))
        log_scheme_event(conn, scheme_id, old["name"], "deleted", f"删除方案，前缀「{old['prefix']}」", user["username"])
        conn.commit()
        return {"id": scheme_id, "deleted": True}


@get("/api/filter/events")
async def list_scheme_events(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, scheme_id, scheme_name, action, detail, actor,
                   to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created_at
            FROM filter_scheme_events ORDER BY id DESC
            """
        ).fetchall()
        return list(rows)


def on_startup() -> None:
    with connect() as conn:
        for stmt in SCHEMA_STATEMENTS:
            conn.execute(stmt)
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
        list_scheme_events,
    ],
    on_startup=[on_startup],
)
