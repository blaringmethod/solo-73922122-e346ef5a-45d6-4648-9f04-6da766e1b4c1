"""候选词表：登记页的聚合逻辑。

设计要点：
- 登记以“扫描页号”为主键；同一页号再次提交即更新。
- 词条按确认结果里标注的“原样文字”汇总，不做任何归一化。
- 支持页数按**不同页号**计数：同一页出现多次只算一页。
- 只有支持页数 >= MIN_TERM_SUPPORT 的词条才进入候选词表。
- 页的登记版本一旦失效（会话当前版本已变 / 结果已不在 / 会话不存在），
  该页退出计数并进入待补录列表。

本模块只做纯计算；数据库读写与批次事务在 db.py 中。
"""

from __future__ import annotations

import json
from collections import defaultdict

MAX_GLOSSARY_PAGES = 30
MIN_TERM_SUPPORT = 2

PENDING_REASONS = {
    "stale_version": "登记版本已过期，需用当前已确认版本重新补录",
    "unconfirmed": "登记版本尚未确认，需确认后补录",
    "session_not_found": "会话不存在，需重新补录",
    "result_unreadable": "确认结果无法读取，需重新补录",
}


class GlossaryBatchError(Exception):
    """整批登记被拒绝；detail 直接作为 409 的响应体。"""

    def __init__(self, detail: dict):
        super().__init__(detail.get("message", "整批登记被拒绝"))
        self.detail = detail


def _page_entry(row: dict) -> tuple[dict, dict | None]:
    """把一行 join 结果转成页面状态；有效页额外返回解析后的确认结果。"""
    page = {
        "page": row["page"],
        "session_id": row["session_id"],
        "revision": row["registered_revision"],
        "current_revision": row["current_revision"],
        "updated_at": row["updated_at"],
    }

    if row["current_revision"] is None:
        reason = "session_not_found"
    elif row["current_revision"] != row["registered_revision"]:
        reason = "stale_version"
    elif row["result_data"] is None:
        reason = "unconfirmed"
    else:
        reason = None

    result = None
    if reason is None:
        try:
            result = json.loads(row["result_data"])
            if not isinstance(result, dict) or not isinstance(
                    result.get("labels"), list):
                raise ValueError
        except (TypeError, ValueError):
            reason = "result_unreadable"
            result = None

    page["status"] = "active" if reason is None else "pending"
    page["reason"] = reason
    page["message"] = PENDING_REASONS.get(reason, "")
    # 证据上下文用确认结果里的修订文，和证据区间同源
    page["revised"] = result.get("revised") if result is not None else None
    return page, result


def build_glossary_view(rows: list[dict]) -> dict:
    """由同一次读取到的登记行构造词表、证据和待补录页。

    rows 必须来自同一条 SQL 查询（db.read_glossary_rows），从而保证
    词表、证据与待补录状态反映同一个已提交快照。
    """
    pages: list[dict] = []
    # text -> {page: [span, ...]}；同一页内的同文多次标注合并进同一页。
    evidence_by_text: dict[str, dict[int, list[dict]]] = defaultdict(
        lambda: defaultdict(list))

    for row in rows:
        page, result = _page_entry(row)
        pages.append(page)
        if result is None:
            continue
        for label in result["labels"]:
            if not isinstance(label, dict):
                continue
            text = label.get("text")
            start, end = label.get("start"), label.get("end")
            revised = result.get("revised")
            if not isinstance(text, str) or text == "":
                continue
            if not (isinstance(start, int) and isinstance(end, int)
                    and 0 <= start < end):
                continue
            # 标注区间必须能在这份修订文里原样落回，否则跳过这条证据
            if not isinstance(revised, str) or revised[start:end] != text:
                continue
            evidence_by_text[text][page["page"]].append({
                "page": page["page"],
                "session_id": page["session_id"],
                "revision": page["revision"],
                "label_id": label.get("id"),
                "start": start,
                "end": end,
            })

    terms = []
    for text, by_page in evidence_by_text.items():
        support = len(by_page)
        if support < MIN_TERM_SUPPORT:
            continue
        evidence = []
        for p in sorted(by_page):
            evidence.extend(sorted(by_page[p], key=lambda s: (s["start"], s["end"])))
        terms.append({
            "text": text,
            "support": support,
            "pages": sorted(by_page),
            "evidence": evidence,
        })
    terms.sort(key=lambda t: (-t["support"], t["text"]))

    # 前端展示证据上下文用的修订文；不在 API 顶层暴露整段修订文，
    # 而是附在每条证据上，避免把未登记的会话内容混进来。
    revised_by_page = {p["page"]: p.pop("revised") for p in pages}
    for term in terms:
        for ev in term["evidence"]:
            ev["revised"] = revised_by_page.get(ev["page"])

    pending = [p for p in pages if p["status"] == "pending"]
    return {
        "max_pages": MAX_GLOSSARY_PAGES,
        "min_support": MIN_TERM_SUPPORT,
        "page_count": len(pages),
        "pages": pages,
        "terms": terms,
        "pending": pending,
    }
