import asyncio
import aiohttp
import json
from datetime import datetime, time
from collections import defaultdict

# ============================================================
# CONFIG
# ============================================================
TELEGRAM_BOT_TOKEN = "8868714626:AAHhSU2GkIW0jdIao1I9ZvKFx71tAJtMAco"
TELEGRAM_CHAT_ID = "7360478330"

# Token de session Stake (x-access-token du navigateur)
# A renouveler quand il expire (deconnexion de Stake)
STAKE_SESSION_TOKEN = "edd0aade282af360a96023fdb4037702918585c2ee236b60c5e5f8f2b03bc2ccf0788b8cff2d3b041561fd5248d60588"

# ============================================================
# FILTRES PAR SPORT
# ============================================================
SPORT_CONFIG = {
    "football":     {"emoji": "⚽", "label": "Football",       "min_stake": 10000, "exact_min_stake": 100, "active": True},
    "tennis":       {"emoji": "🎾", "label": "Tennis",          "min_stake": 8000,  "exact_min_stake": 100, "active": True},
    "tennis-table": {"emoji": "🏓", "label": "Tennis de table", "min_stake": 2000,  "exact_min_stake": 100, "active": True},
    "basketball":   {"emoji": "🏀", "label": "Basketball",      "min_stake": 10000, "exact_min_stake": 100, "active": True},
    "handball":     {"emoji": "🤾", "label": "Handball",        "min_stake": 10000, "exact_min_stake": 100, "active": True},
}

# ============================================================
# ETAT GLOBAL DU BOT
# ============================================================
bot_state = {
    # Controle
    "paused": False,
    "min_odd": 1.75,
    "last_update_id": 0,
    # Regles Telegram
    "exact_min_stake": 1000,
    "bigodds_min_stake": 7000,
    "bigodds_min_odd": 15,
    # Menu VIP
    "waiting_add_vip": False,
    "waiting_remove_vip": False,
    "waiting_min_odd": False,
    # Modification montants
    "waiting_stake_sport": None,      # sport en attente de modification
    "waiting_exact_stake": False,
    "waiting_bigodds_stake": False,
    "waiting_new_token": False,
    # Historique scans page
    "history_page": 0,
    # Session
    "scan_count": 0,
    "total_sent": 0,
    "sample_index": 0,
    "session_start": datetime.now(),
}

# ============================================================
# DONNEES EN MEMOIRE
# ============================================================
VIP_USERS = []
seen_ids = set()

# Statistiques
stats = {
    "total_detected": 0,
    "total_sent_telegram": 0,
    "vip_bets": defaultdict(int),        # user -> nb paris
    "vip_wins": defaultdict(int),        # user -> nb wins (simulé)
    "history": [],                        # liste des 50 derniers paris
    "daily_detected": 0,
    "daily_sent": 0,
    "daily_reset": datetime.now().date(),
}

# Intelligence - consensus
consensus_tracker = defaultdict(list)   # "match_pick" -> [timestamps]
odd_tracker = defaultdict(list)         # "match_pick" -> [odds over time]

# VIP alertes consecutives
vip_consecutive = defaultdict(int)      # user -> nb paris consecutifs recents

# ============================================================
# REGLES TELEGRAM
# ============================================================
def should_send_telegram(bet):
    if bet.get("exact") and bet.get("stake", 0) >= bot_state["exact_min_stake"]:
        return True, "Regle 1 - Score Exact"
    if bet.get("odd", 0) >= bot_state["bigodds_min_odd"] and bet.get("stake", 0) >= bot_state["bigodds_min_stake"]:
        return True, f"Regle 2 - Grosse Cote (>={bot_state['bigodds_min_odd']})"
    return False, None

def is_vip_user(bet):
    user = bet.get("user", "")
    return user and user != "Anonymous" and user.lower() in [v.lower() for v in VIP_USERS]

def matches_filters(bet):
    sport = bet.get("sport", "")
    if sport not in SPORT_CONFIG:
        return False
    cfg = SPORT_CONFIG[sport]
    if not cfg.get("active", True):
        return False
    odd = bet.get("odd", 0)
    stake = bet.get("stake", 0)
    is_exact = bet.get("exact", False)
    if odd < bot_state["min_odd"]:
        return False
    min_stake = cfg["exact_min_stake"] if is_exact else cfg["min_stake"]
    if stake < min_stake:
        return False
    return True

# ============================================================
# DETECTION MATCHS SUSPECTS
# ============================================================
def check_suspicious(bet):
    suspicion_score = 0
    reasons = []

    key = f"{bet.get('match', '')}_{bet.get('pick', '')}"

    # Critere 2 : meme pronostic par plusieurs parieurs
    consensus_tracker[key].append(datetime.now())
    recent = [t for t in consensus_tracker[key] if (datetime.now() - t).seconds < 300]
    consensus_tracker[key] = recent
    count = len(recent)
    if count >= 5:
        suspicion_score += 2
        reasons.append(f"👥 {count} parieurs sur le meme pronostic (5 min)")
    elif count >= 3:
        suspicion_score += 1
        reasons.append(f"👥 {count} parieurs sur le meme pronostic")

    # Critere 3 : mise anormalement elevee vs cote
    stake = bet.get("stake", 0)
    odd = bet.get("odd", 0)
    if odd < 2.0 and stake > 50000:
        suspicion_score += 1
        reasons.append(f"💰 Mise tres elevee (${stake:,}) sur cote faible (x{odd})")
    if odd > 10 and stake > 20000:
        suspicion_score += 2
        reasons.append(f"💰 Mise anormale (${stake:,}) sur grosse cote (x{odd})")

    # Critere 4 : chute de cote rapide
    odd_tracker[key].append({"odd": odd, "time": datetime.now()})
    recent_odds = [o for o in odd_tracker[key] if (datetime.now() - o["time"]).seconds < 600]
    odd_tracker[key] = recent_odds
    if len(recent_odds) >= 2:
        first_odd = recent_odds[0]["odd"]
        last_odd = recent_odds[-1]["odd"]
        if first_odd > 0 and last_odd > 0:
            drop = (first_odd - last_odd) / first_odd * 100
            if drop >= 30:
                suspicion_score += 2
                reasons.append(f"📉 Cote chutee de {drop:.0f}% en 10 min (x{first_odd} -> x{last_odd})")
            elif drop >= 20:
                suspicion_score += 1
                reasons.append(f"📉 Cote chutee de {drop:.0f}% (x{first_odd} -> x{last_odd})")

    if suspicion_score == 0:
        return None

    if suspicion_score >= 4:
        level = "🔴 DANGER"
    elif suspicion_score >= 2:
        level = "🚨 TRES SUSPECT"
    else:
        level = "⚠️ SUSPECT"

    return {"level": level, "score": suspicion_score, "reasons": reasons}

# ============================================================
# DETECTION CONSENSUS
# ============================================================
def check_consensus(bet):
    key = f"{bet.get('match', '')}_{bet.get('pick', '')}"
    recent = [t for t in consensus_tracker.get(key, []) if (datetime.now() - t).seconds < 300]
    return len(recent)

# ============================================================
# DETECTION VIP CONSECUTIFS
# ============================================================
def check_vip_consecutive(user):
    vip_consecutive[user] += 1
    return vip_consecutive[user]

# ============================================================
# TELEGRAM - MESSAGES
# ============================================================
async def send_simple_message(session, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            return data.get("ok", False)
    except Exception as e:
        print(f"Message erreur: {e}")
    return False

async def answer_callback(session, callback_id, text=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        async with session.post(url, json={"callback_query_id": callback_id, "text": text}) as resp:
            pass
    except:
        pass

# ============================================================
# TELEGRAM - MENUS
# ============================================================
async def send_main_menu(session):
    status = "🟢 Actif" if not bot_state["paused"] else "🔴 En pause"
    text = (
        f"*StakeScan - Menu Principal*\n\n"
        f"Statut : {status}\n"
        f"Cote minimum : x{bot_state['min_odd']}\n"
        f"VIP configures : {len(VIP_USERS)}\n"
        f"Paris detectes : {stats['total_detected']}\n"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "⭐ Gerer les VIP",        "callback_data": "menu_vip"}],
            [{"text": "⚙️ Parametres",            "callback_data": "menu_settings"}],
            [{"text": "💰 Modifier les montants", "callback_data": "menu_stakes"}],
            [{"text": "📊 Statistiques",          "callback_data": "menu_stats"}],
            [{"text": "📋 Historique scans",      "callback_data": "menu_history_0"}],
            [{"text": "⏸ Pause" if not bot_state["paused"] else "▶️ Reprendre", "callback_data": "toggle_pause"}],
            [{"text": "🔑 Renouveler token Stake", "callback_data": "renew_token"}],
            [{"text": "🔙 Fermer",                "callback_data": "close_menu"}],
        ]
    }
    await send_simple_message(session, text, keyboard)

async def send_stakes_menu(session):
    text = (
        f"💰 *Modifier les montants minimum*\n\n"
        f"*Par sport :*\n"
        f"⚽ Football : ${SPORT_CONFIG['football']['min_stake']:,}\n"
        f"🎾 Tennis : ${SPORT_CONFIG['tennis']['min_stake']:,}\n"
        f"🏓 Tennis de table : ${SPORT_CONFIG['tennis-table']['min_stake']:,}\n"
        f"🏀 Basketball : ${SPORT_CONFIG['basketball']['min_stake']:,}\n"
        f"🤾 Handball : ${SPORT_CONFIG['handball']['min_stake']:,}\n\n"
        f"*Regles Telegram :*\n"
        f"🎯 Score exact : ${bot_state['exact_min_stake']:,}\n"
        f"💥 Grosse cote (x{bot_state['bigodds_min_odd']}) : ${bot_state['bigodds_min_stake']:,}\n"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "⚽ Modifier Football",      "callback_data": "stake_football"}],
            [{"text": "🎾 Modifier Tennis",         "callback_data": "stake_tennis"}],
            [{"text": "🏓 Modifier Tennis de table","callback_data": "stake_tennis-table"}],
            [{"text": "🏀 Modifier Basketball",     "callback_data": "stake_basketball"}],
            [{"text": "🤾 Modifier Handball",       "callback_data": "stake_handball"}],
            [{"text": "🎯 Modifier Score exact",    "callback_data": "stake_exact"}],
            [{"text": "💥 Modifier Grosse cote",    "callback_data": "stake_bigodds"}],
            [{"text": "🔙 Menu principal",          "callback_data": "open_menu"}],
        ]
    }
    await send_simple_message(session, text, keyboard)

async def send_history_menu(session, page=0):
    per_page = 10
    all_history = stats["history"]
    total = len(all_history)
    start = page * per_page
    end = min(start + per_page, total)
    page_bets = all_history[-(end) : -(start) if start > 0 else None][::-1] if total > 0 else []

    if not page_bets:
        await send_simple_message(session, "📋 Aucun scan effectue pour l'instant.")
        return

    lines = []
    for i, h in enumerate(page_bets, start + 1):
        cfg = SPORT_CONFIG.get(h.get("sport", ""), {"emoji": "🎲"})
        bet_id = format_bet_id(h.get("id", "N/A"))
        exact = "🎯" if h.get("exact") else ""
        live = "🔴" if h.get("live") else ""
        lines.append(
            f"{i}. {cfg['emoji']}{exact}{live} *{h.get('match', 'N/A')}*\n"
            f"    @{h.get('user','?')} | x{h.get('odd')} | ${h.get('stake',0):,}\n"
            f"    🆔 {bet_id}"
        )

    text = f"📋 *Historique scans ({total} total) — Page {page+1}*\n\n" + "\n\n".join(lines)

    nav_buttons = []
    if page > 0:
        nav_buttons.append({"text": "⬅️ Precedent", "callback_data": f"menu_history_{page-1}"})
    if end < total:
        nav_buttons.append({"text": "Suivant ➡️", "callback_data": f"menu_history_{page+1}"})

    keyboard = {"inline_keyboard": []}
    if nav_buttons:
        keyboard["inline_keyboard"].append(nav_buttons)
    keyboard["inline_keyboard"].append([{"text": "🔙 Menu principal", "callback_data": "open_menu"}])

    await send_simple_message(session, text, keyboard)

async def send_vip_menu(session):
    vip_count = len(VIP_USERS)
    text = f"⭐ *Gestion VIP*\n\n{vip_count} utilisateur(s) configure(s)\n\nQue veux-tu faire ?"
    keyboard = {
        "inline_keyboard": [
            [{"text": "➕ Ajouter VIP",    "callback_data": "add_vip"}],
            [{"text": "❌ Supprimer VIP",  "callback_data": "remove_vip"}],
            [{"text": "📋 Liste VIP",      "callback_data": "list_vip"}],
            [{"text": "🔙 Menu principal", "callback_data": "open_menu"}],
        ]
    }
    await send_simple_message(session, text, keyboard)

async def send_settings_menu(session):
    sports_lines = "\n".join([
        f"{'✅' if cfg['active'] else '❌'} {cfg['emoji']} {cfg['label']}"
        for cfg in SPORT_CONFIG.values()
    ])
    text = (
        f"⚙️ *Parametres*\n\n"
        f"Cote minimum : x{bot_state['min_odd']}\n\n"
        f"*Sports actifs :*\n{sports_lines}"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "📈 Modifier cote minimum",    "callback_data": "set_min_odd"}],
            [{"text": "⚽ Football",     "callback_data": "toggle_football"}],
            [{"text": "🎾 Tennis",       "callback_data": "toggle_tennis"}],
            [{"text": "🏓 Tennis table", "callback_data": "toggle_tennis-table"}],
            [{"text": "🏀 Basketball",   "callback_data": "toggle_basketball"}],
            [{"text": "🤾 Handball",     "callback_data": "toggle_handball"}],
            [{"text": "🔙 Menu principal","callback_data": "open_menu"}],
        ]
    }
    await send_simple_message(session, text, keyboard)

async def send_stats_menu(session):
    duration = datetime.now() - bot_state["session_start"]
    hours = int(duration.total_seconds() // 3600)
    mins = int((duration.total_seconds() % 3600) // 60)

    top_vip = sorted(stats["vip_bets"].items(), key=lambda x: x[1], reverse=True)[:5]
    top_lines = "\n".join([f"  ⭐ @{u} — {n} paris" for u, n in top_vip]) if top_vip else "  Aucun VIP suivi"

    text = (
        f"📊 *Statistiques*\n\n"
        f"⏱ Session : {hours}h {mins}min\n"
        f"🔍 Scans effectues : {bot_state['scan_count']}\n"
        f"🎯 Paris detectes : {stats['total_detected']}\n"
        f"✈️ Envoyes Telegram : {stats['total_sent_telegram']}\n\n"
        f"📅 *Aujourd'hui :*\n"
        f"  Detectes : {stats['daily_detected']}\n"
        f"  Envoyes : {stats['daily_sent']}\n\n"
        f"🏆 *Top parieurs VIP :*\n{top_lines}"
    )
    keyboard = {
        "inline_keyboard": [
            [{"text": "📋 Historique recent", "callback_data": "show_history"}],
            [{"text": "🔙 Menu principal",    "callback_data": "open_menu"}],
        ]
    }
    await send_simple_message(session, text, keyboard)

async def send_remove_vip_menu(session):
    if not VIP_USERS:
        await send_simple_message(session, "❌ Aucun utilisateur VIP a supprimer.")
        return
    buttons = [[{"text": f"❌ @{u}", "callback_data": f"del_{u}"}] for u in VIP_USERS]
    buttons.append([{"text": "🔙 Retour", "callback_data": "menu_vip"}])
    await send_simple_message(session, "Choisis l'utilisateur a supprimer :", {"inline_keyboard": buttons})

# ============================================================
# TELEGRAM - NOTIFICATIONS PARIS
# ============================================================
def build_selections_text(bet, cfg):
    msg = ""
    selections = bet.get("selections", [])
    if selections and len(selections) > 1:
        for i, sel in enumerate(selections, 1):
            sel_cfg = SPORT_CONFIG.get(sel.get("sport", ""), {"emoji": "🎲", "label": sel.get("sport", "")})
            msg += (
                f"\n--- Selection {i} ---\n"
                f"Match : {sel.get('match', 'N/A')}\n"
                f"Sport : {sel_cfg['label']}\n"
                f"Marche : {sel.get('market', 'N/A')}\n"
                f"Pronostic : {sel.get('pick', 'N/A')}\n"
            )
    else:
        msg += (
            f"\n--- Selection ---\n"
            f"Match : {bet.get('match', 'N/A')}\n"
            f"Sport : {cfg['label']}\n"
            f"Marche : {bet.get('market', 'N/A')}\n"
            f"Pronostic : {bet.get('pick', 'N/A')}\n"
        )
    return msg

def format_bet_id(bet_id):
    # Formate l'ID avec espaces : 600207473 -> 600 207 473
    bet_id = str(bet_id)
    return ' '.join([bet_id[max(0,i-3):i] for i in range(len(bet_id), 0, -3)][::-1])

async def send_telegram_normal(session, bet, rule_name):
    cfg = SPORT_CONFIG.get(bet["sport"], {"emoji": "?", "label": bet["sport"]})
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    user = bet.get("user", "")
    display_user = "Anonyme" if not user or user == "Anonymous" else "@" + user
    bet_id = format_bet_id(bet.get("id", "N/A"))

    msg = (
        f"{cfg['emoji']} *{rule_name} DETECTE*\n\n"
        f"🆔 ID : {bet_id}\n"
        f"👤 Parieur : {display_user}\n"
        f"🕐 Active : {now}\n"
        f"💸 Cote : x{bet.get('odd', 'N/A')}\n"
        f"💰 Mise : ${bet.get('stake', 0):,}\n"
        f"💵 Gain potentiel : ${bet.get('payout', 0):,}\n"
        f"🔴 Live : {'Oui' if bet.get('live') else 'Non'}\n"
        f"🔗 Combine : {'Oui' if bet.get('combo') else 'Non'}\n"
    )
    msg += build_selections_text(bet, cfg)
    msg += "\n_Detecte par StakeScan_"
    return await send_simple_message(session, msg)

async def send_telegram_vip(session, bet, consecutive=1):
    cfg = SPORT_CONFIG.get(bet["sport"], {"emoji": "🎲", "label": bet["sport"]})
    now = datetime.now().strftime("%d/%m/%Y à %H:%M")
    user = bet.get("user", "Anonyme")
    alert_extra = f"\n🔥 *{consecutive} paris consecutifs !*" if consecutive >= 2 else ""
    bet_id = format_bet_id(bet.get("id", "N/A"))

    msg = (
        f"⭐ *VIP ALERTE - @{user}*{alert_extra}\n\n"
        f"🆔 ID : {bet_id}\n"
        f"🕐 Active : {now}\n"
        f"💸 Cote : x{bet.get('odd', 'N/A')}\n"
        f"💰 Mise : ${bet.get('stake', 0):,}\n"
        f"💵 Gain potentiel : ${bet.get('payout', 0):,}\n"
        f"🔴 Live : {'Oui' if bet.get('live') else 'Non'}\n"
        f"🔗 Combine : {'Oui' if bet.get('combo') else 'Non'}\n"
    )
    msg += build_selections_text(bet, cfg)
    msg += "\n_Detecte par StakeScan_"
    return await send_simple_message(session, msg)

async def send_suspicious_alert(session, bet, suspicion):
    cfg = SPORT_CONFIG.get(bet["sport"], {"emoji": "🎲", "label": bet["sport"]})
    now = datetime.now().strftime("%H:%M")
    reasons_text = "\n".join([f"  • {r}" for r in suspicion["reasons"]])

    msg = (
        f"{suspicion['level']} - MATCH SUSPECT\n\n"
        f"Heure : {now}\n"
        f"Match : {bet.get('match', 'N/A')}\n"
        f"Sport : {cfg['label']}\n"
        f"Pronostic : {bet.get('pick', 'N/A')}\n"
        f"Cote : x{bet.get('odd', 'N/A')}\n"
        f"Mise : ${bet.get('stake', 0):,}\n\n"
        f"*Raisons :*\n{reasons_text}\n\n"
        f"_Attention avant de copier ce pari !_"
    )
    return await send_simple_message(session, msg)

async def send_consensus_alert(session, bet, count):
    cfg = SPORT_CONFIG.get(bet["sport"], {"emoji": "🎲", "label": bet["sport"]})
    msg = (
        f"🤝 *CONSENSUS DETECTE*\n\n"
        f"{cfg['emoji']} {bet.get('match', 'N/A')}\n"
        f"Pronostic : {bet.get('pick', 'N/A')}\n"
        f"Cote : x{bet.get('odd', 'N/A')}\n\n"
        f"*{count} parieurs* ont mise sur ce meme pronostic en 5 min !\n\n"
        f"_Detecte par StakeScan_"
    )
    return await send_simple_message(session, msg)

# ============================================================
# RESUME QUOTIDIEN
# ============================================================
async def send_daily_summary(session):
    top_vip = sorted(stats["vip_bets"].items(), key=lambda x: x[1], reverse=True)[:3]
    top_lines = "\n".join([f"  ⭐ @{u} — {n} paris" for u, n in top_vip]) if top_vip else "  Aucun"

    msg = (
        f"📅 *Resume du jour — {datetime.now().strftime('%d/%m/%Y')}*\n\n"
        f"🎯 Paris detectes : {stats['daily_detected']}\n"
        f"✈️ Envoyes Telegram : {stats['daily_sent']}\n\n"
        f"🏆 *Top VIP du jour :*\n{top_lines}\n\n"
        f"_StakeScan — Bonne nuit !_"
    )
    await send_simple_message(session, msg)
    # Reset stats quotidiennes
    stats["daily_detected"] = 0
    stats["daily_sent"] = 0
    stats["daily_reset"] = datetime.now().date()
    print("Resume quotidien envoye.")

# ============================================================
# TRAITEMENT MISES A JOUR TELEGRAM
# ============================================================
async def process_telegram_updates(session):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": bot_state["last_update_id"] + 1, "timeout": 1}
    try:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            if not data.get("ok"):
                return
            for update in data.get("result", []):
                bot_state["last_update_id"] = update["update_id"]

                # Message texte
                if "message" in update:
                    msg = update["message"]
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != TELEGRAM_CHAT_ID:
                        continue

                    if text in ["/start", "/menu"]:
                        bot_state["waiting_add_vip"] = False
                        bot_state["waiting_remove_vip"] = False
                        bot_state["waiting_min_odd"] = False
                        bot_state["waiting_exact_stake"] = False
                        bot_state["waiting_bigodds_stake"] = False
                        bot_state["waiting_stake_sport"] = None
                        await send_main_menu(session)

                    elif text == "/vip":
                        await send_vip_menu(session)

                    elif text == "/stats":
                        await send_stats_menu(session)

                    elif text == "/token":
                        bot_state["waiting_new_token"] = True
                        await send_simple_message(session,
                            "🔑 *Renouveler le token Stake*\n\n"
                            "1. Ouvre Stake sur Chrome\n"
                            "2. Appuie F12 → Network\n"
                            "3. Recharge la page F5\n"
                            "4. Clique sur une requete graphql\n"
                            "5. Copie la valeur de x-access-token\n\n"
                            "Envoie-moi le nouveau token :"
                        )

                    elif bot_state["waiting_new_token"]:
                        new_token = text.strip()
                        if len(new_token) > 20:
                            global STAKE_SESSION_TOKEN
                            STAKE_SESSION_TOKEN = new_token
                            bot_state["waiting_new_token"] = False
                            await send_simple_message(session, "✅ Token Stake mis a jour avec succes !\nLe bot utilise maintenant le nouveau token.")
                        else:
                            await send_simple_message(session, "⚠️ Token invalide. Verifie et renvoie.")

                    elif bot_state["waiting_add_vip"]:
                        username = text.lstrip("@").strip()
                        if username and username.lower() not in [v.lower() for v in VIP_USERS]:
                            VIP_USERS.append(username)
                            bot_state["waiting_add_vip"] = False
                            await send_simple_message(session, f"✅ *@{username}* ajoute a la liste VIP !\nTotal : {len(VIP_USERS)}")
                            await send_vip_menu(session)
                        else:
                            await send_simple_message(session, f"⚠️ Nom invalide ou deja dans la liste.")

                    elif bot_state["waiting_min_odd"]:
                        try:
                            new_odd = float(text.replace(",", "."))
                            if 1.0 <= new_odd <= 50.0:
                                bot_state["min_odd"] = new_odd
                                bot_state["waiting_min_odd"] = False
                                await send_simple_message(session, f"✅ Cote minimum mise a jour : x{new_odd}")
                                await send_settings_menu(session)
                            else:
                                await send_simple_message(session, "⚠️ Entre une valeur entre 1.0 et 50.0")
                        except:
                            await send_simple_message(session, "⚠️ Format invalide. Exemple : 2.5")

                    elif bot_state["waiting_exact_stake"]:
                        try:
                            new_stake = float(text.replace(",", ".").replace("$", ""))
                            if new_stake >= 0:
                                bot_state["exact_min_stake"] = new_stake
                                bot_state["waiting_exact_stake"] = False
                                await send_simple_message(session, f"✅ Mise min. score exact mise a jour : ${new_stake:,.0f}")
                                await send_stakes_menu(session)
                            else:
                                await send_simple_message(session, "⚠️ Montant invalide.")
                        except:
                            await send_simple_message(session, "⚠️ Format invalide. Exemple : 500")

                    elif bot_state["waiting_bigodds_stake"]:
                        try:
                            new_stake = float(text.replace(",", ".").replace("$", ""))
                            if new_stake >= 0:
                                bot_state["bigodds_min_stake"] = new_stake
                                bot_state["waiting_bigodds_stake"] = False
                                await send_simple_message(session, f"✅ Mise min. grosse cote mise a jour : ${new_stake:,.0f}")
                                await send_stakes_menu(session)
                            else:
                                await send_simple_message(session, "⚠️ Montant invalide.")
                        except:
                            await send_simple_message(session, "⚠️ Format invalide. Exemple : 5000")

                    elif bot_state["waiting_stake_sport"]:
                        sport = bot_state["waiting_stake_sport"]
                        try:
                            new_stake = float(text.replace(",", ".").replace("$", ""))
                            if new_stake >= 0:
                                SPORT_CONFIG[sport]["min_stake"] = new_stake
                                bot_state["waiting_stake_sport"] = None
                                cfg = SPORT_CONFIG[sport]
                                await send_simple_message(session, f"✅ Mise min. {cfg['emoji']} {cfg['label']} mise a jour : ${new_stake:,.0f}")
                                await send_stakes_menu(session)
                            else:
                                await send_simple_message(session, "⚠️ Montant invalide.")
                        except:
                            await send_simple_message(session, "⚠️ Format invalide. Exemple : 8000")

                # Bouton clique
                elif "callback_query" in update:
                    cb = update["callback_query"]
                    data_cb = cb.get("data", "")
                    cb_id = cb["id"]
                    chat_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
                    if chat_id != TELEGRAM_CHAT_ID:
                        continue
                    await answer_callback(session, cb_id)

                    elif data_cb == "renew_token":
                        bot_state["waiting_new_token"] = True
                        await send_simple_message(session,
                            "🔑 *Renouveler le token Stake*\n\n"
                            "1. Ouvre Stake sur Chrome\n"
                            "2. Appuie F12 → Network\n"
                            "3. Recharge la page F5\n"
                            "4. Clique sur une requete graphql\n"
                            "5. Copie la valeur de *x-access-token*\n\n"
                            "Envoie-moi le nouveau token :"
                        )
                    elif data_cb == "open_menu":
                        await send_main_menu(session)
                    elif data_cb == "menu_vip":
                        await send_vip_menu(session)
                    elif data_cb == "menu_settings":
                        await send_settings_menu(session)
                    elif data_cb == "menu_stakes":
                        await send_stakes_menu(session)
                    elif data_cb == "menu_stats":
                        await send_stats_menu(session)
                    elif data_cb.startswith("menu_history_"):
                        page = int(data_cb.replace("menu_history_", ""))
                        await send_history_menu(session, page)
                    elif data_cb == "close_menu":
                        await send_simple_message(session, "Menu ferme. Tape /menu pour rouvrir.")
                    elif data_cb == "toggle_pause":
                        bot_state["paused"] = not bot_state["paused"]
                        status = "mis en pause" if bot_state["paused"] else "repris"
                        await send_simple_message(session, f"{'⏸' if bot_state['paused'] else '▶️'} Bot {status}.")
                        await send_main_menu(session)
                    elif data_cb == "add_vip":
                        bot_state["waiting_add_vip"] = True
                        bot_state["waiting_min_odd"] = False
                        await send_simple_message(session, "Envoie-moi le nom d'utilisateur Stake a ajouter :")
                    elif data_cb == "remove_vip":
                        await send_remove_vip_menu(session)
                    elif data_cb == "list_vip":
                        if VIP_USERS:
                            lines = "\n".join([f"⭐ @{u}" for u in VIP_USERS])
                            await send_simple_message(session, f"📋 *Liste VIP ({len(VIP_USERS)})*\n\n{lines}")
                        else:
                            await send_simple_message(session, "Aucun VIP configure.")
                    elif data_cb == "set_min_odd":
                        bot_state["waiting_min_odd"] = True
                        bot_state["waiting_add_vip"] = False
                        await send_simple_message(session, f"Cote actuelle : x{bot_state['min_odd']}\nEnvoie la nouvelle cote minimum (ex: 2.0) :")
                    elif data_cb.startswith("stake_"):
                        sport_or_rule = data_cb.replace("stake_", "")
                        if sport_or_rule == "exact":
                            bot_state["waiting_exact_stake"] = True
                            bot_state["waiting_stake_sport"] = None
                            await send_simple_message(session, f"Mise actuelle score exact : ${bot_state['exact_min_stake']:,}\nEnvoie le nouveau montant minimum (ex: 500) :")
                        elif sport_or_rule == "bigodds":
                            bot_state["waiting_bigodds_stake"] = True
                            bot_state["waiting_stake_sport"] = None
                            await send_simple_message(session, f"Mise actuelle grosse cote : ${bot_state['bigodds_min_stake']:,}\nEnvoie le nouveau montant minimum (ex: 5000) :")
                        elif sport_or_rule in SPORT_CONFIG:
                            bot_state["waiting_stake_sport"] = sport_or_rule
                            bot_state["waiting_exact_stake"] = False
                            bot_state["waiting_bigodds_stake"] = False
                            cfg = SPORT_CONFIG[sport_or_rule]
                            await send_simple_message(session, f"Mise actuelle {cfg['emoji']} {cfg['label']} : ${cfg['min_stake']:,}\nEnvoie le nouveau montant minimum (ex: 5000) :")
                    elif data_cb.startswith("toggle_"):
                        sport = data_cb.replace("toggle_", "")
                        if sport in SPORT_CONFIG:
                            SPORT_CONFIG[sport]["active"] = not SPORT_CONFIG[sport]["active"]
                            state = "active" if SPORT_CONFIG[sport]["active"] else "desactive"
                            await send_simple_message(session, f"{SPORT_CONFIG[sport]['emoji']} {SPORT_CONFIG[sport]['label']} {state}.")
                            await send_settings_menu(session)
                    elif data_cb.startswith("del_"):
                        username = data_cb[4:]
                        lower_list = [v.lower() for v in VIP_USERS]
                        if username.lower() in lower_list:
                            idx = lower_list.index(username.lower())
                            removed = VIP_USERS.pop(idx)
                            await send_simple_message(session, f"✅ *@{removed}* supprime.")
                            await send_vip_menu(session)
                    elif data_cb == "show_history":
                        await send_history_menu(session, 0)

    except Exception as e:
        print(f"Updates erreur: {e}")

# ============================================================
# STAKE API - FLUX PUBLIC (sports/home)
# ============================================================
async def fetch_stake_bets(session):
    url = "https://stake.com/_api/graphql"
    headers = {
        "Content-Type": "application/json",
        "x-access-token": STAKE_SESSION_TOKEN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "referer": "https://stake.com/fr/sports/home",
        "x-language": "fr",
    }

    # Requete pour le fil public "Tous les Paris" visible sur sports/home
    query = """
    query PublicBetList($limit: Int, $offset: Int) {
      sportBetList(limit: $limit, offset: $offset) {
        id
        amount
        payout
        odds
        cashoutAt
        status
        active
        bet {
          ... on SportBet {
            id
            amount
            payout
            odds
            isLive
            isCashout
            user { name }
            outcomes {
              odds
              result
              fixture {
                id
                name
                slug
                sport { name slug }
                tournament { name }
              }
              market { marketType { name } }
              selection { name }
            }
          }
        }
      }
    }
    """

    # Fallback - requete plus simple si la premiere echoue
    query_simple = """
    query LatestBets {
      latestBets: betList(limit: 40) {
        ... on SportBet {
          id
          amount
          payout
          odds
          isLive
          user { name }
          outcomes {
            odds
            fixture { name sport { slug } }
            market { marketType { name } }
            selection { name }
          }
        }
      }
    }
    """

    bets = []

    # Essai requete principale
    try:
        async with session.post(url,
            json={"query": query, "variables": {"limit": 40, "offset": 0}},
            headers=headers
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                raw_list = data.get("data", {}).get("sportBetList", [])
                if raw_list:
                    for item in raw_list:
                        b = item.get("bet") if item.get("bet") else item
                        if not b:
                            continue
                        parsed = parse_bet(b)
                        if parsed:
                            bets.append(parsed)
                    if bets:
                        return bets
    except Exception as e:
        print(f"  Requete principale erreur: {e}")

    # Fallback requete simple
    try:
        async with session.post(url,
            json={"query": query_simple},
            headers=headers
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                raw_list = data.get("data", {}).get("latestBets", [])
                if raw_list:
                    for b in raw_list:
                        if not b:
                            continue
                        parsed = parse_bet(b)
                        if parsed:
                            bets.append(parsed)
                    if bets:
                        return bets
    except Exception as e:
        print(f"  Fallback erreur: {e}")

    return []

def parse_bet(b):
    try:
        outcomes = b.get("outcomes", [])
        if not outcomes:
            return None
        first = outcomes[0]
        sport_slug = first.get("fixture", {}).get("sport", {}).get("slug", "")
        market_name = first.get("market", {}).get("marketType", {}).get("name", "")
        is_exact = "correct score" in market_name.lower() or "score exact" in market_name.lower()
        sport = map_sport(sport_slug)
        if not sport:
            return None
        selections = []
        for o in outcomes:
            s_slug = o.get("fixture", {}).get("sport", {}).get("slug", "")
            selections.append({
                "sport": map_sport(s_slug) or sport,
                "match": o.get("fixture", {}).get("name", "N/A"),
                "market": o.get("market", {}).get("marketType", {}).get("name", "N/A"),
                "pick": o.get("selection", {}).get("name", "N/A"),
            })
        return {
            "user": b.get("user", {}).get("name", "Anonymous") if b.get("user") else "Anonymous",
            "sport": sport,
            "market": market_name,
            "match": first.get("fixture", {}).get("name", "N/A"),
            "pick": first.get("selection", {}).get("name", "N/A"),
            "odd": float(b.get("odds", 0)),
            "stake": float(b.get("amount", 0)),
            "payout": float(b.get("payout", 0)),
            "combo": len(outcomes) > 1,
            "live": b.get("isLive", False),
            "exact": is_exact,
            "selections": selections,
        }
    except Exception as e:
        print(f"  Parse bet erreur: {e}")
        return None

def map_sport(slug):
    mapping = {
        "soccer": "football", "football": "football",
        "tennis": "tennis", "table-tennis": "tennis-table",
        "tabletennis": "tennis-table", "basketball": "basketball",
        "handball": "handball",
    }
    return mapping.get(slug.lower(), None) if slug else None

# ============================================================
# DONNEES DE TEST
# ============================================================
SAMPLE_BETS = [
    {
        "user": "ShivamGudu910", "sport": "football", "market": "Score exact",
        "match": "Real Madrid vs Barcelona", "pick": "2-1",
        "odd": 7.50, "stake": 500, "payout": 3750, "combo": False, "live": False, "exact": True,
        "selections": [{"sport": "football", "match": "Real Madrid vs Barcelona", "market": "Score exact", "pick": "2-1"}]
    },
    {
        "user": "OddsHunter", "sport": "tennis", "market": "Combine",
        "match": "Djokovic vs Alcaraz + Ma Long vs Fan Zhendong", "pick": "Djokovic / 3-1",
        "odd": 16.50, "stake": 8000, "payout": 132000, "combo": True, "live": False, "exact": True,
        "selections": [
            {"sport": "tennis",       "match": "Djokovic vs Alcaraz",     "market": "Vainqueur match", "pick": "Djokovic"},
            {"sport": "tennis-table", "match": "Ma Long vs Fan Zhendong", "market": "Score exact",     "pick": "3-1"},
        ]
    },
    {
        "user": "MegaBettor", "sport": "football", "market": "Combine",
        "match": "PSG vs Lyon + OM vs Nice + Monaco vs Lens", "pick": "PSG / OM / Monaco",
        "odd": 18.00, "stake": 10000, "payout": 180000, "combo": True, "live": False, "exact": False,
        "selections": [
            {"sport": "football", "match": "PSG vs Lyon",    "market": "Victoire", "pick": "PSG"},
            {"sport": "football", "match": "OM vs Nice",     "market": "Victoire", "pick": "OM"},
            {"sport": "football", "match": "Monaco vs Lens", "market": "Victoire", "pick": "Monaco"},
        ]
    },
    {
        "user": "ExactScoreKing", "sport": "handball", "market": "Score exact",
        "match": "THW Kiel vs SG Flensburg", "pick": "28-25",
        "odd": 22.00, "stake": 7500, "payout": 165000, "combo": False, "live": False, "exact": True,
        "selections": [{"sport": "handball", "match": "THW Kiel vs SG Flensburg", "market": "Score exact", "pick": "28-25"}]
    },
    {
        "user": "SuspectBettor", "sport": "football", "market": "Score exact",
        "match": "FC Unknown vs CD Obscur", "pick": "2-1",
        "odd": 8.50, "stake": 55000, "payout": 467500, "combo": False, "live": False, "exact": True,
        "selections": [{"sport": "football", "match": "FC Unknown vs CD Obscur", "market": "Score exact", "pick": "2-1"}]
    },
]

# ============================================================
# BOUCLE PRINCIPALE
# ============================================================
last_summary_date = datetime.now().date()

async def main():
    global last_summary_date
    bot_state["session_start"] = datetime.now()

    print("=" * 55)
    print("  StakeScan - Bot demarre")
    print(f"  Cote minimum : x{bot_state['min_odd']}")
    print(f"  Commandes : /menu /vip /stats")
    print("=" * 55)

    async with aiohttp.ClientSession() as session:

        # Message demarrage
        await send_simple_message(session,
            "*StakeScan demarre !*\n\n"
            "Le bot scanne les paris en temps reel.\n\n"
            "Commandes :\n"
            "/menu — Menu principal\n"
            "/vip — Gerer les VIP\n"
            "/stats — Statistiques\n"
            "/historique — Historique scans\n"
            "/token — Renouveler le token Stake",
            {"inline_keyboard": [[{"text": "📋 Menu principal", "callback_data": "open_menu"}]]}
        )
        print("Telegram connecte !\n")

        while True:
            # Resume quotidien a minuit
            today = datetime.now().date()
            now_time = datetime.now().time()
            if today != last_summary_date and time(0, 0) <= now_time <= time(0, 5):
                await send_daily_summary(session)
                last_summary_date = today

            # Traiter commandes Telegram
            await process_telegram_updates(session)

            # Si en pause, ne pas scanner
            if bot_state["paused"]:
                await asyncio.sleep(3)
                continue

            bot_state["scan_count"] += 1
            print(f"[Scan #{bot_state['scan_count']}] {datetime.now().strftime('%H:%M:%S')} | VIP: {len(VIP_USERS)} | Cote min: x{bot_state['min_odd']}")

            bets = await fetch_stake_bets(session)
            if not bets:
                print("  API Stake vide - donnees de test")
                bets = [SAMPLE_BETS[bot_state["sample_index"] % len(SAMPLE_BETS)]]
                bot_state["sample_index"] += 1

            for bet in bets:
                bet_id = f"{bet.get('match')}_{bet.get('pick')}_{bet.get('stake')}"
                if bet_id in seen_ids:
                    continue
                seen_ids.add(bet_id)

                # Historique
                stats["history"].append(bet)
                if len(stats["history"]) > 50:
                    stats["history"].pop(0)

                # Detection match suspect
                suspicion = check_suspicious(bet)
                if suspicion:
                    print(f"\n  {suspicion['level']} : {bet.get('match')} | Score: {suspicion['score']}")
                    await send_suspicious_alert(session, bet, suspicion)

                # Detection consensus
                consensus_count = check_consensus(bet)
                if consensus_count >= 3:
                    print(f"\n  CONSENSUS : {bet.get('match')} — {consensus_count} parieurs")
                    await send_consensus_alert(session, bet, consensus_count)

                # VIP
                if is_vip_user(bet):
                    user = bet.get("user", "")
                    consecutive = check_vip_consecutive(user)
                    stats["vip_bets"][user] += 1
                    print(f"\n  ⭐ VIP : @{user} | {bet.get('match')} | x{bet.get('odd')} (#{consecutive})")
                    sent = await send_telegram_vip(session, bet, consecutive)
                    if sent:
                        stats["total_sent_telegram"] += 1
                        stats["daily_sent"] += 1

                # Filtres normaux
                if matches_filters(bet):
                    stats["total_detected"] += 1
                    stats["daily_detected"] += 1
                    cfg = SPORT_CONFIG.get(bet["sport"], {})
                    print(f"\n  DETECTE : {cfg.get('emoji','')} {bet.get('match')} | x{bet.get('odd')} | ${bet.get('stake'):,}")
                    send, rule_name = should_send_telegram(bet)
                    if send:
                        sent = await send_telegram_normal(session, bet, rule_name)
                        if sent:
                            stats["total_sent_telegram"] += 1
                            stats["daily_sent"] += 1

            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot arrete.")
