# -*- coding: utf-8 -*-
"""
Bot Telegram privé — Cotes & probabilités foot (OddsPapi)
---------------------------------------------------------
Usage : tu tapes deux équipes, le bot sort les % implicites
        (proba de victoire / nul / victoire) pour comparer
        avec ton bot Polymarket.

Privé : ne répond qu'à TON chat ID. Ignore tout le monde d'autre.

Variables d'environnement à définir sur Railway :
  - TELEGRAM_TOKEN   : le token de CE bot (créé via @BotFather)
  - OWNER_CHAT_ID    : ton chat ID Telegram (chiffre)
  - ODDSPAPI_KEY     : ta clé gratuite oddspapi.io
"""

import os
import logging
import requests
from telegram import Update
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes)

# ---------- Config ----------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OWNER_CHAT_ID  = int(os.environ["OWNER_CHAT_ID"])
ODDSPAPI_KEY   = os.environ["ODDSPAPI_KEY"]

BASE_URL = "https://api.oddspapi.io/v4"
SPORT_ID = 10  # football/soccer chez OddsPapi

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("footbot")


# ---------- Sécurité : filtre privé ----------
def is_owner(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.id == OWNER_CHAT_ID


# ---------- Logique cotes -> probabilités ----------
def implied_probs(odd_home: float, odd_draw: float, odd_away: float):
    """
    Convertit des cotes décimales en probabilités implicites,
    en retirant la marge du bookmaker (normalisation à 100 %).
    """
    inv_h = 1.0 / odd_home
    inv_d = 1.0 / odd_draw
    inv_a = 1.0 / odd_away
    overround = inv_h + inv_d + inv_a  # > 1 = marge bookmaker
    return (
        round(inv_h / overround * 100, 1),
        round(inv_d / overround * 100, 1),
        round(inv_a / overround * 100, 1),
        round((overround - 1) * 100, 1),  # marge en %
    )


# ---------- Appel OddsPapi ----------
def fetch_match(team_a: str, team_b: str):
    """
    Cherche un match à venir entre deux équipes (dans les 2 semaines)
    et renvoie un dict simplifié, ou None si rien trouvé.

    NOTE : la structure exacte des champs OddsPapi est à confirmer
    avec leur doc une fois la clé en main. Les noms de champs ci-dessous
    sont une base raisonnable à ajuster au premier test réel.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/fixtures",
            params={"apiKey": ODDSPAPI_KEY, "sportId": SPORT_ID},
            timeout=15,
        )
        resp.raise_for_status()
        fixtures = resp.json()
    except Exception as e:
        log.error("Erreur appel OddsPapi: %s", e)
        return {"error": str(e)}

    a = team_a.lower().strip()
    b = team_b.lower().strip()

    for fx in fixtures:
        home = str(fx.get("homeTeam", "")).lower()
        away = str(fx.get("awayTeam", "")).lower()
        # match si les deux équipes citées apparaissent (ordre indifférent)
        if (a in home and b in away) or (a in away and b in home) \
           or (b in home and a in away) or (b in away and a in home):
            return _extract_odds(fx)

    return None


def _extract_odds(fx: dict):
    """Extrait les cotes 1X2 du fixture. À ajuster selon la vraie structure."""
    home = fx.get("homeTeam", "?")
    away = fx.get("awayTeam", "?")
    league = fx.get("tournamentName", "")
    start = fx.get("startTime", "")

    # Recherche du marché 1X2 (h2h). Structure indicative.
    odd_h = odd_d = odd_a = None
    for market in fx.get("markets", []):
        if market.get("name", "").lower() in ("1x2", "h2h", "match winner"):
            for o in market.get("outcomes", []):
                name = str(o.get("name", "")).lower()
                price = o.get("price")
                if name in ("1", "home", home.lower()):
                    odd_h = price
                elif name in ("x", "draw", "nul"):
                    odd_d = price
                elif name in ("2", "away", away.lower()):
                    odd_a = price
            break

    return {
        "home": home, "away": away, "league": league, "start": start,
        "odd_h": odd_h, "odd_d": odd_d, "odd_a": odd_a,
    }


# ---------- Formatage message ----------
def format_reply(m: dict) -> str:
    if m is None:
        return ("❌ Aucun match trouvé entre ces deux équipes dans les "
                "rencontres à venir.\nVérifie l'orthographe ou essaie un nom "
                "plus court (ex: « Real » au lieu de « Real Madrid CF »).")
    if "error" in m:
        return f"⚠️ Erreur API : {m['error']}"
    if not all([m["odd_h"], m["odd_d"], m["odd_a"]]):
        return (f"🔎 Match trouvé : {m['home']} vs {m['away']} "
                f"({m['league']})\nMais les cotes 1X2 ne sont pas encore "
                f"disponibles pour ce match.")

    ph, pd, pa, marge = implied_probs(m["odd_h"], m["odd_d"], m["odd_a"])
    return (
        f"⚽ *{m['home']} vs {m['away']}*\n"
        f"_{m['league']}_  —  {m['start']}\n\n"
        f"🏠 {m['home']} : *{ph}%*  (cote {m['odd_h']})\n"
        f"🤝 Nul : *{pd}%*  (cote {m['odd_d']})\n"
        f"🚌 {m['away']} : *{pa}%*  (cote {m['odd_a']})\n\n"
        f"_Marge bookmaker retirée : {marge}%_\n"
        f"➡️ Compare ces % avec le prix Polymarket."
    )


# ---------- Handlers Telegram ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.message.reply_text(
        "Bot cotes foot prêt 👋\n\n"
        "Tape simplement deux équipes :\n"
        "`France Japon`\n"
        "ou `/match France Japon`\n\n"
        "Je te sors les % implicites pour comparer à Polymarket.",
        parse_mode="Markdown",
    )


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return  # ignore silencieusement tout autre utilisateur

    text = update.message.text or ""
    text = text.replace("/match", "").strip()

    # Sépare deux équipes : « France Japon », « France vs Japon », « France - Japon »
    for sep in (" vs ", " VS ", " - ", " / ", "-"):
        if sep in text:
            parts = text.split(sep, 1)
            break
    else:
        parts = text.split(None, 1)

    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        await update.message.reply_text(
            "Donne-moi deux équipes. Ex : `France Japon`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("🔎 Je cherche…")
    m = fetch_match(parts[0], parts[1])
    await update.message.reply_text(format_reply(m), parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("match", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("Bot démarré (mode polling).")
    app.run_polling()


if __name__ == "__main__":
    main()
