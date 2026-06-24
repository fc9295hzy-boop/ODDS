
# -*- coding: utf-8 -*-
"""
Bot Telegram prive - Cotes & probabilites foot (OddsPapi v4)
------------------------------------------------------------
Tu tapes deux equipes -> il sort les % implicites (1 / N / 2)
moyennes sur les bookmakers, pour comparer avec Polymarket.
 
Prive : ne repond qu'a TON chat ID.
 
Variables d'environnement (Railway) :
  TELEGRAM_TOKEN, OWNER_CHAT_ID, ODDSPAPI_KEY
"""
 
import os
import logging
from datetime import datetime, timedelta
import requests
from telegram import Update
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes)
 
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_CHAT_ID  = int(os.environ["OWNER_CHAT_ID"])
ODDSPAPI_KEY   = os.environ["ODDSPAPI_KEY"]
 
BASE_URL = "https://api.oddspapi.io/v4"
SPORT_ID = 10
MARKET_1X2 = "101"   # Full Time Result : 101=Home, 102=Draw, 103=Away
SEARCH_DAYS = 14     # fenetre de recherche des matchs a venir
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("footbot")
 
 
def is_owner(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID
 
 
# ---------- Probabilites ----------
def implied_probs(odd_h, odd_d, odd_a):
    inv_h, inv_d, inv_a = 1/odd_h, 1/odd_d, 1/odd_a
    over = inv_h + inv_d + inv_a
    return (round(inv_h/over*100, 1), round(inv_d/over*100, 1),
            round(inv_a/over*100, 1), round((over-1)*100, 1))
 
 
# ---------- Etape 1 : trouver le fixture ----------
def find_fixture(team_a, team_b):
    today = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=SEARCH_DAYS)).strftime("%Y-%m-%d")
    r = requests.get(f"{BASE_URL}/fixtures", params={
        "apiKey": ODDSPAPI_KEY, "sportId": SPORT_ID,
        "from": today, "to": end,
    }, timeout=20)
    r.raise_for_status()
    fixtures = r.json()
 
    a, b = team_a.lower().strip(), team_b.lower().strip()
    for fx in fixtures:
        p1 = str(fx.get("participant1Name", "")).lower()
        p2 = str(fx.get("participant2Name", "")).lower()
        if (a in p1 and b in p2) or (b in p1 and a in p2):
            return fx
    return None
 
 
# ---------- Etape 2 : recuperer + moyenner les cotes 1X2 ----------
def get_avg_1x2(fixture_id):
    r = requests.get(f"{BASE_URL}/odds", params={
        "apiKey": ODDSPAPI_KEY, "fixtureId": fixture_id,
    }, timeout=20)
    r.raise_for_status()
    data = r.json()
 
    homes, draws, aways = [], [], []
    for slug, bookie in data.get("bookmakerOdds", {}).items():
        market = bookie.get("markets", {}).get(MARKET_1X2)
        if not market:
            continue
        for outcome_id, outcome in market.get("outcomes", {}).items():
            for _, player in outcome.get("players", {}).items():
                price = player.get("price")
                if not price:
                    continue
                if outcome_id == "101":
                    homes.append(price)
                elif outcome_id == "102":
                    draws.append(price)
                elif outcome_id == "103":
                    aways.append(price)
 
    if not (homes and draws and aways):
        return None
    avg = lambda lst: sum(lst) / len(lst)
    return avg(homes), avg(draws), avg(aways), len(homes)
 
 
# ---------- Formatage ----------
def build_reply(team_a, team_b):
    try:
        fx = find_fixture(team_a, team_b)
    except Exception as e:
        return f"!! Erreur recherche match : {e}"
 
    if not fx:
        return ("Aucun match trouve entre ces deux equipes dans les "
                f"{SEARCH_DAYS} prochains jours.\nEssaie un nom plus court "
                "(ex: Real au lieu de Real Madrid CF).")
 
    p1 = fx.get("participant1Name", "?")
    p2 = fx.get("participant2Name", "?")
    league = fx.get("tournamentName", "")
    start = fx.get("startTime", fx.get("startDate", ""))
    fid = fx.get("fixtureId")
 
    try:
        res = get_avg_1x2(fid)
    except Exception as e:
        return f"!! Erreur recuperation cotes : {e}"
 
    if not res:
        return (f"Match trouve : {p1} vs {p2} ({league})\n"
                "Mais pas de cotes 1X2 disponibles pour l'instant.")
 
    oh, od, oa, nbooks = res
    ph, pd, pa, marge = implied_probs(oh, od, oa)
    return (
        f"*{p1} vs {p2}*\n"
        f"_{league}_  -  {start}\n\n"
        f"{p1} : *{ph}%*  (cote moy. {oh:.2f})\n"
        f"Nul : *{pd}%*  (cote moy. {od:.2f})\n"
        f"{p2} : *{pa}%*  (cote moy. {oa:.2f})\n\n"
        f"_Moyenne sur {nbooks} bookmakers, marge retiree {marge}%_\n"
        f"-> Compare avec le prix Polymarket."
    )
 
 
# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Bot cotes foot pret.\n\nTape deux equipes :\n"
        "`France Japon`  ou  `/match France Japon`",
        parse_mode="Markdown",
    )
 
 
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
