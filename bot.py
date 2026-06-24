# -*- coding: utf-8 -*-
"""
Bot Telegram prive - Cotes & probabilites foot (OddsPapi v4)
------------------------------------------------------------
Tu tapes deux equipes -> % implicites (1/N/2) via cotes Pinnacle.
Recherche par paquets de tournois pour respecter la limite API.
 
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
BOOKMAKER = "pinnacle"
BATCH = 5            # nb de tournois par requete odds-by-tournaments
MAX_BATCHES = 8      # plafond de requetes par recherche (protege le quota)
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("footbot")
 
_PART_CACHE = {}
 
 
def is_owner(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID
 
 
def implied_probs(oh, od, oa):
    ih, id_, ia = 1/oh, 1/od, 1/oa
    over = ih + id_ + ia
    return (round(ih/over*100, 1), round(id_/over*100, 1),
            round(ia/over*100, 1), round((over-1)*100, 1))
 
 
def get_active_tournaments():
    r = requests.get(f"{BASE_URL}/tournaments", params={
        "apiKey": ODDSPAPI_KEY, "sportId": SPORT_ID,
    }, timeout=20)
    r.raise_for_status()
    tours = r.json()
    active = [t for t in tours
              if (t.get("futureFixtures", 0) or t.get("upcomingFixtures", 0)
                  or t.get("liveFixtures", 0))]
    # priorite aux tournois avec des matchs imminents (upcoming + live)
    active.sort(key=lambda t: (t.get("upcomingFixtures", 0)
                               + t.get("liveFixtures", 0)), reverse=True)
    return active
 
 
def resolve_name(pid):
    if pid in _PART_CACHE:
        return _PART_CACHE[pid]
    name = str(pid)
    try:
        r = requests.get(f"{BASE_URL}/participants", params={
            "apiKey": ODDSPAPI_KEY, "sportId": SPORT_ID,
            "participantIds": pid,
        }, timeout=15)
        if r.status_code == 200:
            data = r.json()
            item = data[0] if isinstance(data, list) and data else (
                data if isinstance(data, dict) else {})
            name = item.get("participantName") or item.get("name") or str(pid)
    except Exception:
        pass
    _PART_CACHE[pid] = name
    return name
 
 
def scan_batch(tournament_ids, a, b):
    """Cherche le match dans un paquet de tournois. Renvoie (fx,n1,n2) ou None."""
    ids = ",".join(str(i) for i in tournament_ids)
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
    return None
 
 
def find_match(team_a, team_b):
    a, b = team_a.lower().strip(), team_b.lower().strip()
    tournaments = get_active_tournaments()
    ids = [t["tournamentId"] for t in tournaments]
 
    batches_done = 0
    for i in range(0, len(ids), BATCH):
        if batches_done >= MAX_BATCHES:
            break
        chunk = ids[i:i+BATCH]
        try:
            found = scan_batch(chunk, a, b)
        except Exception as e:
            log.warning("batch %s erreur: %s", chunk, e)
            found = None
        batches_done += 1
        if found:
            return found
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
        for _, p in out.get(oid, {}).get("players", {}).items():
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
        return ("Aucun match trouve entre ces deux equipes parmi les "
                "tournois actifs scannes.\nVerifie l'orthographe, ou le match "
                "est peut-etre dans un tournoi hors des plus actifs.")
    start = fx.get("startTime", "")
    odds = extract_1x2(fx)
    if not odds:
        return f"Match trouve : {n1} vs {n2}\nMais pas de cotes 1X2 Pinnacle dispo."
    oh, od, oa = odds
    ph, pd, pa, marge = implied_probs(oh, od, oa)
    return (
        f"*{n1} vs {n2}*\n"
        f"_{start}_\n\n"
        f"{n1} : *{ph}%*  (cote {oh:.2f})\n"
        f"Nul : *{pd}%*  (cote {od:.2f})\n"
        f"{n2} : *{pa}%*  (cote {oa:.2f})\n\n"
        f"_Source Pinnacle, marge retiree {marge}%_\n"
        f"-> Compare avec le prix Polymarket."
    )
 
 
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
