"""候选词表功能测试：批量登记、整批拒绝、更新、计数、失效处理与快照一致性。"""

import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


def make_confirmed(client, original, revised, labels):
    """创建一个已确认会话，返回 (session_id, revision)。"""
    r = client.post("/api/sessions", json={
        "original": original, "revised": revised, "labels": labels})
    assert r.status_code == 201, r.text
    st = r.json()
    sid, rev = st["session_id"], st["revision"]
    r = client.post(f"/api/sessions/{sid}/confirm", json={"revision": rev})
    assert r.status_code == 200, r.text
    return sid, rev


def register(client, entries):
    return client.post("/api/glossary/pages", json={"entries": entries})


def glossary(client):
    r = client.get("/api/glossary")
    assert r.status_code == 200, r.text
    return r.json()


def term_map(body):
    return {t["text"]: t for t in body["terms"]}


# ---------------- 页面与基本登记 ----------------

class TestGlossaryBasics:
    def test_page_served(self, client):
        r = client.get("/glossary")
        assert r.status_code == 200
        assert "候选词表" in r.text

    def test_empty_glossary(self, client):
        body = glossary(client)
        assert body["pages"] == [] and body["terms"] == [] and body["pending"] == []
        assert body["max_pages"] == 30 and body["min_support"] == 2

    def test_single_page_term_filtered_out(self, client):
        sid1, rev1 = make_confirmed(
            client, "中国人民银行", "中国人民银行",
            [{"id": "b", "start": 0, "end": 4}])
        sid2, rev2 = make_confirmed(
            client, "中国银行", "中国银行", [{"id": "b", "start": 0, "end": 2}])

        r = register(client, [
            {"page": 1, "session_id": sid1, "revision": rev1},
            {"page": 2, "session_id": sid2, "revision": rev2},
        ])
        assert r.status_code == 200, r.text
        body = r.json()
        assert [p["page"] for p in body["pages"]] == [1, 2]
        assert all(p["status"] == "active" for p in body["pages"])
        # 两页的标注文字分别是“中国人民”“中国”，各只 1 页支持，都不到门槛
        assert body["terms"] == []

    def test_terms_need_two_distinct_pages(self, client):
        sid1, rev1 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        sid2, rev2 = make_confirmed(
            client, "abcdef", "abcdef", [{"id": "a", "start": 0, "end": 3}])
        body = register(client, [
            {"page": 1, "session_id": sid1, "revision": rev1},
            {"page": 2, "session_id": sid2, "revision": rev2},
        ]).json()
        terms = term_map(body)
        assert terms["abc"]["support"] == 2
        assert terms["abc"]["pages"] == [1, 2]
        ev = terms["abc"]["evidence"]
        assert {(e["page"], e["start"], e["end"]) for e in ev} == {
            (1, 0, 3), (2, 0, 3)}
        assert all(e["session_id"] in {sid1, sid2} for e in ev)

    def test_same_text_twice_in_one_page_counts_once(self, client):
        # 修订文里两处标注同一文字，同一页仍只算 1 页支持
        sid, rev = make_confirmed(
            client, "abcabc", "abcabc",
            [{"id": "x", "start": 0, "end": 3}, {"id": "y", "start": 3, "end": 6}])
        sid2, rev2 = make_confirmed(
            client, "z", "z", [{"id": "q", "start": 0, "end": 1}])
        body = register(client, [
            {"page": 1, "session_id": sid, "revision": rev},
            {"page": 2, "session_id": sid2, "revision": rev2},
        ]).json()
        assert body["terms"] == []          # “abc”只在页 1 出现，共 1 页
        # 再登记一页同文，才达到两页；页 1 的两次标注在证据里都保留
        sid3, rev3 = make_confirmed(
            client, "abc", "abc", [{"id": "r", "start": 0, "end": 3}])
        body = register(client, [
            {"page": 3, "session_id": sid3, "revision": rev3},
        ]).json()
        terms = term_map(body)
        assert terms["abc"]["support"] == 2
        assert terms["abc"]["pages"] == [1, 3]
        ev_page1 = [e for e in terms["abc"]["evidence"] if e["page"] == 1]
        assert len(ev_page1) == 2

    def test_register_updates_existing_page(self, client):
        sid, rev = make_confirmed(
            client, "ab", "ab", [{"id": "a", "start": 0, "end": 2}])
        sid2, rev2 = make_confirmed(
            client, "ab", "ab", [{"id": "a", "start": 0, "end": 2}])
        register(client, [{"page": 1, "session_id": sid, "revision": rev}])
        body = register(client, [
            {"page": 1, "session_id": sid2, "revision": rev2},
            {"page": 2, "session_id": sid, "revision": rev},
        ]).json()
        # 页 1 已被更新为 sid2；sid 改登记到页 2，仍满足“一会话一页”
        by_page = {p["page"]: p for p in body["pages"]}
        assert by_page[1]["session_id"] == sid2
        assert by_page[2]["session_id"] == sid
        assert term_map(body)["ab"]["support"] == 2

    def test_exact_annotation_text_only(self, client):
        # 不做归一化：空白与大小写不同的标注文字是不同词条
        sid1, rev1 = make_confirmed(
            client, "Alpha", "Alpha", [{"id": "a", "start": 0, "end": 5}])
        sid2, rev2 = make_confirmed(
            client, "Alpha", "Alpha", [{"id": "a", "start": 0, "end": 5}])
        sid3, rev3 = make_confirmed(
            client, "alpha", "alpha", [{"id": "a", "start": 0, "end": 5}])
        body = register(client, [
            {"page": 1, "session_id": sid1, "revision": rev1},
            {"page": 2, "session_id": sid2, "revision": rev2},
            {"page": 3, "session_id": sid3, "revision": rev3},
        ]).json()
        terms = term_map(body)
        assert set(terms) == {"Alpha"}
        assert terms["Alpha"]["support"] == 2


# ---------------- 整批拒绝 ----------------

class TestBatchRejection:
    def _two_sessions(self, client):
        a = make_confirmed(client, "aa", "aa", [{"id": "x", "start": 0, "end": 2}])
        b = make_confirmed(client, "bb", "bb", [{"id": "y", "start": 0, "end": 2}])
        return a, b

    def test_duplicate_page_rejects_whole_batch(self, client):
        (s1, r1), (s2, r2) = self._two_sessions(client)
        resp = register(client, [
            {"page": 5, "session_id": s1, "revision": r1},
            {"page": 5, "session_id": s2, "revision": r2},
        ])
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "5" in [str(p) for p in detail["pages"]]
        assert any(e["code"] == "duplicate_pages" for e in detail["errors"])
        assert glossary(client)["pages"] == []

    def test_unconfirmed_revision_rejects_batch(self, client):
        (s1, r1), _ = self._two_sessions(client)
        r = client.post("/api/sessions", json={
            "original": "cc", "revised": "cc", "labels": [{"start": 0, "end": 2}]})
        unconfirmed_sid = r.json()["session_id"]
        unconfirmed_rev = r.json()["revision"]
        resp = register(client, [
            {"page": 1, "session_id": s1, "revision": r1},
            {"page": 2, "session_id": unconfirmed_sid, "revision": unconfirmed_rev},
        ])
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert [e["code"] for e in detail["errors"]] == ["revision_unconfirmed"]
        assert detail["errors"][0]["pages"] == [2]
        assert glossary(client)["pages"] == []      # 整批未写入

    def test_expired_revision_rejects_batch(self, client):
        (s1, r1), _ = self._two_sessions(client)
        # 让 s1 输入变化 -> 之前确认的版本失效
        client.put(f"/api/sessions/{s1}/inputs", json={
            "original": "aa", "revised": "aaa", "labels": [{"start": 0, "end": 2}]})
        resp = register(client, [
            {"page": 3, "session_id": s1, "revision": r1},
        ])
        assert resp.status_code == 409
        err = resp.json()["detail"]["errors"][0]
        assert err["code"] == "revision_expired"
        assert err["pages"] == [3] and err["current_revision"] == r1 + 1

    def test_unknown_session_rejects_batch(self, client):
        (s1, r1), _ = self._two_sessions(client)
        resp = register(client, [
            {"page": 1, "session_id": s1, "revision": r1},
            {"page": 2, "session_id": "nope", "revision": 1},
        ])
        assert resp.status_code == 409
        errs = resp.json()["detail"]["errors"]
        assert any(e["code"] == "session_not_found" and e["pages"] == [2]
                   for e in errs)
        assert glossary(client)["pages"] == []

    def test_session_multi_page_rejected_and_existing_kept(self, client):
        (s1, r1), (s2, r2) = self._two_sessions(client)
        assert register(client, [
            {"page": 1, "session_id": s1, "revision": r1}]).status_code == 200
        # 把同一会话再登记到页 2 -> 更新后一会话两页，整批拒绝
        resp = register(client, [
            {"page": 2, "session_id": s1, "revision": r1},
            {"page": 3, "session_id": s2, "revision": r2},
        ])
        assert resp.status_code == 409
        err = next(e for e in resp.json()["detail"]["errors"]
                   if e["code"] == "session_multi_page")
        assert err["pages"] == [1, 2]
        # 原登记保持不变：页 1 仍在，页 2/3 未写入
        pages = glossary(client)["pages"]
        assert [p["page"] for p in pages] == [1]
        assert pages[0]["session_id"] == s1

    def test_multi_page_conflict_can_be_resolved_by_reassigning(self, client):
        (s1, r1), (s2, r2) = self._two_sessions(client)
        register(client, [{"page": 1, "session_id": s1, "revision": r1}])
        # 同一批：页 2 -> s1，页 1 -> s2，则最终 s1 只在页 2、s2 只在页 1
        resp = register(client, [
            {"page": 2, "session_id": s1, "revision": r1},
            {"page": 1, "session_id": s2, "revision": r2},
        ])
        assert resp.status_code == 200, resp.text
        by_page = {p["page"]: p["session_id"] for p in resp.json()["pages"]}
        assert by_page == {1: s2, 2: s1}

    def test_capacity_limit(self, client):
        entries = []
        for page in range(1, 31):
            sid, rev = make_confirmed(
                client, f"t{page}", f"t{page}", [{"id": "x", "start": 0, "end": 2}])
            entries.append({"page": page, "session_id": sid, "revision": rev})
        assert register(client, entries).status_code == 200
        extra_sid, extra_rev = make_confirmed(
            client, "more", "more", [{"id": "x", "start": 0, "end": 4}])
        resp = register(client, [
            {"page": 31, "session_id": extra_sid, "revision": extra_rev}])
        assert resp.status_code == 409
        assert any(e["code"] == "capacity_exceeded"
                   for e in resp.json()["detail"]["errors"])
        assert len(glossary(client)["pages"]) == 30

    def test_invalid_page_does_not_trigger_spurious_multi_page(self, client):
        # 会话 s1 已在页 1；本批又把 s1 填进页 2，但版本过期。
        # 该页无效时不应再额外报“一会话多页”（其映射保持原登记）。
        (s1, r1), _ = self._two_sessions(client)
        register(client, [{"page": 1, "session_id": s1, "revision": r1}])
        client.put(f"/api/sessions/{s1}/inputs", json={
            "original": "aa", "revised": "ab", "labels": [{"start": 0, "end": 2}]})
        resp = register(client, [{"page": 2, "session_id": s1, "revision": r1}])
        assert resp.status_code == 409
        codes = {e["code"] for e in resp.json()["detail"]["errors"]}
        assert codes == {"revision_expired"}
        # 原登记不变
        assert [p["page"] for p in glossary(client)["pages"]] == [1]

    def test_malformed_entries_400(self, client):
        (s1, r1), _ = self._two_sessions(client)
        assert register(client, []).status_code == 400
        assert register(client, [{"page": 0, "session_id": s1,
                                  "revision": r1}]).status_code == 400
        assert register(client, [{"page": "1", "session_id": s1,
                                  "revision": r1}]).status_code == 400
        assert register(client, [{"page": 1, "session_id": s1,
                                  "revision": 0}]).status_code == 400
        huge = [{"page": i, "session_id": s1, "revision": r1}
                for i in range(1, 32)]
        assert register(client, huge).status_code == 400


# ---------------- 版本失效 -> 待补录 ----------------

class TestPending:
    def test_stale_page_leaves_counts_and_becomes_pending(self, client):
        s1, r1 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        s2, r2 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        register(client, [
            {"page": 1, "session_id": s1, "revision": r1},
            {"page": 2, "session_id": s2, "revision": r2},
        ])
        assert term_map(glossary(client))["abc"]["support"] == 2

        # 页 1 的会话被再次修改（版本失效，结果被删除）
        client.put(f"/api/sessions/{s1}/inputs", json={
            "original": "abc", "revised": "abcd", "labels": [{"start": 0, "end": 3}]})
        body = glossary(client)
        assert body["terms"] == []                      # 只剩 1 页支持
        pending = {p["page"]: p for p in body["pending"]}
        assert set(pending) == {1}
        assert pending[1]["reason"] == "stale_version"
        assert pending[1]["revision"] == r1
        assert pending[1]["current_revision"] == r1 + 1
        # 登记没有被删除
        assert 1 in [p["page"] for p in body["pages"]]

    def test_pending_page_can_be_replenished(self, client):
        s1, r1 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        s2, r2 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        register(client, [
            {"page": 1, "session_id": s1, "revision": r1},
            {"page": 2, "session_id": s2, "revision": r2},
        ])
        r = client.put(f"/api/sessions/{s1}/inputs", json={
            "original": "abc", "revised": "abc", "labels": [{"start": 0, "end": 3}]})
        new_rev = r.json()["revision"]
        assert client.post(
            f"/api/sessions/{s1}/confirm", json={"revision": new_rev}
        ).status_code == 200
        body = register(client, [
            {"page": 1, "session_id": s1, "revision": new_rev}]).json()
        assert body["pending"] == []
        assert term_map(body)["abc"]["support"] == 2
        ev_pages = {e["page"] for e in term_map(body)["abc"]["evidence"]}
        assert ev_pages == {1, 2}

    def test_pending_when_registered_version_unconfirmed(self, client):
        # 直接写库模拟“登记后版本尚未确认”（正常 API 不会接受这种登记）
        s1, r1 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        register(client, [{"page": 1, "session_id": s1, "revision": r1}])
        r = client.post("/api/sessions", json={
            "original": "abc", "revised": "abc", "labels": [{"start": 0, "end": 3}]})
        sid2, rev2 = r.json()["session_id"], r.json()["revision"]
        conn = sqlite3.connect(os.environ["DB_PATH"])
        conn.execute(
            "INSERT INTO glossary_pages(page, session_id, registered_revision,"
            " updated_at) VALUES (?,?,?,?)",
            (2, sid2, rev2, "2026-01-01T00:00:00+00:00"))
        conn.commit()
        conn.close()
        body = glossary(client)
        p2 = {p["page"]: p for p in body["pending"]}[2]
        assert p2["reason"] == "unconfirmed"
        assert term_map(body).get("abc") is None       # 页 2 不计入

    def test_pending_when_session_deleted(self, client):
        s1, r1 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        s2, r2 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        register(client, [
            {"page": 1, "session_id": s1, "revision": r1},
            {"page": 2, "session_id": s2, "revision": r2},
        ])
        conn = sqlite3.connect(os.environ["DB_PATH"])
        conn.execute("DELETE FROM sessions WHERE id=?", (s1,))
        conn.commit()
        conn.close()
        body = glossary(client)
        p1 = {p["page"]: p for p in body["pending"]}[1]
        assert p1["reason"] == "session_not_found"
        # 页 1 退出计数后只剩 1 页支持，不再进入词表
        assert "abc" not in term_map(body)
        assert [p["page"] for p in body["pages"]] == [1, 2]


# ---------------- 同一次已提交状态 ----------------

class TestSnapshot:
    def test_glossary_built_from_single_query(self, client):
        s1, r1 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        s2, r2 = make_confirmed(
            client, "abc", "abc", [{"id": "a", "start": 0, "end": 3}])
        register(client, [
            {"page": 1, "session_id": s1, "revision": r1},
            {"page": 2, "session_id": s2, "revision": r2},
        ])

        from app import db
        executed = []
        real_connect = db.connect

        class Recording:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                executed.append(sql)
                return self._conn.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def fake_connect():
            return Recording(real_connect())

        db.connect = fake_connect
        try:
            body = db.get_glossary()
        finally:
            db.connect = real_connect

        selects = [s for s in executed if s.strip().upper().startswith("SELECT")]
        assert len(selects) == 1                    # 词表与证据来自同一条快照查询
        assert body["pending"] == []
        assert term_map(body)["abc"]["support"] == 2

    def test_atomic_batch_write(self, client, monkeypatch):
        """批内写入中途失败时，已写入的行回滚，原登记不受影响。"""
        (s1, r1) = make_confirmed(
            client, "aa", "aa", [{"id": "x", "start": 0, "end": 2}])
        (s2, r2) = make_confirmed(
            client, "bb", "bb", [{"id": "y", "start": 0, "end": 2}])
        (s3, r3) = make_confirmed(
            client, "cc", "cc", [{"id": "z", "start": 0, "end": 2}])
        register(client, [{"page": 1, "session_id": s1, "revision": r1}])

        from app import db
        real_connect = db.connect

        def partial_failure():
            conn = real_connect()
            real_execute = conn.execute

            def execute(sql, *args):
                if sql.lstrip().startswith("INSERT INTO glossary_pages") \
                        and args:
                    page = args[0] if isinstance(args[0], int) else args[0][0]
                    if page == 3:
                        raise sqlite3.OperationalError("disk I/O error")
                return real_execute(sql, *args)

            class Wrapper:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, *a):
                    return execute(sql, *a)

            return Wrapper()

        db.connect = partial_failure
        try:
            with pytest.raises(sqlite3.OperationalError):
                db.register_glossary_pages([
                    {"page": 2, "session_id": s2, "revision": r2},
                    {"page": 3, "session_id": s3, "revision": r3},
                ])
        finally:
            db.connect = real_connect

        body = glossary(client)
        # 页 2 的 INSERT 先执行成功，但页 3 失败导致整个事务回滚
        assert [p["page"] for p in body["pages"]] == [1]
