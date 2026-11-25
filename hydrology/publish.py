"""发布服务：量程 / 分区表单独或一并发布。

交错提交的核心规则
------------------
每笔发布在 ``BEGIN IMMEDIATE`` 事务内：

1. 读取 **届时生效** 的现行量程版本与分区表版本；
2. 确定本次待核口径：
   * ``ranges``：新量程 + 现行分区表（用另一项届时生效的版本复核）；
   * ``rules``：现行量程 + 新分区表；
   * ``bundle``：新量程 + 新分区表（成对复核）；
3. 跑坐标压缩核对；
4. 通过才写入新版本并移动 ``meta`` 现行指针；不通过则现行指针原样保留，
   只落一条 ``accepted=0`` 的核对记录。

读请求从不使用候选数据，因此核对期间网页查到的始终是已提交的现行归属。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from .db import connect, immediate, load_current, now_iso
from .domain import RANGE_KEYS, parse_range, parse_rules, verify

VALID_KINDS = ("ranges", "rules", "bundle")

KIND_LABELS = {
    "ranges": "仅量程修订",
    "rules": "仅分区表修订",
    "bundle": "量程 + 分区表一并发布",
    "seed": "初始播种",
}

# 候选稿格式版本。旧稿没有这个字段：改版前网页表单里量程与规则行共用
# ``wl_min`` 等字段名，提交时每行区间会整体串到下一行，最后一行区间丢失。
CANDIDATE_SCHEMA = 2

# 载回旧版网页稿时的说明。
LEGACY_WEB_RULES_WARNING = (
    "这是一份旧版网页稿：提交时每行区间曾整体串到下一行。"
    "能确认归属的区间已回到对应行；最后一行区间在旧稿中没有保存下来，已留空，"
    "请补填后再提交。"
)
MISSING_SECTION_WARNING = "该稿没有保存这一部分内容，已留空，请补填后再提交。"

# 规则编辑器中参与“缺项”判定的字段（按页面展示顺序）。
EDITOR_RULE_KEYS = ("zone", "priority") + RANGE_KEYS


def build_candidate(
    kind: str,
    range_data: Optional[Mapping[str, Any]],
    rules_data: Any,
) -> dict[str, Any]:
    """构造留档候选稿，只保存本次提交动作实际送审的部分。"""
    candidate: dict[str, Any] = {
        "candidate_schema": CANDIDATE_SCHEMA,
        "kind": kind,
    }
    if kind in ("ranges", "bundle"):
        candidate["range"] = range_data
    if kind in ("rules", "bundle"):
        candidate["rules"] = rules_data
    return candidate


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _normalise_rules(raw_rules: Any) -> list[dict[str, Any]]:
    """把留档的规则列表整理成编辑器行（字段缺失保留为空）。"""
    rows: list[dict[str, Any]] = []
    if not isinstance(raw_rules, (list, tuple)):
        return rows
    for item in raw_rules:
        if not isinstance(item, Mapping):
            rows.append({key: None for key in ("id",) + EDITOR_RULE_KEYS})
            continue
        row: dict[str, Any] = {"id": item.get("id", item.get("rule_id"))}
        for key in EDITOR_RULE_KEYS:
            row[key] = item.get(key)
        rows.append(row)
    return rows


def _restore_legacy_web_rules(raw_rules: Any) -> list[dict[str, Any]]:
    """还原旧版网页稿的规则区间。

    旧表单中量程编辑器与规则行共用 ``wl_min`` 等字段名，浏览器按文档顺序
    提交同名值，于是留档的第 ``i`` 行（``i >= 1``）拿到的是原第 ``i-1`` 行
    的区间，原第 0 行拿到的是量程值，原末行的区间溢出丢失。这里把能确认
    归属的区间下移回对应行；原末行区间无法找回，留空待补填。
    """
    rows = _normalise_rules(raw_rules)
    restored: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        item: dict[str, Any] = {
            "id": row.get("id"),
            "zone": row.get("zone"),
            "priority": row.get("priority"),
        }
        if idx + 1 < len(rows):
            source = rows[idx + 1]
            for key in RANGE_KEYS:
                item[key] = source.get(key)
        else:
            # 原末行的区间在旧稿中从未被保存。
            for key in RANGE_KEYS:
                item[key] = ""
        restored.append(item)
    return restored


def _incomplete_rows(rules: list[Mapping[str, Any]]) -> list[int]:
    """返回存在缺项的规则行号（从 1 开始）。"""
    return [
        idx
        for idx, row in enumerate(rules, start=1)
        if any(_is_blank(row.get(key)) for key in EDITOR_RULE_KEYS)
    ]


def build_restore_draft(attempt: Mapping[str, Any]) -> dict[str, Any]:
    """把一笔被驳回稿转成编辑器初值（待编辑副本，不写库）。

    * 只带回原提交动作实际送审的部分（量程 / 分区表）；
    * 新版网页稿与 API 稿按原值载回；
    * 旧版网页稿按旧表单的错行方式还原，丢失区间留空并提示补填。
    """
    draft: dict[str, Any] = {
        "kind": attempt.get("kind"),
        "range": None,
        "rules": None,
        "warnings": [],
        "missing_rows": [],
        "legacy_web": False,
        "range_missing": False,
    }

    data: dict[str, Any] = {}
    candidate_json = attempt.get("candidate_json")
    if candidate_json:
        try:
            loaded = json.loads(candidate_json)
        except (ValueError, TypeError):
            loaded = None
        if isinstance(loaded, dict):
            data = loaded

    kind = attempt.get("kind")
    schema = data.get("candidate_schema")
    is_legacy_web = schema is None and attempt.get("source") == "web"

    if kind in ("ranges", "bundle"):
        raw_range = data.get("range")
        if isinstance(raw_range, Mapping):
            draft["range"] = {key: raw_range.get(key) for key in RANGE_KEYS}
        else:
            draft["range"] = {key: "" for key in RANGE_KEYS}
            draft["warnings"].append(MISSING_SECTION_WARNING)
        draft["range_missing"] = any(
            _is_blank(draft["range"].get(key)) for key in RANGE_KEYS
        )

    if kind in ("rules", "bundle"):
        raw_rules = data.get("rules")
        if not isinstance(raw_rules, (list, tuple)):
            draft["rules"] = []
            draft["warnings"].append(MISSING_SECTION_WARNING)
        elif is_legacy_web:
            draft["rules"] = _restore_legacy_web_rules(raw_rules)
            draft["legacy_web"] = True
            draft["warnings"].append(LEGACY_WEB_RULES_WARNING)
        else:
            draft["rules"] = _normalise_rules(raw_rules)
        draft["missing_rows"] = _incomplete_rows(draft["rules"])

    return draft


def submit_publish(
    db_path: str,
    *,
    kind: str,
    range_data: Optional[Mapping[str, Any]] = None,
    rules_data: Any = None,
    note: str = "",
    source: str = "web",
) -> dict[str, Any]:
    """提交一笔发布。永远返回结构化结果，核对失败不抛异常。"""
    if kind not in VALID_KINDS:
        raise ValueError(f"未知发布类型: {kind!r}，应为 {VALID_KINDS}")

    note = (note or "").strip()[:200]
    candidate_json = json.dumps(
        build_candidate(kind, range_data, rules_data),
        ensure_ascii=False,
        default=str,
    )

    # ---- 1) 解析候选（事务外做纯计算，错误也落成 rejected 记录） ----------
    parse_errors: list[str] = []
    candidate_range = None
    candidate_rules = None

    if kind in ("ranges", "bundle"):
        candidate_range, errs = parse_range(range_data)
        parse_errors.extend(errs)
    if kind in ("rules", "bundle"):
        candidate_rules, errs = parse_rules(rules_data)
        parse_errors.extend(errs)

    conn = connect(db_path)
    try:
        with immediate(conn):
            current = load_current(conn)
            cur_range = current["range"]
            cur_rules = current["rules"]
            base_range_id = current["range_id"]
            base_rules_id = current["rules_id"]

            # 初始口径必须存在（init_database 已播种），否则无法交叉复核。
            if cur_range is None or not cur_rules:
                parse_errors.append("现行口径尚未初始化，无法复核发布")

            if parse_errors:
                report = {
                    "ok": False,
                    "structural_errors": parse_errors,
                    "warnings": [],
                    "gap": None,
                    "tie": None,
                }
                attempt_id = _insert_attempt(
                    conn,
                    kind=kind,
                    source=source,
                    accepted=False,
                    note=note,
                    range_id=None,
                    rules_id=None,
                    base_range_id=base_range_id,
                    base_rules_id=base_rules_id,
                    report=report,
                    candidate_json=candidate_json,
                )
                return {
                    "attempt_id": attempt_id,
                    "accepted": False,
                    "report": report,
                    "current_range_id": base_range_id,
                    "current_rules_id": base_rules_id,
                    "new_range_id": None,
                    "new_rules_id": None,
                }

            # ---- 2) 用“届时生效”的另一项版本拼出待核口径 ----------------
            if kind == "ranges":
                check_range = candidate_range
                check_rules = cur_rules
                checked_range_id = None  # 候选，尚未入库
                checked_rules_id = base_rules_id
            elif kind == "rules":
                check_range = cur_range
                check_rules = candidate_rules
                checked_range_id = base_range_id
                checked_rules_id = None
            else:  # bundle
                check_range = candidate_range
                check_rules = candidate_rules
                checked_range_id = None
                checked_rules_id = None

            report = verify(check_range, check_rules)
            report["checked_against"] = {
                "range_version_id": checked_range_id,
                "rules_version_id": checked_rules_id,
                "note": (
                    "单独发布的一类，已与另一项届时生效的现行版本交叉复核"
                    if kind != "bundle"
                    else "量程与分区表成对复核"
                ),
            }

            # ---- 3a) 核对失败：只记录，不动现行指针 -----------------------
            if not report["ok"]:
                attempt_id = _insert_attempt(
                    conn,
                    kind=kind,
                    source=source,
                    accepted=False,
                    note=note,
                    range_id=None,
                    rules_id=None,
                    base_range_id=base_range_id,
                    base_rules_id=base_rules_id,
                    report=report,
                    candidate_json=candidate_json,
                )
                return {
                    "attempt_id": attempt_id,
                    "accepted": False,
                    "report": report,
                    "current_range_id": base_range_id,
                    "current_rules_id": base_rules_id,
                    "new_range_id": None,
                    "new_rules_id": None,
                }

            # ---- 3b) 通过：写新版本并移动现行指针 -------------------------
            ts = now_iso()
            new_range_id = base_range_id
            new_rules_id = base_rules_id

            if kind in ("ranges", "bundle"):
                cur = conn.execute(
                    """INSERT INTO range_versions
                       (wl_min, wl_max, fl_min, fl_max, note, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        candidate_range.wl_min,
                        candidate_range.wl_max,
                        candidate_range.fl_min,
                        candidate_range.fl_max,
                        note or f"{KIND_LABELS[kind]}",
                        ts,
                    ),
                )
                new_range_id = cur.lastrowid

            if kind in ("rules", "bundle"):
                cur = conn.execute(
                    "INSERT INTO rules_versions (note, created_at) VALUES (?, ?)",
                    (note or KIND_LABELS[kind], ts),
                )
                new_rules_id = cur.lastrowid
                for r in candidate_rules:
                    conn.execute(
                        """INSERT INTO rules
                           (version_id, ord, zone, priority,
                            wl_min, wl_max, fl_min, fl_max)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            new_rules_id,
                            r.ord if r.ord is not None else 0,
                            r.zone,
                            r.priority,
                            r.wl_min,
                            r.wl_max,
                            r.fl_min,
                            r.fl_max,
                        ),
                    )

            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('current_range_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_range_id),),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('current_rules_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(new_rules_id),),
            )

            attempt_id = _insert_attempt(
                conn,
                kind=kind,
                source=source,
                accepted=True,
                note=note,
                range_id=new_range_id,
                rules_id=new_rules_id,
                base_range_id=base_range_id,
                base_rules_id=base_rules_id,
                report=report,
                candidate_json=candidate_json,
            )
            return {
                "attempt_id": attempt_id,
                "accepted": True,
                "report": report,
                "current_range_id": new_range_id,
                "current_rules_id": new_rules_id,
                "new_range_id": new_range_id,
                "new_rules_id": new_rules_id,
            }
    finally:
        conn.close()


def _insert_attempt(
    conn,
    *,
    kind: str,
    source: str,
    accepted: bool,
    note: str,
    range_id: Optional[int],
    rules_id: Optional[int],
    base_range_id: Optional[int],
    base_rules_id: Optional[int],
    report: dict[str, Any],
    candidate_json: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO publish_attempts
           (created_at, kind, source, accepted, note,
            range_id, rules_id, base_range_id, base_rules_id,
            report_json, candidate_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now_iso(),
            kind,
            source,
            1 if accepted else 0,
            note,
            range_id,
            rules_id,
            base_range_id,
            base_rules_id,
            json.dumps(report, ensure_ascii=False),
            candidate_json,
        ),
    )
    return cur.lastrowid
