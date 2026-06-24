
# -*- coding: utf-8 -*-
"""
Bot Telegram prive - Probabilites foot (odds-api.io)
----------------------------------------------------
Tu tapes deux equipes -> % implicites (1/N/2) a partir des cotes
reelles des bookmakers, pour comparer avec Polymarket.
 
Quota gratuit : 100 requetes / heure (large).
Workflow : /v3/events (trouver le match) puis /v3/odds (cotes).
 
Variables Railway :
  TELEGRAM_TOKEN, OWNER_CHAT_ID, ODDSAPIIO_KEY
"""
 
import os
import logging
from datetime import datetime, timezone
import requests
from telegram import Update
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes)
 
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_CHAT_ID  = int(os.environ["OWNER_CHAT_ID"])
ODDSAPIIO_KEY  = os.environ["ODDSAPIIO_KEY"]
 
BASE_URL = "https://api.odds-api.io/v3"
# Tes bookmakers selectionnes sur le compte (plan gratuit = 2).
# A ajuster avec tes books reels via la variable BOOKMAKERS sur Railway.
BOOKMAKERS = os.environ.get("BOOKMAKERS", "Bet365,Unibet")
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("footbot")
 
 
def is_owner(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID
 
 
# ---------- Cotes -> probabilites ----------
def implied_probs(oh, od, oa):
    """Si pas de cote de nul (od=None), calcul a 2 issues."""
    if od:
        ih, idr, ia = 1/oh, 1/od, 1/oa
        over = ih + idr + ia
        return (round(ih/over*100, 1), round(idr/over*100, 1),
                round(ia/over*100, 1), round((over-1)*100, 1))
    ih, ia = 1/oh, 1/oa
    over = ih + ia
    return (round(ih/over*100, 1), None,
            round(ia/over*100, 1), round((over-1)*100, 1))
 
 
# ---------- Recherche du match ----------
def get_events():
    r = requests.get(f"{BASE_URL}/events", params={
        "apiKey": ODDSAPIIO_KEY, "sport": "football",
    }, timeout=25)
    r.raise_for_status()
    return r.json()
 
 
def find_event(team_a, team_b):
    a, b = team_a.lower().strip(), team_b.lower().strip()
    events = get_events()
    if isinstance(events, dict):
        events = events.get("events", events.get("data", []))
    for ev in events:
        home = str(ev.get("home", "")).lower()
        away = str(ev.get("away", "")).lower()
        if (a in home and b in away) or (b in home and a in away):
            return ev
    return None
 
 
# ---------- Cotes d'un match ----------
def get_odds(event_id):
    r = requests.get(f"{BASE_URL}/odds", params={
        "apiKey": ODDSAPIIO_KEY, "eventId": event_id,
        "bookmakers": BOOKMAKERS,
    }, timeout=25)
    r.raise_for_status()
    return r.json()
 
 
def avg_1x2(odds_json):
    """Moyenne les cotes ML (Match Winner) sur les bookmakers dispo.
    Structure odds-api.io confirmee :
      odds_json['bookmakers'] = { 'Bet365': [ {'name':'ML',
          'odds':[{'home','draw','away'}] }, ... ], ... }"""
    books = odds_json.get("bookmakers", {})
    if not isinstance(books, dict):
        return None
 
    homes, draws, aways = [], [], []
    for book_name, markets in books.items():
        if not isinstance(markets, list):
            continue
        for m in markets:
            if str(m.get("name", "")).upper() == "ML":
                for o in m.get("odds", []):
                    try:
                        if o.get("home"):
                            homes.append(float(o["home"]))
                        if o.get("draw"):
                            draws.append(float(o["draw"]))
                        if o.get("away"):
                            aways.append(float(o["away"]))
                    except (ValueError, TypeError):
                        pass
    if not (homes and aways):
        return None
    avg = lambda l: sum(l) / len(l)
    od = avg(draws) if draws else None
    return avg(homes), od, avg(aways), len(homes)
 
 
# ---------- Construction reponse ----------
def build_reply(team_a, team_b):
    try:
        ev = find_event(team_a, team_b)
    except Exception as e:
        return f"!! Erreur recherche match : {e}"
    if not ev:
        return ("Aucun match a venir trouve entre ces deux equipes.\n"
                "Verifie l'orthographe (essaie les noms anglais).")
 
    home = ev.get("home", "?")
    away = ev.get("away", "?")
    league = ev.get("league", {})
    league = league.get("name", "") if isinstance(league, dict) else str(league)
    date = str(ev.get("date", ""))[:16].replace("T", " ")
    eid = ev.get("id")
 
    try:
        odds_json = get_odds(eid)
    except Exception as e:
        return f"!! Erreur recuperation cotes : {e}"
 
    res = avg_1x2(odds_json)
    if not res:
        return (f"Match trouve : {home} vs {away} ({league})\n"
                "Mais pas de cotes Match Winner dispo pour l'instant.")
 
    oh, od, oa, nb = res
    ph, pd, pa, marge = implied_probs(oh, od, oa)
    msg = (f"*{home} vs {away}*\n"
           f"_{league}_  -  {date}\n\n"
           f"{home} : *{ph}%*  (cote {oh:.2f})\n")
    if pd is not None:
        msg += f"Nul : *{pd}%*  (cote {od:.2f})\n"
    msg += (f"{away} : *{pa}%*  (cote {oa:.2f})\n\n"
            f"_Moyenne sur {nb} bookmaker(s), marge retiree {marge}%_\n"
            f"-> Compare avec le prix Polymarket.")
    return msg
 
 
# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Bot proba foot pret (odds-api.io).\n\n"
        "Tape deux equipes :\n`France Japon`  ou  `/match France Japon`",
        parse_mode="Markdown")
 
 
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
    app.add_handler(CommandHandler("match", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("Bot demarre (mode polling).")
    app.run_polling()
 
 
if __name__ == "__main__":
    main()
