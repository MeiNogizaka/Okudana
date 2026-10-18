"""アプリのルートパス解決 (Docker コンテナ実行前提)。

外部データ vs 同梱データの使い分け:
- モデル・DB・キャッシュ等、ユーザ環境ごとに異なる/差し替え可能なファイルは
  ここの app_root()/data_dir() 基準。Docker では data_dir() を OKUDANA_DATA
  経由でボリュームマウントに向ける。未設定なら旧名 PHOTOFINDER_DATA を読む。
- schema.sql や static/ のようにアプリコードと不可分の同梱データは、
  従来通り Path(__file__).parent 基準のままにする (db.py の SCHEMA_PATH,
  main.py の STATIC_DIR)。
"""
from __future__ import annotations

import os
from pathlib import Path


def app_root() -> Path:
    return Path(__file__).parent.parent


def env(suffix: str, default: str | None = None) -> str | None:
    """OKUDANA_<suffix> を優先し、未設定なら旧 PHOTOFINDER_<suffix> を読む。

    空文字は未設定と同じ。0.4.x のコンテナ環境変数を、新しいイメージへ
    切り替える前でも読めるようにするための受け口。
    """
    for prefix in ("OKUDANA_", "PHOTOFINDER_"):
        val = os.environ.get(prefix + suffix)
        if val is not None and val != "":
            return val
    return default


def data_dir() -> Path:
    """外部データ (DB・サムネ・FAISS・キャッシュ・POI等) のルート。

    OKUDANA_DATA（無ければ PHOTOFINDER_DATA）があればそれを優先する
    (テスト時のデータ分離用)。未設定なら app_root()/data。main.DATA_DIR
    だけでなく bird/colors のキャッシュ・POI_DB もこれを基準にすることで、
    環境変数指定時に一部だけ旧データを見てしまう不整合を防ぐ。
    """
    override = env("DATA")
    return Path(override) if override else app_root() / "data"
