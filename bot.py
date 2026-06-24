# -*- coding: utf-8 -*-
"""
Bot Telegram prive - Probabilites foot (API-Football)
-----------------------------------------------------
Tu tapes deux equipes -> % de victoire / nul / victoire,
via l'endpoint /predictions d'API-Football.
 
Economies :
  - cache des IDs d'equipes (pas de re-recherche)
  - compteur quotidien + garde-fou anti-depassement
  - 2-3 requetes par recherche max
 
Variables Railway :
  TELEGRAM_TOKEN, OWNER_CHAT_ID, APIFOOTBALL_KEY
"""
 
import os
import json
import logging
from datetime import datetime, timezone
import requests
from telegram import Update
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes)
 
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
OWNER_CHAT_ID   = int(os.environ["OWNER_CHAT_ID"])
APIFOOTBALL_KEY = os.environ["APIFOOTBALL_KEY"]
 
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": APIFOOTBALL_KEY}
 
DAILY_LIMIT = 100        # quota du plan gratuit
SAFETY_STOP = 90         # on s'arrete avant pour garder une marge
SEASON = datetime.now(timezone.utc).year   # saison courante
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("footbot")
 
# --- etat persistant leger (cache equipes + compteur jour) ---
STATE_FILE = "/tmp/footbot_state.json"
 
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"teams": {}, "date": "", "count": 0}
 
def save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception as e:
        log.warning("save_state: %s", e)
 
STATE = load_state()
 
 
def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
 
def _reset_if_new_day():
    if STATE.get("date") != _today():
        STATE["date"] = _today()
        STATE["count"] = 0
        save_state(STATE)
 
def _can_request():
    _reset_if_new_day()
    return STATE["count"] < SAFETY_STOP
 
def _track():
    STATE["count"] = STATE.get("count", 0) + 1
    save_state(STATE)
 
def _remaining():
    _reset_if_new_day()
    return max(0, DAILY_LIMIT - STATE["count"])
 
 
def is_owner(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID
 
 
# ---------- Appels API ----------
def api_get(path, params):
    r = requests.get(f"{BASE_URL}{path}", headers=HEADERS,
                     params=params, timeout=20)
    _track()
    r.raise_for_status()
    data = r.json()
    # API-Football renvoie les erreurs dans data["errors"]
    errs = data.get("errors")
    if errs:
        raise RuntimeError(str(errs))
    return data.get("response", [])
 
 
def find_team_id(name):
    """Cherche l'ID d'une equipe, avec cache."""
    key = name.lower().strip()
    cached = STATE.get("teams", {}).get(key)
    if cached:
        return cached
    resp = api_get("/teams", {"search": key})
    if not resp:
        return None
    tid = resp[0]["team"]["id"]
    tname = resp[0]["team"]["name"]
    STATE.setdefault("teams", {})[key] = tid
    STATE["teams"][tname.lower()] = tid
    save_state(STATE)
    return tid
 
 
def find_fixture(id_a, id_b):
    """Trouve le prochain match a venir entre deux equipes (head to head).
    Le parametre 'next' est payant : on recupere donc le H2H et on filtre
    le prochain match non encore joue cote code."""
    h2h = f"{id_a}-{id_b}"
    resp = api_get("/fixtures/headtohead", {"h2h": h2h})
    if not resp:
        return None
 
    now = datetime.now(timezone.utc)
    upcoming = []
    for fx in resp:
        status = fx.get("fixture", {}).get("status", {}).get("short", "")
        date_str = fx.get("fixture", {}).get("date", "")
        if status in ("NS", "TBD"):
            try:
                d = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            except Exception:
                d = None
            upcoming.append((d, fx))
 
    if upcoming:
        upcoming.sort(key=lambda x: (x[0] is None, x[0]))
        return upcoming[0][1]
 
    return resp[-1]
 
 
def get_prediction(fixture_id):
    resp = api_get("/predictions", {"fixture": fixture_id})
    if not resp:
        return None
    return resp[0]
 
 
# ---------- Construction reponse ----------
def build_reply(team_a, team_b):
    if not _can_request():
        return (f"Quota du jour bientot atteint ({STATE['count']}/{DAILY_LIMIT}).\n"
                "Je m'arrete pour proteger ton compte. Ca repart demain.")
 
    try:
        ida = find_team_id(team_a)
        idb = find_team_id(team_b)
    except Exception as e:
        return f"!! Erreur recherche equipe : {e}"
 
    if not ida:
        return f"Equipe introuvable : {team_a}. Essaie le nom anglais (ex: Morocco)."
    if not idb:
        return f"Equipe introuvable : {team_b}. Essaie le nom anglais (ex: Japan)."
 
    try:
        fx = find_fixture(ida, idb)
    except Exception as e:
        return f"!! Erreur recherche match : {e}"
    if not fx:
        return ("Aucun match a venir trouve entre ces deux equipes.\n"
                "Le bot ne gere que les matchs programmes (pas les hypothetiques).")
 
    fid = fx["fixture"]["id"]
    league = fx["league"]["name"]
    date = fx["fixture"]["date"][:16].replace("T", " ")
    home = fx["teams"]["home"]["name"]
    away = fx["teams"]["away"]["name"]
 
    try:
        pred = get_prediction(fid)
    except Exception as e:
        return f"!! Erreur prediction : {e}"
    if not pred:
        return f"Match trouve : {home} vs {away} ({league})\nPas de prediction dispo."
 
    pct = pred.get("predictions", {}).get("percent", {})
    p_home = pct.get("home", "?")
    p_draw = pct.get("draw", "?")
    p_away = pct.get("away", "?")
 
    advice = pred.get("predictions", {}).get("advice", "")
 
    msg = (
        f"*{home} vs {away}*\n"
        f"_{league}_  -  {date}\n\n"
        f"{home} : *{p_home}*\n"
        f"Nul : *{p_draw}*\n"
        f"{away} : *{p_away}*\n"
    )
    if advice:
        msg += f"\n_Conseil API : {advice}_\n"
    msg += (f"\n-> Compare avec le prix Polymarket."
            f"\n_Requetes today : {STATE['count']}/{DAILY_LIMIT}_")
    return msg
 
 
# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Bot proba foot pret (API-Football).\n\n"
        "Tape deux equipes :\n`France Japon`  ou  `/match France Japon`\n\n"
        f"Quota du jour : {_remaining()}/{DAILY_LIMIT} requetes restantes.",
        parse_mode="Markdown")
 
 
async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        f"Requetes utilisees aujourd'hui : {STATE['count']}/{DAILY_LIMIT}\n"
        f"Restantes : {_remaining()}", parse_mode="Markdown")
 
 
async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = (update.message.text or "").replace("/match", "").strip()
    parts = None
    for sep in (" vs ", " VS ", " - ", " / ", "-"):
        if sep in text:
            parts = text.split(sep, 1)
            break
    if parts is None:
        parts = text.split(None, 1)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        await update.message.reply_text(
            "Donne-moi deux equipes. Ex : `France Japon`", parse_mode="Markdown")
        return
    await update.message.reply_text("Je cherche...")
    reply = build_reply(parts[0].strip(), parts[1].strip())
    await update.message.reply_text(reply, parse_mode="Markdown")
 
 
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("match", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("Bot demarre (mode polling).")
    app.run_polling()
 
 
if __name__ == "__main__":
    main()
