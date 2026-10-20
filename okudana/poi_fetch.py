"""OSM POI データの取得・管理 (tools/build_poi_db.py と共有)。

- 1地域（ISO 3166-2、OSM admin_level=4）ずつ Overpass API から取得する。
  日本の保存キーは従来どおり都道府県名。国外は ISO コードがキーで、
  画面上の名前は poi_meta.label。
- 国全体（ISO 3166-1、admin_level=2）は取得しない。理由は
  docs/data-schema.md の POI 節。UI にも「この国すべて」は置かない。
- カスタム地点の手動追加/削除
- 名前検索（オートコンプリート・管理画面用）

data/poi.db は完全にローカル。取得後はオフラインで poi_lookup (geo.py) が使う。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .countries import COUNTRIES
from .paths import data_dir

log = logging.getLogger("okudana.poi_fetch")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "Okudana-personal/0.1 (local photo indexer)"}

PREF_ISO = {
    "北海道": "JP-01", "青森県": "JP-02", "岩手県": "JP-03", "宮城県": "JP-04",
    "秋田県": "JP-05", "山形県": "JP-06", "福島県": "JP-07", "茨城県": "JP-08",
    "栃木県": "JP-09", "群馬県": "JP-10", "埼玉県": "JP-11", "千葉県": "JP-12",
    "東京都": "JP-13", "神奈川県": "JP-14", "新潟県": "JP-15", "富山県": "JP-16",
    "石川県": "JP-17", "福井県": "JP-18", "山梨県": "JP-19", "長野県": "JP-20",
    "岐阜県": "JP-21", "静岡県": "JP-22", "愛知県": "JP-23", "三重県": "JP-24",
    "滋賀県": "JP-25", "京都府": "JP-26", "大阪府": "JP-27", "兵庫県": "JP-28",
    "奈良県": "JP-29", "和歌山県": "JP-30", "鳥取県": "JP-31", "島根県": "JP-32",
    "岡山県": "JP-33", "広島県": "JP-34", "山口県": "JP-35", "徳島県": "JP-36",
    "香川県": "JP-37", "愛媛県": "JP-38", "高知県": "JP-39", "福岡県": "JP-40",
    "佐賀県": "JP-41", "長崎県": "JP-42", "熊本県": "JP-43", "大分県": "JP-44",
    "宮崎県": "JP-45", "鹿児島県": "JP-46", "沖縄県": "JP-47",
}
ISO_TO_PREF = {iso: name for name, iso in PREF_ISO.items()}

# 国全体（ISO 3166-1 / admin_level=2）は取得しない。
# 1つの都道府県でも Overpass は数十秒〜数分、件数は数千〜約2万（timeout 300秒）。
# 日本全体やアメリカ、中国を1クエリにすると制限時間を超え、poi.db も肥大する。
# 取得単位は都道府県と同じ admin_level=4 の1地域だけ。国コード（"JP" や "US"）は拒否する。
# 大きい地域（イングランド等）が時間切れになっても、国全体へ広げたり細分したりしない。
REGION_ISO_RE = re.compile(r"^[A-Z]{2}-[A-Z0-9]{1,3}$")
_COUNTRY_ONLY_RE = re.compile(r"^[A-Z]{2}$")

# Overpass の抽出条件。「撮影地になりうる名前付き地点」に絞る
POI_SELECTORS = [
    '["tourism"]',                        # 観光地・展望台・美術館など
    '["historic"]',                       # 史跡・城・記念碑
    '["leisure"~"^(park|garden|nature_reserve|beach_resort|bird_hide)$"]',
    '["amenity"~"^(place_of_worship|aquarium|zoo|theatre|arts_centre|marketplace|university)$"]',
    '["natural"~"^(peak|beach|cape|spring|wetland|wood)$"]',
    '["man_made"~"^(tower|lighthouse|bridge|pier)$"]',
    '["railway"="station"]',
    '["waterway"~"^(waterfall|dam)$"]',
    '["boundary"="national_park"]',
]

POI_DB = data_dir() / "poi.db"  # geo.py はここから import する (単一の定義元)


def open_poi_db(path: Path = POI_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout=15000")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pois (
            id       INTEGER PRIMARY KEY,
            osm_id   TEXT NOT NULL,          -- 'node/123' 'way/456' / 手動は 'manual/<uuid>'
            name     TEXT NOT NULL,
            kind     TEXT NOT NULL,          -- tourism=viewpoint 等の代表タグ / 手動は 'manual'
            pref     TEXT NOT NULL,          -- 取得単位 (入れ替え更新のキー) / 手動は 'manual'
            lat REAL NOT NULL, lon REAL NOT NULL,
            UNIQUE (pref, osm_id)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS poi_rtree USING rtree(
            id, min_lat, max_lat, min_lon, max_lon
        );
        CREATE TABLE IF NOT EXISTS poi_meta (
            pref TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            count INTEGER NOT NULL,
            label TEXT                 -- 表示名。日本は NULL（pref が都道府県名）。国外は「国 / 地域」
        );
    """)
    ensure_poi_schema(db)
    return db


def ensure_poi_schema(db: sqlite3.Connection) -> None:
    """既存の poi.db に poi_meta.label を足す。新しい CREATE だけでは旧ファイルは変わらない。"""
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='poi_meta'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in db.execute("PRAGMA table_info(poi_meta)")}
    if "label" not in cols:
        db.execute("ALTER TABLE poi_meta ADD COLUMN label TEXT")
        db.commit()


def _overpass(query: str, *, attempts: int, pause_s: float, http_timeout: int) -> dict:
    body = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(OVERPASS_URL, data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                return json.load(r)
        except Exception as e:
            last = e
            if attempt == attempts - 1:
                raise
            wait = pause_s * (attempt + 1)
            log.warning("overpass retry in %ds (%s)", wait, e)
            time.sleep(wait)
    raise last  # pragma: no cover


def fetch_by_iso(iso: str) -> list[dict]:
    """Overpass API から ISO 3166-2 の1地域分を取得する（数十秒〜数分）。

    国コードだけの指定はここで拒否する。クエリ文字列へ埋め込む前に
    REGION_ISO_RE で固定し、地域コード以外が area 条件に入らないようにする。
    """
    iso = iso.strip().upper()
    if _COUNTRY_ONLY_RE.fullmatch(iso):
        raise ValueError("国全体では取得できません。国の中の地域を指定してください")
    if not REGION_ISO_RE.fullmatch(iso):
        raise ValueError(f"invalid region code: {iso}")
    selectors = "\n".join(
        f'  nwr(area.a)["name"]{sel};' for sel in POI_SELECTORS)
    query = f"""[out:json][timeout:300];
area["ISO3166-2"="{iso}"][admin_level=4]->.a;
(
{selectors}
);
out center tags;"""
    data = _overpass(query, attempts=3, pause_s=30, http_timeout=360)
    return _pois_from_elements(data.get("elements") or [])


def fetch_pref(pref: str) -> list[dict]:
    """日本の都道府県名で1地域を取得する。保存キーは呼び出した側が pref のまま使う。"""
    return fetch_by_iso(PREF_ISO[pref])


def _pois_from_elements(elements: list) -> list[dict]:
    out = []
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name or len(name) > 60:
            continue
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None:
            continue
        kind = next(
            (f"{k}={tags[k]}" for k in
             ("tourism", "historic", "leisure", "amenity", "natural",
              "man_made", "railway", "waterway", "boundary") if k in tags),
            "unknown")
        out.append({"osm_id": f"{el['type']}/{el['id']}", "name": name,
                    "kind": kind, "lat": lat, "lon": lon})
    return out


def store_pref(db: sqlite3.Connection, pref: str, pois: list[dict],
               label: str | None = None) -> None:
    """1地域を丸ごと入れ替える。再実行がそのまま更新になる。

    label が None の再取得では、既に入っている表示名を消さない
    （日本の都道府県は label 無しのまま）。
    """
    old = [r["id"] for r in db.execute("SELECT id FROM pois WHERE pref=?", (pref,))]
    if old:
        db.executemany("DELETE FROM poi_rtree WHERE id=?", [(i,) for i in old])
        db.execute("DELETE FROM pois WHERE pref=?", (pref,))
    for p in pois:
        cur = db.execute(
            "INSERT OR IGNORE INTO pois (osm_id, name, kind, pref, lat, lon) "
            "VALUES (?,?,?,?,?,?)",
            (p["osm_id"], p["name"], p["kind"], pref, p["lat"], p["lon"]))
        if cur.lastrowid:
            db.execute("INSERT INTO poi_rtree VALUES (?,?,?,?,?)",
                       (cur.lastrowid, p["lat"], p["lat"], p["lon"], p["lon"]))
    db.execute(
        "INSERT INTO poi_meta (pref, fetched_at, count, label) VALUES (?,?,?,?) "
        "ON CONFLICT(pref) DO UPDATE SET fetched_at=excluded.fetched_at, "
        "count=excluded.count, label=COALESCE(excluded.label, poi_meta.label)",
        (pref, datetime.now(timezone.utc).isoformat(), len(pois), label))
    db.commit()


def delete_pref(db: sqlite3.Connection, pref: str) -> int:
    """指定都道府県のPOIデータを削除する。削除件数を返す。"""
    ids = [r["id"] for r in db.execute("SELECT id FROM pois WHERE pref=?", (pref,))]
    if ids:
        db.executemany("DELETE FROM poi_rtree WHERE id=?", [(i,) for i in ids])
        db.execute("DELETE FROM pois WHERE pref=?", (pref,))
    db.execute("DELETE FROM poi_meta WHERE pref=?", (pref,))
    db.commit()
    return len(ids)


def add_manual_poi(db: sqlite3.Connection, name: str, lat: float, lon: float) -> int:
    """ユーザ手動追加の地点。pref='manual' として保存し、既存の近傍検索に自動的に乗る。"""
    osm_id = f"manual/{uuid.uuid4().hex[:12]}"
    cur = db.execute(
        "INSERT INTO pois (osm_id, name, kind, pref, lat, lon) VALUES (?,?,?,?,?,?)",
        (osm_id, name.strip(), "manual", "manual", lat, lon))
    db.execute("INSERT INTO poi_rtree VALUES (?,?,?,?,?)",
              (cur.lastrowid, lat, lat, lon, lon))
    db.execute(
        "INSERT INTO poi_meta (pref, fetched_at, count) VALUES ('manual', ?, "
        "  (SELECT count(*) FROM pois WHERE pref='manual')) "
        "ON CONFLICT(pref) DO UPDATE SET fetched_at=excluded.fetched_at, "
        "  count=(SELECT count(*) FROM pois WHERE pref='manual')",
        (datetime.now(timezone.utc).isoformat(),))
    db.commit()
    return cur.lastrowid


def delete_poi(db: sqlite3.Connection, poi_id: int) -> bool:
    row = db.execute("SELECT pref FROM pois WHERE id=?", (poi_id,)).fetchone()
    if not row:
        return False
    db.execute("DELETE FROM poi_rtree WHERE id=?", (poi_id,))
    db.execute("DELETE FROM pois WHERE id=?", (poi_id,))
    if row["pref"] == "manual":
        db.execute(
            "UPDATE poi_meta SET count=(SELECT count(*) FROM pois WHERE pref='manual') "
            "WHERE pref='manual'")
    db.commit()
    return True


def search_pois(db: sqlite3.Connection, q: str, limit: int = 20) -> list[dict]:
    rows = db.execute(
        "SELECT id, name, kind, pref, lat, lon FROM pois WHERE name LIKE ? "
        "ORDER BY name LIMIT ?", (f"%{q}%", limit)).fetchall()
    return [dict(r) for r in rows]


def poi_status(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT * FROM poi_meta ORDER BY pref").fetchall()
    total = db.execute("SELECT count(*) FROM pois").fetchone()[0]
    return {"available": bool(rows), "prefs": [dict(r) for r in rows], "total": total}


# --------------------------------------------------------- 国と地域の一覧 ----

_region_cache: dict[str, list[dict]] = {}
_region_cache_lock = threading.Lock()


def regions_from_elements(elements: list, cc: str) -> list[dict]:
    """Overpass の relation タグから admin_level=4 の地域を取り出す。

    名前は name:ja、name:en、name の順。同じ ISO コードが複数あっても1件。
    key は保存時の pref。日本以外は ISO コードそのもの。
    """
    best: dict[str, tuple[int, dict]] = {}
    prefix = f"{cc}-"
    rank = {"name": 1, "name:en": 2, "name:ja": 3}
    for el in elements:
        tags = el.get("tags") or {}
        iso = (tags.get("ISO3166-2") or "").strip().upper()
        if not iso.startswith(prefix) or REGION_ISO_RE.fullmatch(iso) is None:
            continue
        name, score = iso, 0
        for key, points in rank.items():
            val = (tags.get(key) or "").strip()
            if val and points >= score:
                name, score = val, points
        if len(name) > 80:
            name = name[:80]
        item = {"iso": iso, "name": name, "key": ISO_TO_PREF.get(iso, iso)}
        old = best.get(iso)
        if old is None or score > old[0]:
            best[iso] = (score, item)
    return sorted((row for _, row in best.values()), key=lambda r: (r["name"], r["iso"]))


def list_regions(country: str) -> list[dict]:
    """国コードから地域一覧を返す。日本はネットワークを使わない。

    成功した応答（0件を含む）だけをプロセス内にキャッシュする。例外はキャッシュしない。
    国全体という要素は入れない。
    """
    cc = (country or "").strip().upper()
    if _COUNTRY_ONLY_RE.fullmatch(cc) is None or cc not in COUNTRIES:
        raise ValueError(f"unknown country: {cc}")
    if cc == "JP":
        return [
            {"iso": iso, "name": name, "key": name}
            for name, iso in PREF_ISO.items()
        ]
    with _region_cache_lock:
        cached = _region_cache.get(cc)
    if cached is not None:
        return [dict(r) for r in cached]
    query = f"""[out:json][timeout:90];
rel["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"~"^{cc}-"];
out tags;"""
    data = _overpass(query, attempts=2, pause_s=5, http_timeout=120)
    if "elements" not in data:
        raise RuntimeError(data.get("remark") or "overpass returned no elements")
    regions = regions_from_elements(data.get("elements") or [], cc)
    with _region_cache_lock:
        _region_cache[cc] = regions
    return [dict(r) for r in regions]


def resolve_fetch_target(*, pref: str | None = None, iso: str | None = None,
                         name: str | None = None) -> dict:
    """取得リクエストを保存キー・表示名・Overpass 用 ISO に分ける。

    日本の都道府県名、または JP-26 のような地域コードは保存キーを都道府県名にする
    （既存の poi.db と未取得バナーを壊さない）。国外の保存キーは ISO コード。
    "JP" や "US" のような国コードは拒否する。
    """
    if pref:
        if pref not in PREF_ISO:
            raise ValueError(f"unknown prefecture: {pref}")
        return {"pref": pref, "label": None, "iso": PREF_ISO[pref]}
    if not iso:
        raise ValueError("pref or iso is required")
    iso = iso.strip().upper()
    if _COUNTRY_ONLY_RE.fullmatch(iso):
        raise ValueError("国全体では取得できません。国の中の地域を指定してください")
    if not REGION_ISO_RE.fullmatch(iso):
        raise ValueError(f"invalid region code: {iso}")
    if iso in ISO_TO_PREF:
        return {"pref": ISO_TO_PREF[iso], "label": None, "iso": iso}
    cc = iso.split("-", 1)[0]
    if cc not in COUNTRIES:
        raise ValueError(f"unknown country: {cc}")
    region_name = (name or "").strip() or iso
    if len(region_name) > 80:
        region_name = region_name[:80]
    return {
        "pref": iso,
        "label": f"{COUNTRIES[cc]} / {region_name}",
        "iso": iso,
    }


# ------------------------------------------------------- バックグラウンド取得 --

@dataclass
class FetchStatus:
    running: bool = False
    pref: str = ""
    error: str | None = None
    done_prefs: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {"running": self.running, "pref": self.pref, "error": self.error}


STATUS = FetchStatus()
_fetch_lock = threading.Lock()


def fetch_and_store(pref: str, *, label: str | None = None, iso: str | None = None,
                    poi_db_path: Path = POI_DB) -> dict:
    """1地域の取得を予約してすぐ戻る。ダウンロードは別スレッド。

    ロックは戻る前に取る。呼び出し側が先にスレッドを立てると、重なった
    リクエストの両方に started=true が返り、終わっていない方を完了と誤る。
    iso を省略したときは pref を日本の都道府県名として扱う。
    """
    if not _fetch_lock.acquire(blocking=False):
        return {"started": False, "reason": "fetch already running"}
    shown = label or pref
    STATUS.running, STATUS.pref, STATUS.error = True, shown, None
    threading.Thread(
        target=_fetch_worker,
        kwargs={
            "pref": pref, "label": label, "iso": iso, "poi_db_path": poi_db_path,
        },
        daemon=True,
    ).start()
    return {"started": True, "pref": pref}


def _fetch_worker(pref: str, *, label: str | None, iso: str | None,
                  poi_db_path: Path) -> None:
    try:
        code = iso or PREF_ISO[pref]
        pois = fetch_by_iso(code)
        db = open_poi_db(poi_db_path)
        try:
            store_pref(db, pref, pois, label=label)
        finally:
            db.close()
        STATUS.done_prefs.append(pref)
    except Exception as e:
        log.exception("poi fetch failed: %s", pref)
        STATUS.error = str(e)
    finally:
        STATUS.running = False
        _fetch_lock.release()
