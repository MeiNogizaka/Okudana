# Okudana データスキーマ

製品名は Okudana。SQLite ファイル名は `okudana.db`。0.5.0 までの `photofinder.db` は起動時に改名する。旧バックアップ zip の中のファイル名も復元時に `okudana.db` へ揃える。

ストア構成: **SQLite（メタデータ + FTS5 全文）** / **FAISS（埋め込みベクトル）** / **ファイルシステム（サムネ WebP）**。
FAISS の vector ID = `photos.id` として結合し、対応表を持たない。

## ER 図

```mermaid
erDiagram
    ROOTS ||--o{ PHOTOS : contains
    PHOTOS ||--o| EXIF : has
    PHOTOS ||--o{ DETECTIONS : has
    PHOTOS ||--o{ OCR_TEXTS : has
    PHOTOS ||--o{ PHOTO_TAGS : has
    TAGS   ||--o{ PHOTO_TAGS : labels
    PHOTOS ||--o| GEO : has
    PHOTOS ||--o| PHOTOS_FTS : indexed_by
    DETECTIONS ||--o| BIRD_IDS : refined_by
    PHOTOS ||--o{ PHOTO_POSTS : linked_to
    PHOTOS ||--o| PHOTO_PALETTE : has
    PHOTOS ||--o{ PHOTO_EXPORT_REGIONS : cropped_as

    ROOTS {
        int id PK
        text path "走査ルート (UNC可)"
        text ext_filter "jpg;jpeg;png;heic;..."
        int scan_interval_sec "未使用 (定期スキャンは不採用)"
        int enabled
        int recursive "0=直下のみ 1=サブフォルダ含む(既定)"
    }
    PHOTOS {
        int id PK "== FAISS vector id"
        int root_id FK
        text path "ルート相対パス"
        int size
        real mtime
        text xxhash "16hex 差分/移動検知"
        blob phash "8byte 原本特定用"
        text ext "RAWも含む (raw_utils.RAW_EXTS)"
        int width "RAWは埋め込みプレビュー解像度(センサー原寸ではない)"
        int height "同上"
        text taken_at "ISO8601, EXIF優先"
        text index_state "pending|error|complete"
        int ml_version "適用済みMLパイプライン版。バックフィル判定に使用"
        int deleted "tombstone"
        text created_at
        text updated_at
        text exported_at "最後に書き出し成功したUTC時刻。NULLなら未書き出し"
    }
    EXIF {
        int photo_id PK_FK
        text camera_make
        text camera_model
        text lens_model
        real focal_length_mm
        real f_number
        text shutter_speed
        int iso
        real gps_lat
        real gps_lon
        real gps_alt
        real gps_img_direction
        text raw_json "全EXIFのJSON退避"
    }
    GEO {
        int photo_id PK_FK
        text country
        text prefecture
        text city
        text poi_name "建物/POI名 (最寄り、data/poi.db 参照)"
        real poi_conf
        text poi_alt "周辺の主要POI名(空白区切り)。地主神社の写真を清水寺でも検索可能にする"
        text poi_source "osm_nearby|manual (landmark_clf は未実装)"
    }
    DETECTIONS {
        int id PK
        int photo_id FK
        text label "yolo クラス名"
        real conf
        text bbox "x,y,w,h (作業画像比率)"
    }
    BIRD_IDS {
        int detection_id PK_FK
        text species_ja "和名"
        text species_sci "学名"
        real conf
        text topk_json "上位3候補"
        int confirmed "常に0のまま (既存UIの確定/否認操作はPHOTO_TAGS.verifiedのみ更新し、
                       このカラムは触らない。種名タグの確定/否認状態を見るなら
                       PHOTO_TAGS.verified側を種名タグ名で突き合わせること。
                       dataset_export.pyのspecies_detailゲートも同様の理由でこちらを使う"
    }
    OCR_TEXTS {
        int id PK
        int photo_id FK
        text text
        real conf
        text bbox
    }
    TAGS {
        int id PK
        text name UK
        text kind "auto|manual|species|place|ocr"
    }
    PHOTO_TAGS {
        int photo_id PK_FK
        int tag_id PK_FK
        real conf "autoのみ"
        text source "yolo|bird|geo|user|..."
        int verified "0=未レビュー(既定) 1=ユーザ確定 -1=ユーザ否認。
                     dataset_export.pyはverified!=0の行のみを人手の正解データとして書き出す"
    }
    PHOTOS_FTS {
        text tags_text "分かち書き済み"
        text ocr_text
        text place_text
        text caption "投稿キャプションと切り出しメモの集約"
    }
    PHOTO_POSTS {
        int id PK
        int photo_id FK
        text url "投稿URL (X/Instagram/その他)"
        text posted_at "判明していればISO8601。現状は手動入力のみ"
        text caption_snippet "oEmbedから取得したツイート本文の抜粋 (Xのみ)"
        text source "manual|archive (archiveはXデータアーカイブ取込)"
        text platform "x|instagram|other。投稿先SNS種別 (sourceとは独立)"
        text platform_label "platform='other'の時のみ使う任意の表示名"
        text note
        text created_at
    }
    PHOTO_PALETTE {
        int photo_id PK_FK
        int version "palette.PALETTE_VERSION。ml_versionとは別"
        text colors_json "最大5色。hexと画素数比のみ。印象文は保存しない"
        text updated_at
    }
    PHOTO_EXPORT_REGIONS {
        int photo_id PK_FK
        int slot PK "1から3。ブラウザの連射用の枠とは別"
        real x "作業画像に対する0-1"
        real y
        real w
        real h
        int src_w "記録したときの作業画像の幅"
        int src_h
        text memo "全文検索の caption に入る。空は対象外"
        int locked "1写真につき1枠。自動記録では上書きしない"
        text updated_at "自動記録が古い枠を選ぶ時刻。メモ更新では動かない"
    }
```

## SQLite DDL

**唯一の定義元は [okudana/schema.sql](../okudana/schema.sql)**（本書に DDL を複製すると
ドリフトするため撤去）。マイグレーションは okudana/db.py の _migrate が起動時に適用する。

スキーマ版の履歴:

| 版 | 変更 |
|---|---|
| v1 | 初期スキーマ（本書 ER 図の骨格） |
| v2 | photos.ml_version 追加（ML パイプライン版によるバックフィル判定）。photos_fts を contentless から通常 FTS5 へ（行の更新/削除を可能にするため） |
| v3 | geo.poi_alt 追加（周辺主要 POI 名。「地主神社」の写真を「清水寺」で検索可能にする）。app_settings テーブル追加（scan_on_startup, backup_auto） |
| v4 | roots.recursive 追加（フォルダごとに再帰/直下のみを切替） |
| v5 | photo_posts 追加（X投稿リンク・重複投稿警告機能。ALTER不要のCREATE TABLE IF NOT EXISTSのみのため _migrate() への追加コードなし） |
| v6 | photo_posts.platform/platform_label 追加（X以外のSNS投稿リンクにも対応）。photos.exported_at 追加（書き出し済みマーク表示用） |
| v7 | app_settings から backup_auto を削除（週次自動スナップショットをフルバックアップ/復元に置き換え、参照されなくなったため） |
| v8 | photo_palette 追加（カラーパレット。最大5色の hex と割合。印象文は保存しない。手順は [palette.md](palette.md)） |
| v9 | photo_export_regions 追加（写真ごとの切り出し枠。最大3。メモ付き。ブラウザの直前の枠とは別。メモは保存時にその写真の全文索引へ入れる。ML の再抽出はしない） |
| v10 | photo_export_regions.locked 追加（1写真につき1枠。自動記録で上書きしない）。app_settings.export_region_auto（既定オン。書き出し成功時に最新3件へ残す。画像全体は対象外） |

ER 図との差分に気づいたら schema.sql を正としてください。

## POI データベース (data/poi.db) — メイン DB とは別ファイル

OSM から取得した POI（建物・スポット名）と、ユーザが手動追加したカスタム地点を格納する。
`okudana/poi_fetch.py` が定義元。メイン DB (`okudana.db`) からは参照されず、
`geo.py` の `poi_lookup()` が実行時に別接続で検索する（`geo.poi_name` へは値のコピーのみ保存）。

```sql
CREATE TABLE pois (
    id     INTEGER PRIMARY KEY,
    osm_id TEXT NOT NULL,     -- 'node/123' 'way/456' / 手動地点は 'manual/<uuid>'
    name   TEXT NOT NULL,
    kind   TEXT NOT NULL,     -- 'tourism=viewpoint' 等の代表タグ / 手動は 'manual'
    pref   TEXT NOT NULL,     -- 取得単位のキー。日本は都道府県名、国外は ISO 3166-2、手動は 'manual'
    lat REAL NOT NULL, lon REAL NOT NULL,
    UNIQUE (pref, osm_id)
);
CREATE VIRTUAL TABLE poi_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
CREATE TABLE poi_meta (
    pref TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    count INTEGER NOT NULL,
    label TEXT                 -- 表示名。日本は NULL。国外は「国名 / 地域名」
);
```

既存の `poi.db` は `CREATE TABLE IF NOT EXISTS` では列が増えない。`ensure_poi_schema()`
が `poi_meta.label` を `ALTER TABLE` で足す（`open_poi_db()` と、`geo.py` の常駐接続の両方）。

- **更新単位は1地域**: `pref` ごとに丸ごと入れ替え（`store_pref`）。日本のキーは都道府県名のまま
  （`京都府` など。`JP-26` には書き換えない）。国外のキーは `US-CA` のような ISO 3166-2 で、
  画面に出す名前は `label`（例: `アメリカ / California`）。`label` を付けない再取得は、
  入っている表示名を消さない
- **国全体では取得しない**: 設定画面に「この国すべて」は無い。API も国コードだけ
  （`JP` や `US`、ISO 3166-1 / `admin_level=2`）は 422 で拒む。1つの都道府県でも
  Overpass は数十秒〜数分、件数は数千〜約2万（タイムアウト 300 秒。例: 京都府 17,358件、
  大阪府 19,837件、鳥取県 2,319件）。日本全体やアメリカ、中国を1クエリにすると制限時間を超え、
  `poi.db` も肥大する。取得の粒度は都道府県と同じ `admin_level=4` の1地域だけにする。
  イングランドのように大きい地域が時間切れになっても、国全体へ広げることはしない。
  そのときはエラーを出し、地点はカスタム地点で足す
- **手動地点の pref は `'manual'`**: 近傍検索 (`poi_lookup`) は `pref` を問わず bbox 検索するため、
  カスタム地点も取得済み POI と同列に扱われ自動的にマッチ対象になる。写真ごとの場所名の手修正もそのまま
- 市区町村名は GeoNames の逆ジオコーディング（国を問わない）で別途付く。POI の国・地域の選択は建物・スポット名の取得範囲だけを決める

## FAISS 構成

| 項目 | 値 |
|---|---|
| ファイル | `data/vectors.faiss`（tmp 書き→rename でアトミック更新） |
| 型 | `IndexIDMap2(IndexHNSWFlat(ml.DIM, M=32))`、〜50万枚まで。`ml.DIM`は現在1152（SigLIP2 so400m、2026-07-19〜。旧768から変更）。次元変更時は`VectorStore`が不一致を検知して自動再構築（`okudana/vectors.py`参照） |
| HNSWパラメータ | `efConstruction=200`（旧: FAISS既定値40のまま未調整）。`efSearch`はクエリのk（main.pyのフィルタ絞り込み強度に応じた200〜2000の動的拡張）に合わせ`max(128, min(4000, k*2))`で毎回動的設定（このフォークで追加） |
| 距離 | 内積（ベクトルは L2 正規化済み → コサイン等価） |
| 削除 | tombstone（`photos.deleted=1` で検索後除外）。HNSW は remove 不可のため再インデックスで同一 id が重複しうる（検索時 dedupe） |
| 再構築 | `POST /api/index/rebuild-vectors`（設定 UI・バックアップ時オプションからも可）。既存ベクトルを reconstruct して重複・削除済み id を除去。画像の再エンコード不要 |

## サムネイル

```
data/thumbs/{xxhash[:2]}/{xxhash}.webp   -- 320px 長辺, q=80
data/previews/{xxhash[:2]}/{xxhash}.webp -- 1600px 長辺, 初回要求時に遅延生成
```
