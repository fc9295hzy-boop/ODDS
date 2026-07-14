# -*- coding: utf-8 -*-
"""
storage.py - Historisation des cotes par bookmaker (foot ET tennis, pipelines separes)
----------------------------------------------------------------------
Contrairement a oddyoddy actuel (stateless, snapshot a la demande), ce module
garde une trace de CHAQUE cote individuelle par bookmaker dans le temps,
pour pouvoir calculer des deltas et detecter des steam moves.

IMPORTANT - Persistence sur Railway :
Le filesystem Railway est ephemere par defaut (reset a chaque redeploy).
Pour garder l'historique, il faut soit :
  a) Monter un Volume Railway sur le dossier contenant odds_history.db
  b) Passer sur Postgres (addon Railway, quelques clics)
Tant que ce n'est pas fait, l'historique repart a zero a chaque deploy.

Schema : une ligne = une cote d'UN bookmaker sur UN marche a UN instant T.
On ne stocke jamais la moyenne : la moyenne, on la recalcule a la demande.
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "odds_history.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                sport TEXT NOT NULL,
                event_id TEXT NOT NULL,
                home TEXT NOT NULL,
                away TEXT NOT NULL,
                bookmaker TEXT NOT NULL,
                odds_home REAL NOT NULL,
                odds_draw REAL,
                odds_away REAL NOT NULL,
                implied_home REAL NOT NULL,
                implied_draw REAL,
                implied_away REAL NOT NULL,
                margin_pct REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_book_ts
            ON odds_snapshots (sport, event_id, bookmaker, ts)
        """)


def _implied(oh, od, oa):
    """Meme logique que implied_probs() dans bot.py, mais par book individuel."""
    if od:
        ih, idr, ia = 1/oh, 1/od, 1/oa
        over = ih + idr + ia
        return ih/over, idr/over, ia/over, (over - 1) * 100
    ih, ia = 1/oh, 1/oa
    over = ih + ia
    return ih/over, None, ia/over, (over - 1) * 100


def save_snapshot(sport, event, odds_json):
    """
    Parcourt les cotes ML de chaque bookmaker individuellement et les stocke.
    sport: "football" ou "tennis"
    event: dict retourne par find_event() (home, away, id, ...)
    odds_json: dict retourne par get_odds()
    Retourne le nombre de lignes inserees.
    """
    books = odds_json.get("bookmakers", {})
    if not isinstance(books, dict):
        return 0

    now = datetime.now(timezone.utc).isoformat()
    home, away, event_id = event.get("home", "?"), event.get("away", "?"), str(event.get("id"))
    rows = []

    for book_name, markets in books.items():
        if not isinstance(markets, list):
            continue
        for m in markets:
            if str(m.get("name", "")).upper() != "ML":
                continue
            for o in m.get("odds", []):
                try:
                    oh = float(o["home"]) if o.get("home") else None
                    od = float(o["draw"]) if o.get("draw") else None
                    oa = float(o["away"]) if o.get("away") else None
                except (ValueError, TypeError):
                    continue
                if not (oh and oa):
                    continue
                ih, idr, ia, margin = _implied(oh, od, oa)
                rows.append((now, sport, event_id, home, away, book_name,
                             oh, od, oa, ih, idr, ia, margin))

    if not rows:
        return 0

    with get_conn() as conn:
        conn.executemany("""
            INSERT INTO odds_snapshots
            (ts, sport, event_id, home, away, bookmaker,
             odds_home, odds_draw, odds_away, implied_home, implied_draw, implied_away, margin_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
    return len(rows)


def get_history(sport, event_id, bookmaker=None, since_minutes=None):
    """Recupere l'historique brut, filtre optionnel par book et par fenetre temporelle."""
    query = "SELECT * FROM odds_snapshots WHERE sport = ? AND event_id = ?"
    params = [sport, str(event_id)]
    if bookmaker:
        query += " AND bookmaker = ?"
        params.append(bookmaker)
    if since_minutes:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
        query += " AND ts >= ?"
        params.append(cutoff)
    query += " ORDER BY ts ASC"

    with get_conn() as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]
