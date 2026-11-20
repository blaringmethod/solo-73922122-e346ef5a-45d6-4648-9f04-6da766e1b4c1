"""SQLite 存储层。

四张表：
- sessions:       当前输入（原文/修订文/标签）+ 单调递增的 revision。
                  每次修改输入 revision 必变（不是内容哈希，改回去也不会复原）。
- selections:     用户在某个 revision 上的选择，随 revision 失效。
- results:        确认成功后原子写入的最终结果；失败不落任何行。
- glossary_pages: 候选词表登记：扫描页号 -> (会话, 确认版本)。
                  同一页号再次登记即更新；版本失效时保留登记并标为待补录。

并发：每次操作独立连接，关键写路径用 BEGIN IMMEDIATE 串行化，
确认时的"检查 revision + 写结果"在同一事务内完成，避免 TOCTOU。
词表批量登记也在单个 IMMEDIATE 事务内校验并写入，失败整批回滚。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone

from .glossary import MAX_GLOSSARY_PAGES, GlossaryBatchError, build_glossary_view

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,
  original   TEXT NOT NULL,
  revised    TEXT NOT NULL,
  labels     TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selections (
  session_id TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  data       TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (session_id, revision)
);
CREATE TABLE IF NOT EXISTS results (
  session_id TEXT NOT NULL,
  revision   INTEGER NOT NULL,
  data       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (session_id, revision)
);
CREATE TABLE IF NOT EXISTS glossary_pages (
  page                INTEGER PRIMARY KEY,
  session_id          TEXT NOT NULL,
  registered_revision INTEGER NOT NULL,
  updated_at          TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> str:
    return os.environ.get("DB_PATH", "./data/app.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


# ---------- sessions ----------

def create_session(original: str, revised: str, labels: list[dict]) -> dict:
    sid = uuid.uuid4().hex
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(id, original, revised, labels, revision,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (sid, original, revised, json.dumps(labels, ensure_ascii=False),
             1, now, now))
    return get_session(sid)


def get_session(sid: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        return None
    return _session_from_row(row)


def _session_from_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "original": row["original"],
        "revised": row["revised"],
        "labels": json.loads(row["labels"]),
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def update_inputs(sid: str, original: str, revised: str,
                  labels: list[dict]) -> dict | None:
    """修改输入：revision 必 +1，旧选择与旧结果同事务删除（改回也不恢复）。"""
    now = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT revision FROM sessions WHERE id=?", (sid,))
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            return None
        new_rev = row["revision"] + 1
        conn.execute(
            "UPDATE sessions SET original=?, revised=?, labels=?, revision=?,"
            " updated_at=? WHERE id=?",
            (original, revised, json.dumps(labels, ensure_ascii=False),
             new_rev, now, sid))
        conn.execute("DELETE FROM selections WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM results WHERE session_id=?", (sid,))
        conn.commit()
    return get_session(sid)


# ---------- selections ----------

def get_selections(sid: str, revision: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT data FROM selections WHERE session_id=? AND revision=?",
            (sid, revision)).fetchone()
    return json.loads(row["data"]) if row else {}


# ---------- results ----------

def get_result(sid: str, revision: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT data, created_at FROM results"
            " WHERE session_id=? AND revision=?", (sid, revision)).fetchone()
    if row is None:
        return None
    return {"data": json.loads(row["data"]), "created_at": row["created_at"]}


# ---------- glossary ----------

_GLOSSARY_ROWS_SQL = """
SELECT g.page, g.session_id, g.registered_revision, g.updated_at,
       s.revision AS current_revision,
       r.data     AS result_data
FROM glossary_pages AS g
LEFT JOIN sessions AS s ON s.id = g.session_id
LEFT JOIN results  AS r ON r.session_id = g.session_id
                       AND r.revision = g.registered_revision
ORDER BY g.page
"""


def get_glossary() -> dict:
    """在同一事务里一次性读取全部登记行，保证词表/证据/待补录同源。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        rows = [dict(r) for r in conn.execute(_GLOSSARY_ROWS_SQL).fetchall()]
        conn.commit()
    finally:
        conn.close()
    return build_glossary_view(rows)


def register_glossary_pages(entries: list[dict]) -> dict:
    """整批登记/更新页号。任一校验失败则整批回滚，原登记保持不变。

    entries: [{"page", "session_id", "revision"}]，已由 schemas 规范化。
    失败时抛出 GlossaryBatchError，detail 里带相关页号。
    """
    now = _now()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = {row["page"]: row["session_id"]
                    for row in conn.execute(
                        "SELECT page, session_id FROM glossary_pages")}

        errors: list[dict] = []
        invalid_pages: set[int] = set()

        # 1) 同批页号重复
        counts = Counter(e["page"] for e in entries)
        for page in sorted(p for p, c in counts.items() if c > 1):
            invalid_pages.add(page)
            errors.append({
                "code": "duplicate_pages",
                "pages": [page],
                "message": f"页 {page} 在同一批中出现了 {counts[page]} 次",
            })

        # 2) 会话 / 版本是否有效
        unique = {}
        for e in entries:
            unique.setdefault(e["page"], e)
        session_ids = sorted({e["session_id"] for e in unique.values()})
        session_rows = {}
        if session_ids:
            placeholders = ",".join("?" * len(session_ids))
            for row in conn.execute(
                    "SELECT s.id, s.revision, r.data AS result_data"
                    " FROM sessions AS s"
                    " LEFT JOIN results AS r ON r.session_id = s.id"
                    "                      AND r.revision = s.revision"
                    f" WHERE s.id IN ({placeholders})",
                    session_ids):
                session_rows[row["id"]] = row

        for page in sorted(unique):
            e = unique[page]
            row = session_rows.get(e["session_id"])
            if row is None:
                invalid_pages.add(page)
                errors.append({
                    "code": "session_not_found",
                    "pages": [page],
                    "session_id": e["session_id"],
                    "message": f"页 {page} 的会话不存在",
                })
            elif row["revision"] != e["revision"]:
                invalid_pages.add(page)
                if e["revision"] > row["revision"]:
                    message = (f"页 {page} 提交的版本 r{e['revision']} 不存在"
                               f"（当前版本 r{row['revision']}）")
                else:
                    message = (f"页 {page} 提交的版本 r{e['revision']} 已过期，"
                               f"当前版本为 r{row['revision']}，需重新确认")
                errors.append({
                    "code": "revision_expired",
                    "pages": [page],
                    "session_id": e["session_id"],
                    "revision": e["revision"],
                    "current_revision": row["revision"],
                    "message": message,
                })
            elif row["result_data"] is None:
                invalid_pages.add(page)
                errors.append({
                    "code": "revision_unconfirmed",
                    "pages": [page],
                    "session_id": e["session_id"],
                    "revision": e["revision"],
                    "message": (f"页 {page} 提交的版本 r{e['revision']} "
                                "尚未确认成功"),
                })

        # 3) 一个会话只能对应一个页号（按更新后的结果计算；
        #    本批中不合格的页保持原登记，不参与新映射）
        effective = dict(existing)
        for page, e in unique.items():
            if page not in invalid_pages:
                effective[page] = e["session_id"]
        pages_by_session: dict[str, set[int]] = defaultdict(set)
        for page, sid in effective.items():
            pages_by_session[sid].add(page)
        for sid in sorted(pages_by_session):
            pages = sorted(pages_by_session[sid])
            if len(pages) > 1:
                errors.append({
                    "code": "session_multi_page",
                    "pages": pages,
                    "session_id": sid,
                    "message": (f"会话 {sid} 在更新后对应多个页号："
                                + "、".join(map(str, pages))),
                })

        # 4) 清单容量上限
        if len(effective) > MAX_GLOSSARY_PAGES:
            new_pages = sorted(set(effective) - set(existing))
            errors.append({
                "code": "capacity_exceeded",
                "pages": new_pages or sorted(effective),
                "message": (f"登记清单最多 {MAX_GLOSSARY_PAGES} 页，"
                            f"本批写入后会有 {len(effective)} 页"),
            })

        if errors:
            conn.rollback()
            related = sorted({p for err in errors for p in err["pages"]})
            raise GlossaryBatchError({
                "code": "batch_rejected",
                "message": "整批登记被拒绝，原有登记保持不变",
                "pages": related,
                "errors": errors,
            })

        for page in sorted(unique):
            e = unique[page]
            conn.execute(
                "INSERT INTO glossary_pages(page, session_id,"
                " registered_revision, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(page) DO UPDATE SET"
                " session_id=excluded.session_id,"
                " registered_revision=excluded.registered_revision,"
                " updated_at=excluded.updated_at",
                (page, e["session_id"], e["revision"], now))
        conn.commit()
    except GlossaryBatchError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_glossary()

