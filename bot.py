
# -*- coding: utf-8 -*-
"""
Bot Telegram prive - Cotes & probabilites foot (OddsPapi v4)
------------------------------------------------------------
Tu tapes deux equipes -> il sort les % implicites (1 / N / 2)
bases sur les cotes Pinnacle, pour comparer avec Polymarket.
 
Workflow OddsPapi reel :
  1. /tournaments      -> liste des tournois (id + nom)
  2. /odds-by-tournaments?bookmaker=pinnacle&tournamentIds=...
                       -> fixtures + cotes en un appel
  3. /participants     -> resolution des noms d'equipes (id -> nom)
 
Prive : ne repond qu'a TON chat ID.
Variables Railway : TELEGRAM_TOKEN, OWNER_CHAT_ID, ODDSPAPI_KEY
"""
 
import os
import logging
import requests
from telegram import Update
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes)
 
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_CHAT_ID  = int(os.environ["OWNER_CHAT_ID"])
ODDSPAPI_KEY   = os.environ["ODDSPAPI_KEY"]
 
BASE_URL = "https://api.oddspapi.io/v4"
SPORT_ID = 10
MARKET_1X2 = "101"
BOOKMAKER = "pinnacle"   # cotes de reference, les plus justes
MAX_TOURNAMENTS = 40     # on scanne les tournois actifs les plus charges
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("footbot")
 
# cache simple des noms de participants (id -> nom) pour la session
_PART_CACHE = {}
 
 
def is_owner(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID
 
 
def implied_probs(oh, od, oa):
    ih, id_, ia = 1/oh, 1/od, 1/oa
    over = ih + id_ + ia
    return (round(ih/over*100, 1), round(id_/over*100, 1),
            round(ia/over*100, 1), round((over-1)*100, 1))
 
 
# ---------- Tournois actifs (avec des matchs a venir) ----------
def get_active_tournaments():
    r = requests.get(f"{BASE_URL}/tournaments", params={
        "apiKey": ODDSPAPI_KEY, "sportId": SPORT_ID,
    }, timeout=20)
    r.raise_for_status()
    tours = r.json()
    # on garde ceux qui ont des matchs a venir ou en cours, tries par volume
    active = [t for t in tours
              if t.get("futureFixtures", 0) or t.get("upcomingFixtures", 0)
              or t.get("liveFixtures", 0)]
    active.sort(key=lambda t: (t.get("upcomingFixtures", 0)
                               + t.get("liveFixtures", 0)
                               + t.get("futureFixtures", 0)), reverse=True)
    return active[:MAX_TOURNAMENTS]
 
 
# ---------- Resolution des noms d'equipes ----------
def resolve_name(pid):
    if pid in _PART_CACHE:
        return _PART_CACHE[pid]
    try:
        r = requests.get(f"{BASE_URL}/participants", params={
            "apiKey": ODDSPAPI_KEY, "sportId": SPORT_ID,
            "participantIds": pid,
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            name = data[0].get("participantName") or data[0].get("name") or str(pid)
        elif isinstance(data, dict):
            name = data.get("participantName") or data.get("name") or str(pid)
        else:
            name = str(pid)
    except Exception:
        name = str(pid)
    _PART_CACHE[pid] = name
    return name
 
 
# ---------- Recherche du match + cotes ----------
def find_match(team_a, team_b):
    a, b = team_a.lower().strip(), team_b.lower().strip()
    tournaments = get_active_tournaments()
    ids = ",".join(str(t["tournamentId"]) for t in tournaments)
 
    r = requests.get(f"{BASE_URL}/odds-by-tournaments", params={
        "apiKey": ODDSPAPI_KEY, "bookmaker": BOOKMAKER, "tournamentIds": ids,
    }, timeout=30)
    r.raise_for_status()
    fixtures = r.json()
 
    for fx in fixtures:
        n1 = resolve_name(fx.get("participant1Id"))
        n2 = resolve_name(fx.get("participant2Id"))
        l1, l2 = n1.lower(), n2.lower()
        if (a in l1 and b in l2) or (b in l1 and a in l2):
            return fx, n1, n2
    return None, None, None
 
 
def extract_1x2(fx):
    book = fx.get("bookmakerOdds", {}).get(BOOKMAKER)
    if not book:
        return None
    market = book.get("markets", {}).get(MARKET_1X2)
    if not market:
        return None
    out = market.get("outcomes", {})
    def price(oid):
        players = out.get(oid, {}).get("players", {})
        for _, p in players.items():
            if p.get("price"):
                return p["price"]
        return None
    oh, od, oa = price("101"), price("102"), price("103")
    if not (oh and od and oa):
        return None
    return oh, od, oa
 
 
def build_reply(team_a, team_b):
    try:
        fx, n1, n2 = find_match(team_a, team_b)
    except Exception as e:
        return f"!! Erreur API : {e}"
 
    if not fx:
        return ("Aucun match trouve entre ces deux equipes dans les tournois "
                "actifs.\nVerifie l'orthographe ou essaie un nom plus court.")
 
    league_start = fx.get("startTime", "")
    odds = extract_1x2(fx)
    if not odds:
        return (f"Match trouve : {n1} vs {n2}\n"
                f"Mais pas de cotes 1X2 Pinnacle dispo pour l'instant.")
 
    oh, od, oa = odds
    ph, pd, pa, marge = implied_probs(oh, od, oa)
    return (
        f"*{n1} vs {n2}*\n"
        f"_{league_start}_\n\n"
        f"{n1} : *{ph}%*  (cote {oh:.2f})\n"
        f"Nul : *{pd}%*  (cote {od:.2f})\n"
        f"{n2} : *{pa}%*  (cote {oa:.2f})\n\n"
        f"_Source Pinnacle, marge retiree {marge}%_\n"
        f"-> Compare avec le prix Polymarket."
    )
 
 
# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Bot cotes foot pret.\n\nTape deux equipes :\n"
        "`France Japon`  ou  `/match France Japon`",
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
            "Donne-moi deux equipes. Ex : `France Japon`",
            parse_mode="Markdown")
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
