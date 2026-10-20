"""写真ごとの切り出し3件。

書き出しに成功したときの自動記録（新しい順に3件、ロック1件は残す）と、
枠のロック・メモ更新。呼び出し側がトランザクションを commit する。
"""
from __future__ import annotations

import sqlite3

from .fts import update_fts

REGION_COLUMNS = "slot, x, y, w, h, src_w, src_h, memo, locked, updated_at"
# 再書き出しを同じ記録とみなすゆらぎ。1px 未満の差で別件にしない程度。
_MATCH_EPS = 1e-4


def regions_match(row: sqlite3.Row | dict, region: dict, eps: float = _MATCH_EPS) -> bool:
    """同じ作業画像サイズで、枠の位置と大きさが揃っている。"""
    if int(row["src_w"]) != int(region["src_w"]) or int(row["src_h"]) != int(region["src_h"]):
        return False
    return all(abs(float(row[k]) - float(region[k])) <= eps for k in ("x", "y", "w", "h"))


def plan_auto_record(rows: list[sqlite3.Row | dict], region: dict) -> dict:
    """自動記録の次の一手。

    touch: 同じ枠がロックなしである。時刻だけ新しくする。
    skip: 同じ枠はロック中だけ、またはロック以外の空きが無い。
    insert: 空いている番号へ足す。
    replace: ロックしていない一番古い枠を、今回の範囲で上書きする。
    """
    matches = [r for r in rows if regions_match(r, region)]
    unlocked_matches = [r for r in matches if not r["locked"]]
    if unlocked_matches:
        target = max(unlocked_matches, key=lambda r: (r["updated_at"] or "", r["slot"]))
        return {"op": "touch", "slot": int(target["slot"])}
    if matches:
        return {"op": "skip", "slot": None}
    used = {int(r["slot"]) for r in rows}
    empty = [s for s in (1, 2, 3) if s not in used]
    if empty:
        return {"op": "insert", "slot": empty[0]}
    candidates = [r for r in rows if not r["locked"]]
    if not candidates:
        return {"op": "skip", "slot": None}
    oldest = min(candidates, key=lambda r: (r["updated_at"] or "", r["slot"]))
    return {"op": "replace", "slot": int(oldest["slot"])}


def _rows(db: sqlite3.Connection, photo_id: int) -> list[sqlite3.Row]:
    return db.execute(
        f"SELECT {REGION_COLUMNS} FROM photo_export_regions WHERE photo_id=? ORDER BY slot",
        (photo_id,),
    ).fetchall()


def _one(db: sqlite3.Connection, photo_id: int, slot: int) -> sqlite3.Row | None:
    return db.execute(
        f"SELECT {REGION_COLUMNS} FROM photo_export_regions WHERE photo_id=? AND slot=?",
        (photo_id, slot),
    ).fetchone()


def apply_auto_record(db: sqlite3.Connection, photo_id: int, region: dict) -> str:
    """設定がオンのとき、書き出し成功後に呼ぶ。commit はしない。"""
    rows = _rows(db, photo_id)
    plan = plan_auto_record(rows, region)
    op, slot = plan["op"], plan["slot"]
    if op == "skip":
        return "skip"
    if op == "touch":
        db.execute(
            """UPDATE photo_export_regions SET updated_at=datetime('now')
               WHERE photo_id=? AND slot=? AND locked=0""",
            (photo_id, slot),
        )
        return "touch"
    if op == "insert":
        db.execute(
            """INSERT INTO photo_export_regions
                 (photo_id, slot, x, y, w, h, src_w, src_h, memo, locked, updated_at)
               VALUES (?,?,?,?,?,?,?,?, '', 0, datetime('now'))""",
            (photo_id, slot, region["x"], region["y"], region["w"], region["h"],
             region["src_w"], region["src_h"]),
        )
        update_fts(db, photo_id)
        return "insert"
    db.execute(
        """UPDATE photo_export_regions
             SET x=?, y=?, w=?, h=?, src_w=?, src_h=?, memo='', updated_at=datetime('now')
           WHERE photo_id=? AND slot=? AND locked=0""",
        (region["x"], region["y"], region["w"], region["h"],
         region["src_w"], region["src_h"], photo_id, slot),
    )
    update_fts(db, photo_id)
    return "replace"


def region_is_locked(db: sqlite3.Connection, photo_id: int, slot: int) -> bool:
    row = db.execute(
        "SELECT locked FROM photo_export_regions WHERE photo_id=? AND slot=?",
        (photo_id, slot),
    ).fetchone()
    return bool(row and row["locked"])


def set_region_lock(db: sqlite3.Connection, photo_id: int, slot: int, locked: bool) -> sqlite3.Row | None:
    """1写真につきロックは1件。新しいロックを付けると、他の枠のロックは外れる。"""
    if _one(db, photo_id, slot) is None:
        return None
    if locked:
        db.execute(
            "UPDATE photo_export_regions SET locked=0 WHERE photo_id=? AND slot!=?",
            (photo_id, slot),
        )
    db.execute(
        "UPDATE photo_export_regions SET locked=? WHERE photo_id=? AND slot=?",
        (1 if locked else 0, photo_id, slot),
    )
    return _one(db, photo_id, slot)


def update_region_memo(db: sqlite3.Connection, photo_id: int, slot: int, memo: str) -> sqlite3.Row | None:
    """メモだけ変える。枠の時刻は動かさない（自動記録の古さ判定に使っているため）。"""
    if _one(db, photo_id, slot) is None:
        return None
    db.execute(
        "UPDATE photo_export_regions SET memo=? WHERE photo_id=? AND slot=?",
        (memo, photo_id, slot),
    )
    update_fts(db, photo_id)
    return _one(db, photo_id, slot)
