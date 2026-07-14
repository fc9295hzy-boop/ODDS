# -*- coding: utf-8 -*-
"""
Bot Telegram prive - Probabilites foot + tennis (odds-api.io)
----------------------------------------------------
Tu tapes deux equipes/joueurs -> % implicites (1/N/2 ou 1/2) a partir des
cotes reelles des bookmakers, pour comparer avec Polymarket.
 
Foot   : `France Japon`            ou  `/match France Japon`
Tennis : `tennis Alcaraz Sinner`   ou  `/tennis Alcaraz Sinner`
 
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
    """Si pas de cote de nul (od=None), calcul a 2 issues.
    Le tennis n'a jamais de nul -> passe toujours par cette branche."""
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
def get_events(sport="football"):
    r = requests.get(f"{BASE_URL}/events", params={
        "apiKey": ODDSAPIIO_KEY, "sport": sport,
    }, timeout=25)
    r.raise_for_status()
    return r.json()
 
 
def find_event(team_a, team_b, sport="football"):
    a, b = team_a.lower().strip(), team_b.lower().strip()
    events = get_events(sport)
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
          'odds':[{'home','draw','away'}] }, ... ], ... }
    En tennis, 'draw' est simplement absent -> geree nativement."""
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
def build_reply(name_a, name_b, sport="football"):
    label = "match" if sport == "football" else "joueurs"
    try:
        ev = find_event(name_a, name_b, sport)
    except Exception as e:
        return f"!! Erreur recherche {label} : {e}"
    if not ev:
        return (f"Aucun {label} a venir trouve entre ces deux {label}.\n"
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
        return (f"{label.capitalize()} trouve : {home} vs {away} ({league})\n"
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
 
 
# ---------- Parsing texte libre en deux noms ----------
def split_two_names(text):
    parts = None
    for sep in (" vs ", " VS ", " - ", " / ", "-"):
        if sep in text:
            parts = text.split(sep, 1)
            break
    if parts is None:
        parts = text.split(None, 1)
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip(), parts[1].strip()
 
 
# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Bot proba foot + tennis pret (odds-api.io).\n\n"
        "Foot   : `France Japon`  ou  `/match France Japon`\n"
        "Tennis : `tennis Alcaraz Sinner`  ou  `/tennis Alcaraz Sinner`",
        parse_mode="Markdown")
 
 
async def handle_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Foot uniquement, via /match."""
    if not is_owner(update):
        return
    text = (update.message.text or "").replace("/match", "").strip()
    names = split_two_names(text)
    if not names:
        await update.message.reply_text(
            "Donne-moi deux equipes. Ex : `France Japon`", parse_mode="Markdown")
        return
    await update.message.reply_text("Je cherche...")
    reply = build_reply(*names, sport="football")
    await update.message.reply_text(reply, parse_mode="Markdown")
 
 
async def handle_tennis(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tennis uniquement, via /tennis."""
    if not is_owner(update):
        return
    text = (update.message.text or "").replace("/tennis", "").strip()
    names = split_two_names(text)
    if not names:
        await update.message.reply_text(
            "Donne-moi deux joueurs. Ex : `Alcaraz Sinner`", parse_mode="Markdown")
        return
    await update.message.reply_text("Je cherche...")
    reply = build_reply(*names, sport="tennis")
    await update.message.reply_text(reply, parse_mode="Markdown")
 
 
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Texte libre : prefixe 'tennis ' -> tennis, sinon foot par defaut."""
    if not is_owner(update):
        return
    text = (update.message.text or "").strip()
 
    sport = "football"
    if text.lower().startswith("tennis"):
        sport = "tennis"
        text = text[len("tennis"):].strip()
 
    names = split_two_names(text)
    if not names:
        await update.message.reply_text(
            "Donne-moi deux noms. Ex : `France Japon` ou `tennis Alcaraz Sinner`",
            parse_mode="Markdown")
        return
    await update.message.reply_text("Je cherche...")
    reply = build_reply(*names, sport=sport)
    await update.message.reply_text(reply, parse_mode="Markdown")
 
 
async def post_init(app: Application):
    """
    Appele automatiquement par PTB une fois le bot demarre.
    Lance le tracker (steam move detection) EN TACHE DE FOND, dans ce meme
    process/service Railway -> pas de 2e service, budget $5/mois respecte.
    Import local pour eviter un import circulaire (tracker.py importe bot.py).
    """
    try:
        from tracker import start_tracking
        await start_tracking()
        log.info("Tracker foot+tennis demarre en tache de fond.")
    except Exception as e:
        log.error(f"Impossible de demarrer le tracker : {e}")
 
 
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("match", handle_match))
    app.add_handler(CommandHandler("tennis", handle_tennis))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot demarre (mode polling).")
    app.run_polling()
 
 
if __name__ == "__main__":
    main()
