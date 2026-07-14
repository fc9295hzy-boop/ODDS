
# -*- coding: utf-8 -*-
"""
tracker.py - Poll continu + detection de steam move (foot et tennis SEPARES)
----------------------------------------------------------------------
Deux pipelines independants (config, seuils, listes de matchs suivis) :
FOOT_CONFIG et TENNIS_CONFIG ne se melangent jamais dans l'analyse.
Chaque sport a ses propres seuils de detection, calibres a sa liquidite.
 
Logique :
1. Toutes les X secondes, pour chaque match suivi -> save_snapshot()
2. Calcul du delta par bookmaker vs la cote d'il y a N minutes
3. Si >= 2 bookmakers bougent dans le meme sens au-dela du seuil -> steam move -> alerte Telegram
4. Si un seul book bouge alors que les autres non -> divergence isolee -> alerte plus discrete
   (potentiel sharp qui a bouge en premier, ou book en retard)
"""
 
import os
import time
import logging
import asyncio
from datetime import datetime, timezone
 
from telegram import Bot
 
from storage import init_db, save_snapshot, get_history, list_tracked_matches, remove_tracked_match
from bot import find_event, get_odds  # reutilise les fonctions existantes de bot.py
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tracker")
 
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
bot = Bot(token=TELEGRAM_TOKEN)
 
# ---------- Config par sport - JAMAIS partagee entre les deux ----------
FOOT_CONFIG = {
    "sport": "football",
    "poll_interval_sec": 900,       # 15 min par defaut
    "poll_interval_close_sec": 120, # 2 min dans la fenetre serree pre-match
    "close_window_min": 180,        # "pre-match serre" = 3h avant coup d'envoi
    "delta_window_min": 30,         # compare vs il y a 30 min
    "steam_threshold_pct": 3.0,     # 3% de mouvement sur la proba implicite
    "min_books_for_steam": 2,       # au moins 2 books doivent bouger ensemble
    # Plus de liste "matches" en dur ici : lue depuis la DB a chaque cycle,
    # geree via /track /untrack /tracked sur Telegram.
}
 
TENNIS_CONFIG = {
    "sport": "tennis",
    "poll_interval_sec": 600,       # 10 min (matchs plus courts a suivre)
    "poll_interval_close_sec": 60,  # 1 min dans la fenetre serree pre-match
    "close_window_min": 90,         # tennis = fenetre serree plus courte
    "delta_window_min": 20,
    "steam_threshold_pct": 4.0,     # seuil plus haut : moins de books, plus de bruit
    "min_books_for_steam": 2,
    # Plus de liste "matches" en dur ici : lue depuis la DB a chaque cycle.
}
 
 
def compute_deltas(sport, event_id, delta_window_min):
    """
    Pour chaque bookmaker ayant des donnees sur ce match, compare la derniere
    cote connue vs celle d'il y a `delta_window_min` minutes.
    Retourne une liste de dicts {bookmaker, delta_home_pct, delta_away_pct, direction}.
    """
    history = get_history(sport, event_id, since_minutes=delta_window_min + 5)
    if not history:
        return []
 
    by_book = {}
    for row in history:
        by_book.setdefault(row["bookmaker"], []).append(row)
 
    results = []
    for book, rows in by_book.items():
        rows.sort(key=lambda r: r["ts"])
        if len(rows) < 2:
            continue
        first, last = rows[0], rows[-1]
        delta_home = (last["implied_home"] - first["implied_home"]) * 100
        delta_away = (last["implied_away"] - first["implied_away"]) * 100
        results.append({
            "bookmaker": book,
            "delta_home_pct": round(delta_home, 2),
            "delta_away_pct": round(delta_away, 2),
            "first_ts": first["ts"],
            "last_ts": last["ts"],
            "home": last["home"],
            "away": last["away"],
        })
    return results
 
 
def detect_steam(deltas, threshold_pct, min_books):
    """
    Regarde si plusieurs books bougent dans le meme sens au-dela du seuil.
    Retourne (is_steam, direction, books_concernes) ou (False, None, []).
    """
    movers_home = [d for d in deltas if d["delta_home_pct"] >= threshold_pct]
    movers_away = [d for d in deltas if d["delta_away_pct"] >= threshold_pct]
 
    if len(movers_home) >= min_books:
        return True, "home", movers_home
    if len(movers_away) >= min_books:
        return True, "away", movers_away
    return False, None, []
 
 
def detect_isolated_divergence(deltas, threshold_pct):
    """Un seul book bouge fort pendant que les autres sont stables -> a signaler,
    signal plus faible qu'un steam mais utile (sharp book en avance, ou book en retard)."""
    strong_movers = [d for d in deltas
                      if abs(d["delta_home_pct"]) >= threshold_pct
                      or abs(d["delta_away_pct"]) >= threshold_pct]
    if len(strong_movers) == 1:
        return strong_movers[0]
    return None
 
 
async def send_alert(sport, msg):
    prefix = "⚽" if sport == "football" else "🎾"
    await bot.send_message(chat_id=OWNER_CHAT_ID, text=f"{prefix} {msg}", parse_mode="Markdown")
 
 
async def process_match(cfg, team_a, team_b):
    sport = cfg["sport"]
    try:
        event = find_event(team_a, team_b, sport)
    except Exception as e:
        log.error(f"[{sport}] Erreur recherche {team_a} vs {team_b}: {e}")
        return
    if not event:
        log.warning(f"[{sport}] Match introuvable: {team_a} vs {team_b}")
        return
 
    # ---- Arret automatique si le coup d'envoi est deja passe ----
    # Evite de continuer a consommer le quota API sur un match qui a demarre :
    # le steam move sur les cotes n'a plus de sens une fois le match en cours
    # (les books passent en mode live, logique totalement differente).
    kickoff_str = event.get("date", "")
    try:
        kickoff = datetime.fromisoformat(kickoff_str.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= kickoff:
            remove_tracked_match(sport, team_a, team_b)
            log.info(f"[{sport}] {team_a} vs {team_b} : coup d'envoi passe, "
                      f"suivi arrete automatiquement.")
            await send_alert(sport, f"Suivi arrete (coup d'envoi passe) : "
                                     f"{event.get('home')} vs {event.get('away')}")
            return
    except (ValueError, TypeError):
        # Si la date est illisible, on ne bloque pas le suivi pour autant
        log.warning(f"[{sport}] Date illisible pour {team_a} vs {team_b}: {kickoff_str!r}")
 
    try:
        odds_json = get_odds(event["id"])
    except Exception as e:
        log.error(f"[{sport}] Erreur cotes {team_a} vs {team_b}: {e}")
        return
 
    n = save_snapshot(sport, event, odds_json)
    log.info(f"[{sport}] Snapshot {event['home']} vs {event['away']}: {n} lignes stockees")
 
    deltas = compute_deltas(sport, str(event["id"]), cfg["delta_window_min"])
    if not deltas:
        return
 
    is_steam, direction, books = detect_steam(deltas, cfg["steam_threshold_pct"], cfg["min_books_for_steam"])
    if is_steam:
        side = event["home"] if direction == "home" else event["away"]
        book_names = ", ".join(d["bookmaker"] for d in books)
        avg_delta = sum(d[f"delta_{direction}_pct"] for d in books) / len(books)
        msg = (f"*STEAM MOVE* : {event['home']} vs {event['away']}\n"
               f"Mouvement vers *{side}* : +{avg_delta:.1f}% sur {len(books)} books ({book_names})\n"
               f"Fenetre : {cfg['delta_window_min']} min")
        await send_alert(sport, msg)
        return
 
    isolated = detect_isolated_divergence(deltas, cfg["steam_threshold_pct"])
    if isolated:
        msg = (f"*Divergence isolee* : {event['home']} vs {event['away']}\n"
               f"{isolated['bookmaker']} seul a bouger : "
               f"home {isolated['delta_home_pct']:+.1f}% / away {isolated['delta_away_pct']:+.1f}%\n"
               f"(sharp en avance, ou book en retard sur le marche)")
        await send_alert(sport, msg)
 
 
async def run_pipeline(cfg):
    """Boucle infinie pour un sport donne. Relit la liste des matchs suivis
    depuis la DB a CHAQUE cycle -> /track et /untrack prennent effet
    immediatement, sans redeploy."""
    while True:
        tracked = list_tracked_matches(cfg["sport"])
        if not tracked:
            log.info(f"[{cfg['sport']}] Aucun match suivi actuellement.")
        for row in tracked:
            await process_match(cfg, row["team_a"], row["team_b"])
        # TODO: rendre l'intervalle adaptatif (poll_interval_close_sec) une fois
        # qu'on croise avec l'heure de coup d'envoi de chaque match (cf. event["date"])
        await asyncio.sleep(cfg["poll_interval_sec"])
 
 
async def start_tracking():
    """
    Point d'entree appele depuis bot.py pour lancer les pipelines en tache de fond,
    DANS LE MEME PROCESS que le bot Telegram (1 seul service Railway = budget respecte).
    N'est PAS bloquant : cree des asyncio.Task et retourne immediatement.
    Les matchs suivis sont geres via /track /untrack /tracked sur Telegram,
    pas besoin de config en dur ni de redeploy.
    """
    init_db()
    log.info("DB initialisee, demarrage des pipelines foot + tennis (separes).")
    asyncio.create_task(run_pipeline(FOOT_CONFIG))
    asyncio.create_task(run_pipeline(TENNIS_CONFIG))
 
 
# Lancement autonome possible pour tester tracker.py seul en local, hors Railway.
if __name__ == "__main__":
    async def _standalone():
        await start_tracking()
        await asyncio.Event().wait()  # bloque indefiniment
    asyncio.run(_standalone())
