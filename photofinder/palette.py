"""写真1枚のカラーパレット（最大5色）と、表示用の印象文。

抽出は OKLab 上の k-means。鳥の色タグ（colors.py の SigLIP）とは別で、
検索用タグにも全文検索にも入れない。心理効果の文章は保存せず、表示時に
保存済みの #RRGGBB から組み立てる。手順としきい値の根拠は docs/palette.md。
"""
from __future__ import annotations

import json
import math
import re

import numpy as np
from PIL import Image

# 算法の版。上げると次回スキャンでパレット行だけを作り直す。
# ML_VERSION とは独立（こちらを上げても YOLO / OCR / 埋め込みは再実行しない）。
PALETTE_VERSION = 1

MAX_COLORS = 5
SAMPLE_EDGE = 96          # 長辺。これ以上縮めてから集計する
K_INIT = 8                # いったん多めに分け、近い色を畳んでから最大5色に絞る
KMEANS_ITERS = 12
MERGE_DISTANCE = 0.05     # OKLab ユークリッド距離。これ未満は同一色として畳む
MIN_SHARE = 0.04           # これ未満のクラスタは面積枠から外す
ACCENT_MIN_SHARE = 0.015   # 小さいが鮮やかな色を1枠残す下限
ACCENT_MIN_CHROMA = 0.08   # その色を「鮮やか」とみなす OKLab 彩度
# 彩度がこれ未満なら色相名を付けず、白・灰・黒として扱う
HUE_CHROMA_MIN = 0.03
# 活動性。彩度だけで決める（明度と覚醒の向きは研究間で一致しない。docs/palette.md）
# この値以上の彩度のときだけ、色相ごとの覚醒を含む一文を足す
ACTIVITY_CALM = 0.12
# 重さ。OKLab の L
WEIGHT_HEAVY = 0.45
WEIGHT_LIGHT = 0.75
# 暖寒。Ou らの「彩度が高いほど色相の温度が立つ」に合わせ、C * cos(h - ピーク) で判定。
# ピーク 50° は OKLab で赤〜橙のあいだ。しきい値は純緑 (#00AA00, 色相約143°) が
# 「涼しい」に入るよう 0.008 にしている
WARM_PEAK_DEG = 50.0
WARM_SCORE = 0.008
# 無彩色の名前分け
WHITE_L = 0.92
BLACK_L = 0.22
# 茶: 黄橙寄り・暗め・彩度が高すぎない。桃: 赤〜赤紫寄りで明るい
BROWN_L = 0.55
BROWN_C_MAX = 0.16
PINK_L = 0.70

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def extract_palette(img: Image.Image) -> list[dict]:
    """RGB 画像から最大5色を返す。各要素は {"hex", "ratio"}。

    ratio は元画像の画素数に対する割合で、捨てた端色の分だけ合計が 1 未満に
    なりうる。呼び出し側の画像は縮小しない。
    """
    sample = img.convert("RGB")
    sample.thumbnail((SAMPLE_EDGE, SAMPLE_EDGE), Image.Resampling.BOX)
    pixels = np.asarray(sample, dtype=np.float32).reshape(-1, 3)
    if pixels.size == 0:
        return []
    lab = _srgb_to_oklab(pixels)
    k = min(K_INIT, len(lab))
    labels, _centers = _kmeans(lab, k)
    buckets: dict[int, list[np.ndarray]] = {}
    for lab_px, label in zip(lab, labels):
        buckets.setdefault(int(label), []).append(lab_px)
    clusters = []
    for group in buckets.values():
        stacked = np.stack(group)
        clusters.append((stacked.mean(axis=0), len(group)))
    merged = _merge(clusters, MERGE_DISTANCE)
    total = sum(count for _, count in merged) or 1

    class _C:
        def __init__(self, center: np.ndarray, count: int):
            self.center = center
            self.count = count
            self.share = count / total
            self.chroma = float(np.hypot(center[1], center[2]))

    items = [_C(c, n) for c, n in merged]
    significant = [c for c in items if c.share >= MIN_SHARE]
    if not significant:
        significant = [max(items, key=lambda c: c.share)]
    significant.sort(key=lambda c: -c.share)
    picks = significant[:4]
    picked = {id(c) for c in picks}
    accent = None
    accent_chroma = -1.0
    for c in items:
        if id(c) in picked:
            continue
        if c.share < ACCENT_MIN_SHARE or c.chroma < ACCENT_MIN_CHROMA:
            continue
        if c.chroma > accent_chroma:
            accent, accent_chroma = c, c.chroma
    if accent is not None:
        picks.append(accent)
    elif len(significant) > 4:
        picks.append(significant[4])
    picks.sort(key=lambda c: -c.share)
    return [
        {"hex": _oklab_to_hex(c.center), "ratio": round(c.share, 4)}
        for c in picks[:MAX_COLORS]
    ]


def save_palette(db, photo_id: int, colors: list[dict]) -> None:
    """パレット行を書く。commit は呼び出し側。"""
    payload = json.dumps(
        [{"hex": c["hex"], "ratio": c["ratio"]} for c in colors],
        ensure_ascii=False,
    )
    db.execute(
        """INSERT INTO photo_palette (photo_id, version, colors_json, updated_at)
           VALUES (?,?,?,datetime('now'))
           ON CONFLICT(photo_id) DO UPDATE SET
             version=excluded.version,
             colors_json=excluded.colors_json,
             updated_at=datetime('now')""",
        (photo_id, PALETTE_VERSION, payload),
    )


def colors_from_json(text: str | None) -> list[dict] | None:
    """保存 JSON を API 用の色リストにする。壊れていれば None。"""
    if not text:
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw[:MAX_COLORS]:
        if not isinstance(item, dict):
            continue
        hex_code = item.get("hex")
        ratio = item.get("ratio")
        if not isinstance(hex_code, str) or not _HEX.match(hex_code):
            continue
        if not isinstance(ratio, (int, float)):
            continue
        described = describe_hex(hex_code)
        described["ratio"] = round(float(ratio), 4)
        out.append(described)
    return out


def describe_hex(hex_code: str) -> dict:
    """#RRGGBB から色名と印象語を返す。ratio は付けない。"""
    L, C, h = _oklch_of_hex(hex_code)
    name, hue_note = _category(L, C, h)
    return {
        "hex": hex_code.upper(),
        "name": name,
        "temperature": _temperature(C, h),
        "weight": _weight(L),
        "activity": _activity(C),
        "hue_note": hue_note,
    }


def _temperature(C: float, h: float) -> str:
    if C < HUE_CHROMA_MIN:
        return "温度感は弱い"
    score = C * math.cos(math.radians(h - WARM_PEAK_DEG))
    if score >= WARM_SCORE:
        return "暖かい"
    if score <= -WARM_SCORE:
        return "涼しい"
    return "温度感は弱い"


def _weight(L: float) -> str:
    if L < WEIGHT_HEAVY:
        return "重い"
    if L > WEIGHT_LIGHT:
        return "軽い"
    return "中くらいの重さ"


def _activity(C: float) -> str:
    if C < HUE_CHROMA_MIN:
        return "静か"
    if C < ACTIVITY_CALM:
        return "穏やか"
    return "活発"


def _category(L: float, C: float, h: float) -> tuple[str, str | None]:
    if C < HUE_CHROMA_MIN:
        if L >= WHITE_L:
            return "白", "前向きで、覚醒は低め"
        if L <= BLACK_L:
            return "黒", "否定寄りで、覚醒は高め。暗い色は支配感が強い"
        return "灰", "覚醒が低く、否定寄り"
    # 呼び方に明度が織り込まれているものから先に分ける
    if L >= 0.80 and C < 0.08 and 40.0 <= h <= 115.0:
        return "ベージュ", None
    if L < BROWN_L and C < BROWN_C_MAX and 30.0 <= h <= 115.0:
        return "茶", "黄橙の暗色。重く、地に足がついた印象。否定寄りの報告もある"
    if L >= PINK_L and (h <= 45.0 or h >= 330.0):
        return "桃", "前向きで、柔らかい"
    if h < 40.0 or h >= 345.0:
        name, note = "赤", "高覚醒で、力と、快・緊張の両方がある"
    elif h < 75.0:
        name, note = "橙", "前向きで、覚醒が高い"
    elif h < 115.0:
        name, note = "黄", "前向きで、覚醒が高い"
    elif h < 165.0:
        name, note = "緑", "前向きで、覚醒は低め"
    elif h < 210.0:
        name, note = "青緑", "前向きで、覚醒は低め"
    elif h < 275.0:
        name, note = "青", "前向きで、覚醒は低め"
    elif h < 330.0:
        name, note = "紫", "力と格式"
    else:
        name, note = "赤", "高覚醒で、力と、快・緊張の両方がある"
    # 色相の一文は、その色相らしい彩度があるときだけ。薄い色は3軸だけにする
    if C < ACTIVITY_CALM:
        return name, None
    return name, note


def _oklch_of_hex(hex_code: str) -> tuple[float, float, float]:
    rgb = np.array([[
        int(hex_code[1:3], 16),
        int(hex_code[3:5], 16),
        int(hex_code[5:7], 16),
    ]], dtype=np.float32)
    L, a, b = _srgb_to_oklab(rgb)[0]
    C = float(math.hypot(a, b))
    h = math.degrees(math.atan2(float(b), float(a))) % 360.0
    return float(L), C, h


def _srgb_to_oklab(rgb: np.ndarray) -> np.ndarray:
    """sRGB 0-255 の (N,3) を OKLab にする。Björn Ottosson の変換。"""
    c = rgb.astype(np.float64) / 255.0
    linear = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = linear[:, 0], linear[:, 1], linear[:, 2]
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b_ = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return np.stack([L, a, b_], axis=1)


def _oklab_to_hex(lab: np.ndarray) -> str:
    L, a, b = (float(v) for v in lab)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bch = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    rgb = []
    for channel in (r, g, bch):
        channel = min(1.0, max(0.0, channel))
        if channel <= 0.0031308:
            encoded = 12.92 * channel
        else:
            encoded = 1.055 * (channel ** (1 / 2.4)) - 0.055
        rgb.append(int(round(min(1.0, max(0.0, encoded)) * 255)))
    return f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"


def _kmeans(points: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """固定シードの k-means++。同じ画像なら同じ初期値になる。"""
    rng = np.random.default_rng(0)
    n = len(points)
    centers = np.empty((k, 3), dtype=np.float64)
    centers[0] = points[int(rng.integers(0, n))]
    closest = np.full(n, np.inf)
    for i in range(1, k):
        dist = np.sum((points - centers[i - 1]) ** 2, axis=1)
        closest = np.minimum(closest, dist)
        total = float(closest.sum())
        if total <= 0:
            centers[i:] = centers[0]
            break
        centers[i] = points[int(rng.choice(n, p=closest / total))]
    labels = np.full(n, -1, dtype=np.int32)
    for _ in range(KMEANS_ITERS):
        d2 = np.sum((points[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        new_labels = np.argmin(d2, axis=1).astype(np.int32)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            mask = labels == j
            if np.any(mask):
                centers[j] = points[mask].mean(axis=0)
            else:
                centers[j] = points[int(np.argmax(np.min(d2, axis=1)))]
    return labels, centers


def _merge(clusters: list[tuple[np.ndarray, int]], dist: float) -> list[tuple[np.ndarray, int]]:
    """近い中心を、画素数の多い側へ畳む。"""
    ordered = sorted(clusters, key=lambda item: -item[1])
    kept: list[tuple[np.ndarray, int]] = []
    for center, count in ordered:
        if count <= 0:
            continue
        merged = False
        for i, (kc, kn) in enumerate(kept):
            if float(np.linalg.norm(center - kc)) < dist:
                n = kn + count
                kept[i] = ((kc * kn + center * count) / n, n)
                merged = True
                break
        if not merged:
            kept.append((center.copy(), count))
    return kept
