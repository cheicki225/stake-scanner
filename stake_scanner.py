import asyncio
import aiohttp
import json
import websockets
from datetime import datetime, time
from collections import defaultdict

# ============================================================
# CONFIG
# ============================================================
TELEGRAM_BOT_TOKEN = "8868714626:AAHhSU2GkIW0jdIao1I9ZvKFx71tAJtMAco"
TELEGRAM_CHAT_ID = "7360478330"
STAKE_WS_URL = "wss://stake.com/_api/websockets"

# Token de session (se renouvelle via /token sur Telegram)
STAKE_TOKEN = "edd0aade282af360a96023fdb4037702918585c2ee236b60c5e5f8f2b03bc2ccf0788b8cff2d3b041561fd5248d60588"

# ============================================================
# SPORTS CONFIG
# ============================================================
SPORT_CONFIG = {
    "football":     {"emoji": "⚽", "label": "Football",        "min_stake": 10000, "exact_min_stake": 100, "active": True},
    "tennis":       {"emoji": "🎾", "label": "Tennis",           "min_stake": 8000,  "exact_min_stake": 100, "active": True},
    "tennis-table": {"emoji": "🏓", "label": "Tennis de table",  "min_stake": 2000,  "exact_min_stake": 100, "active": True},
    "basketball":   {"emoji": "🏀", "label": "Basketball",       "min_stake": 10000, "exact_min_stake": 100, "active": True},
    "handball":     {"emoji": "🤾", "label": "Handball",         "min_stake": 10000, "exact_min_stake": 100, "active": True},
}

SPORT_SLUGS = {
    "soccer": "football", "football": "football",
    "tennis": "tennis", "table-tennis": "tennis-table",
    "tabletennis": "tennis-table", "basketball": "basketball",
    "handball": "handball",
}

# ============================================================
# ETAT DU BOT
# ============================================================
state = {
    "paused": False,
    "min_odd": 1.75,
    "exact_min_stake": 1000,
    "bigodds_min_stake": 7000,
    "bigodds_min_odd": 15,
    "last_update_id": 0,
    "scan_count": 0,
    "sample_index": 0,
    "session_start": datetime.now(),
    # Attentes
    "wait_add_vip": False,
    "wait_min_odd": False,
    "wait_exact_stake": False,
    "wait_bigodds_stake": False,
    "wait_sport_stake": None,
    "wait_token": False,
    "history_page": 0,
}

VIP_USERS = []
seen_ids = set()

# Stats
stats = {
    "total_detected": 0,
    "total_sent": 0,
    "daily_detected": 0,
    "daily_sent": 0,
    "vip_bets": defaultdict(int),
    "vip_consecutive": defaultdict(int),
    "history": [],
}

# Detection suspects et consensus
consensus_tracker = defaultdict(list)
odd_tracker = defaultdict(list)

# ============================================================
# FONCTIONS UTILITAIRES
# ============================================================
def map_sport(slug):
    return SPORT_SLUGS.get((slug or "").lower())

def format_id(bet_id):
    s = str(bet_id)
    return " ".join([s[max(0,i-3):i] for i in range(len(s), 0, -3)][::-1])

def format_money(amount):
    return f"${float(amount):,.2f}"

def is_vip(user):
    return user and user != "Anonymous" and user.lower() in [v.lower() for v in VIP_USERS]

def matches_filters(bet):
    sport = bet.get("sport")
    if sport not in SPORT_CONFIG:
        return False
    cfg = SPORT_CONFIG[sport]
    if not cfg["active"]:
        return False
    if bet.get("odd", 0) < state["min_odd"]:
        return False
    min_stake = cfg["exact_min_stake"] if bet.get("exact") else cfg["min_stake"]
    if bet.get("stake", 0) < min_stake:
        return False
    return True

def should_notify(bet):
    if bet.get("exact") and bet.get("stake", 0) >= state["exact_min_stake"]:
        return True, "🎯 Score Exact"
    if bet.get("odd", 0) >= state["bigodds_min_odd"] and bet.get("stake", 0) >= state["bigodds_min_stake"]:
        return True, f"💥 Grosse Cote (>={state['bigodds_min_odd']})"
    return False, None

def check_suspicious(bet):
    key = f"{bet.get('match')}_{bet.get('pick')}"
    score = 0
    reasons = []

    # Critere 2 : consensus
    consensus_tracker[key].append(datetime.now())
    recent = [t for t in consensus_tracker[key] if (datetime.now()-t).seconds < 300]
    consensus_tracker[key] = recent
    if len(recent) >= 5:
        score += 2
        reasons.append(f"👥 {len(recent)} parieurs sur le meme pronostic")
    elif len(recent) >= 3:
        score += 1
        reasons.append(f"👥 {len(recent)} parieurs identiques")

    # Critere 3 : mise anormale
    stake = bet.get("stake", 0)
    odd = bet.get("odd", 0)
    if odd < 2.0 and stake > 50000:
        score += 1
        reasons.append(f"💰 Mise elevee (${stake:,}) sur cote faible (x{odd})")
    if odd > 10 and stake > 20000:
        score += 2
        reasons.append(f"💰 Mise anormale (${stake:,}) sur grosse cote (x{odd})")

    # Critere 4 : chute de cote
    odd_tracker[key].append({"odd": odd, "time": datetime.now()})
    recent_odds = [o for o in odd_tracker[key] if (datetime.now()-o["time"]).seconds < 600]
    odd_tracker[key] = recent_odds
    if len(recent_odds) >= 2:
        first = recent_odds[0]["odd"]
        last = recent_odds[-1]["odd"]
        if first > 0:
            drop = (first - last) / first * 100
            if drop >= 30:
                score += 2
                reasons.append(f"📉 Cote chutee de {drop:.0f}% (x{first} → x{last})")
            elif drop >= 20:
                score += 1
                reasons.append(f"📉 Cote chutee de {drop:.0f}%")

    if score == 0:
        return None
    level = "🔴 DANGER" if score >= 4 else "🚨 TRES SUSPECT" if score >= 2 else "⚠️ SUSPECT"
    return {"level": level, "score": score, "reasons": reasons}

# ============================================================
# TELEGRAM - ENVOI
# ============================================================
async def tg_send(session, text, keyboard=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        async with session.post(url, json=payload) as resp:
            data = await resp.json()
            return data.get("ok", False)
    except Exception as e:
        print(f"TG erreur: {e}")
    return False

async def tg_answer(session, cb_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        async with session.post(url, json={"callback_query_id": cb_id}) as resp:
            pass
    except:
        pass

# ============================================================
# TELEGRAM - MENUS
# ============================================================
async def menu_main(session):
    status = "🟢 Actif" if not state["paused"] else "🔴 En pause"
    text = (
        f"*StakeScan — Menu Principal*\n\n"
        f"Statut : {status}\n"
        f"Cote min : x{state['min_odd']}\n"
        f"VIP : {len(VIP_USERS)}\n"
        f"Scans : {state['scan_count']}\n"
        f"Detectes : {stats['total_detected']}"
    )
    kb = {"inline_keyboard": [
        [{"text": "⭐ VIP",              "callback_data": "menu_vip"}],
        [{"text": "⚙️ Parametres",       "callback_data": "menu_settings"}],
        [{"text": "💰 Montants",         "callback_data": "menu_stakes"}],
        [{"text": "📊 Statistiques",     "callback_data": "menu_stats"}],
        [{"text": "📋 Historique",       "callback_data": "hist_0"}],
        [{"text": "⏸ Pause" if not state["paused"] else "▶️ Reprendre", "callback_data": "toggle_pause"}],
        [{"text": "🔑 Renouveler token", "callback_data": "renew_token"}],
        [{"text": "❌ Fermer",           "callback_data": "close"}],
    ]}
    await tg_send(session, text, kb)

async def menu_vip(session):
    text = f"⭐ *Gestion VIP*\n\n{len(VIP_USERS)} utilisateur(s)\n\nQue veux-tu faire ?"
    kb = {"inline_keyboard": [
        [{"text": "➕ Ajouter VIP",   "callback_data": "add_vip"}],
        [{"text": "❌ Supprimer VIP", "callback_data": "del_vip_menu"}],
        [{"text": "📋 Liste VIP",     "callback_data": "list_vip"}],
        [{"text": "🔙 Retour",        "callback_data": "menu_main"}],
    ]}
    await tg_send(session, text, kb)

async def menu_settings(session):
    lines = "\n".join([
        f"{'✅' if cfg['active'] else '❌'} {cfg['emoji']} {cfg['label']}"
        for cfg in SPORT_CONFIG.values()
    ])
    text = f"⚙️ *Parametres*\n\nCote min : x{state['min_odd']}\n\n*Sports :*\n{lines}"
    kb = {"inline_keyboard": [
        [{"text": "📈 Cote minimum",     "callback_data": "set_odd"}],
        [{"text": "⚽ Football",         "callback_data": "tog_football"}],
        [{"text": "🎾 Tennis",           "callback_data": "tog_tennis"}],
        [{"text": "🏓 Tennis de table",  "callback_data": "tog_tennis-table"}],
        [{"text": "🏀 Basketball",       "callback_data": "tog_basketball"}],
        [{"text": "🤾 Handball",         "callback_data": "tog_handball"}],
        [{"text": "🔙 Retour",           "callback_data": "menu_main"}],
    ]}
    await tg_send(session, text, kb)

async def menu_stakes(session):
    text = (
        f"💰 *Montants minimum*\n\n"
        f"⚽ Football : ${SPORT_CONFIG['football']['min_stake']:,}\n"
        f"🎾 Tennis : ${SPORT_CONFIG['tennis']['min_stake']:,}\n"
        f"🏓 Tennis de table : ${SPORT_CONFIG['tennis-table']['min_stake']:,}\n"
        f"🏀 Basketball : ${SPORT_CONFIG['basketball']['min_stake']:,}\n"
        f"🤾 Handball : ${SPORT_CONFIG['handball']['min_stake']:,}\n\n"
        f"🎯 Score exact : ${state['exact_min_stake']:,}\n"
        f"💥 Grosse cote : ${state['bigodds_min_stake']:,}"
    )
    kb = {"inline_keyboard": [
        [{"text": "⚽ Football",          "callback_data": "stk_football"}],
        [{"text": "🎾 Tennis",            "callback_data": "stk_tennis"}],
        [{"text": "🏓 Tennis de table",   "callback_data": "stk_tennis-table"}],
        [{"text": "🏀 Basketball",        "callback_data": "stk_basketball"}],
        [{"text": "🤾 Handball",          "callback_data": "stk_handball"}],
        [{"text": "🎯 Score exact",       "callback_data": "stk_exact"}],
        [{"text": "💥 Grosse cote",       "callback_data": "stk_bigodds"}],
        [{"text": "🔙 Retour",            "callback_data": "menu_main"}],
    ]}
    await tg_send(session, text, kb)

async def menu_stats(session):
    dur = datetime.now() - state["session_start"]
    h, m = int(dur.total_seconds()//3600), int((dur.total_seconds()%3600)//60)
    top = sorted(stats["vip_bets"].items(), key=lambda x: x[1], reverse=True)[:5]
    top_txt = "\n".join([f"  ⭐ @{u} — {n} paris" for u,n in top]) if top else "  Aucun"
    text = (
        f"📊 *Statistiques*\n\n"
        f"⏱ Session : {h}h {m}min\n"
        f"🔍 Scans : {state['scan_count']}\n"
        f"🎯 Detectes : {stats['total_detected']}\n"
        f"✈️ Telegram : {stats['total_sent']}\n\n"
        f"📅 *Aujourd'hui :*\n"
        f"  Detectes : {stats['daily_detected']}\n"
        f"  Envoyes : {stats['daily_sent']}\n\n"
        f"🏆 *Top VIP :*\n{top_txt}"
    )
    kb = {"inline_keyboard": [
        [{"text": "📋 Historique", "callback_data": "hist_0"}],
        [{"text": "🔙 Retour",     "callback_data": "menu_main"}],
    ]}
    await tg_send(session, text, kb)

async def menu_history(session, page=0):
    per_page = 10
    total = len(stats["history"])
    if not total:
        await tg_send(session, "📋 Aucun historique pour l'instant.")
        return
    start = page * per_page
    items = stats["history"][-(start+per_page):len(stats["history"])-start if start > 0 else None][::-1]
    lines = []
    for i, h in enumerate(items, start+1):
        cfg = SPORT_CONFIG.get(h.get("sport",""), {"emoji":"🎲"})
        bid = format_id(h.get("id","?"))
        flags = ("🎯" if h.get("exact") else "") + ("🔴" if h.get("live") else "")
        lines.append(f"{i}. {cfg['emoji']}{flags} *{h.get('match','?')}*\n    @{h.get('user','?')} | x{h.get('odd')} | ${h.get('stake',0):,}\n    🆔 {bid}")
    text = f"📋 *Historique ({total} total) — Page {page+1}*\n\n" + "\n\n".join(lines)
    nav = []
    if page > 0:
        nav.append({"text": "⬅️ Precedent", "callback_data": f"hist_{page-1}"})
    if start + per_page < total:
        nav.append({"text": "Suivant ➡️", "callback_data": f"hist_{page+1}"})
    kb = {"inline_keyboard": []}
    if nav:
        kb["inline_keyboard"].append(nav)
    kb["inline_keyboard"].append([{"text": "🔙 Retour", "callback_data": "menu_main"}])
    await tg_send(session, text, kb)

async def menu_del_vip(session):
    if not VIP_USERS:
        await tg_send(session, "❌ Aucun VIP a supprimer.")
        return
    buttons = [[{"text": f"❌ @{u}", "callback_data": f"rmv_{u}"}] for u in VIP_USERS]
    buttons.append([{"text": "🔙 Retour", "callback_data": "menu_vip"}])
    await tg_send(session, "Choisis le VIP a supprimer :", {"inline_keyboard": buttons})

# ============================================================
# TELEGRAM - NOTIFICATIONS PARIS
# ============================================================
def build_selections(bet):
    cfg = SPORT_CONFIG.get(bet.get("sport",""), {"emoji":"🎲","label":"?"})
    msg = ""
    sels = bet.get("selections", [])
    if sels and len(sels) > 1:
        for i, s in enumerate(sels, 1):
            sc = SPORT_CONFIG.get(s.get("sport",""), {"emoji":"🎲","label":s.get("sport","")})
            msg += f"\n--- Selection {i} ---\nMatch : {s.get('match','?')}\nSport : {sc['label']}\nMarche : {s.get('market','?')}\nPronostic : {s.get('pick','?')}\n"
    else:
        msg += f"\n--- Selection ---\nMatch : {bet.get('match','?')}\nSport : {cfg['label']}\nMarche : {bet.get('market','?')}\nPronostic : {bet.get('pick','?')}\n"
    return msg

async def notify_normal(session, bet, rule):
    cfg = SPORT_CONFIG.get(bet.get("sport",""), {"emoji":"?","label":"?"})
    now = datetime.now().strftime("%d/%m/%Y a %H:%M")
    user = bet.get("user","")
    display = "Anonyme" if not user or user == "Anonymous" else f"@{user}"
    bid = format_id(bet.get("id","?"))
    msg = (
        f"{cfg['emoji']} *{rule} DETECTE*\n\n"
        f"🆔 ID : {bid}\n"
        f"👤 Parieur : {display}\n"
        f"🕐 Active : {now}\n"
        f"💸 Cote : x{bet.get('odd')}\n"
        f"💰 Mise : ${bet.get('stake',0):,}\n"
        f"💵 Gain potentiel : ${bet.get('payout',0):,}\n"
        f"🔴 Live : {'Oui' if bet.get('live') else 'Non'}\n"
        f"🔗 Combine : {'Oui' if bet.get('combo') else 'Non'}\n"
    )
    msg += build_selections(bet)
    msg += "\n_Detecte par StakeScan_"
    return await tg_send(session, msg)

async def notify_vip(session, bet, consecutive=1):
    cfg = SPORT_CONFIG.get(bet.get("sport",""), {"emoji":"🎲","label":"?"})
    now = datetime.now().strftime("%d/%m/%Y a %H:%M")
    user = bet.get("user","Anonyme")
    bid = format_id(bet.get("id","?"))
    extra = f"\n🔥 *{consecutive} paris consecutifs !*" if consecutive >= 2 else ""
    msg = (
        f"⭐ *VIP ALERTE - @{user}*{extra}\n\n"
        f"🆔 ID : {bid}\n"
        f"🕐 Active : {now}\n"
        f"💸 Cote : x{bet.get('odd')}\n"
        f"💰 Mise : ${bet.get('stake',0):,}\n"
        f"💵 Gain potentiel : ${bet.get('payout',0):,}\n"
        f"🔴 Live : {'Oui' if bet.get('live') else 'Non'}\n"
        f"🔗 Combine : {'Oui' if bet.get('combo') else 'Non'}\n"
    )
    msg += build_selections(bet)
    msg += "\n_Detecte par StakeScan_"
    return await tg_send(session, msg)

async def notify_suspicious(session, bet, suspicion):
    cfg = SPORT_CONFIG.get(bet.get("sport",""), {"emoji":"🎲"})
    now = datetime.now().strftime("%H:%M")
    reasons = "\n".join([f"  • {r}" for r in suspicion["reasons"]])
    msg = (
        f"{suspicion['level']} MATCH SUSPECT\n\n"
        f"🕐 {now}\n"
        f"🏟 Match : {bet.get('match','?')}\n"
        f"🎯 Pronostic : {bet.get('pick','?')}\n"
        f"💸 Cote : x{bet.get('odd')}\n"
        f"💰 Mise : ${bet.get('stake',0):,}\n\n"
        f"*Raisons :*\n{reasons}\n\n"
        f"_Attention avant de copier ce pari !_"
    )
    return await tg_send(session, msg)

async def notify_consensus(session, bet, count):
    cfg = SPORT_CONFIG.get(bet.get("sport",""), {"emoji":"🎲"})
    msg = (
        f"🤝 *CONSENSUS DETECTE*\n\n"
        f"{cfg['emoji']} {bet.get('match','?')}\n"
        f"Pronostic : {bet.get('pick','?')}\n"
        f"Cote : x{bet.get('odd')}\n\n"
        f"*{count} parieurs* ont mise pareil en 5 min !\n\n"
        f"_Detecte par StakeScan_"
    )
    return await tg_send(session, msg)

async def send_daily_summary(session):
    top = sorted(stats["vip_bets"].items(), key=lambda x: x[1], reverse=True)[:3]
    top_txt = "\n".join([f"  ⭐ @{u} — {n} paris" for u,n in top]) if top else "  Aucun"
    msg = (
        f"📅 *Resume du jour — {datetime.now().strftime('%d/%m/%Y')}*\n\n"
        f"🎯 Detectes : {stats['daily_detected']}\n"
        f"✈️ Envoyes : {stats['daily_sent']}\n\n"
        f"🏆 *Top VIP :*\n{top_txt}\n\n"
        f"_StakeScan — Bonne nuit !_"
    )
    await tg_send(session, msg)
    stats["daily_detected"] = 0
    stats["daily_sent"] = 0

# ============================================================
# TELEGRAM - TRAITEMENT UPDATES
# ============================================================
async def process_updates(session):
    global STAKE_TOKEN
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        async with session.get(url, params={"offset": state["last_update_id"]+1, "timeout": 1}) as resp:
            data = await resp.json()
            if not data.get("ok"):
                return
            for upd in data.get("result", []):
                state["last_update_id"] = upd["update_id"]

                # Message texte
                if "message" in upd:
                    msg = upd["message"]
                    text = msg.get("text","").strip()
                    cid = str(msg.get("chat",{}).get("id",""))
                    if cid != TELEGRAM_CHAT_ID:
                        continue

                    # Reset tous les waits sur /menu
                    if text in ["/start","/menu"]:
                        for k in ["wait_add_vip","wait_min_odd","wait_exact_stake","wait_bigodds_stake","wait_token"]:
                            state[k] = False
                        state["wait_sport_stake"] = None
                        await menu_main(session)

                    elif text == "/vip":
                        await menu_vip(session)
                    elif text == "/stats":
                        await menu_stats(session)
                    elif text == "/historique":
                        await menu_history(session, 0)
                    elif text == "/token":
                        state["wait_token"] = True
                        await tg_send(session, "🔑 Envoie le nouveau x-access-token de Stake :")

                    elif state["wait_token"]:
                        if len(text) > 20:
                            STAKE_TOKEN = text.strip()
                            state["wait_token"] = False
                            await tg_send(session, "✅ Token Stake mis a jour !")
                        else:
                            await tg_send(session, "⚠️ Token invalide.")

                    elif state["wait_add_vip"]:
                        username = text.lstrip("@").strip()
                        if username and username.lower() not in [v.lower() for v in VIP_USERS]:
                            VIP_USERS.append(username)
                            state["wait_add_vip"] = False
                            await tg_send(session, f"✅ *@{username}* ajoute ! Total : {len(VIP_USERS)}")
                            await menu_vip(session)
                        else:
                            await tg_send(session, "⚠️ Nom invalide ou deja dans la liste.")

                    elif state["wait_min_odd"]:
                        try:
                            v = float(text.replace(",","."))
                            if 1.0 <= v <= 50.0:
                                state["min_odd"] = v
                                state["wait_min_odd"] = False
                                await tg_send(session, f"✅ Cote min mise a jour : x{v}")
                                await menu_settings(session)
                            else:
                                await tg_send(session, "⚠️ Entre une valeur entre 1.0 et 50.0")
                        except:
                            await tg_send(session, "⚠️ Format invalide. Exemple : 2.5")

                    elif state["wait_exact_stake"]:
                        try:
                            v = float(text.replace(",",".").replace("$",""))
                            state["exact_min_stake"] = v
                            state["wait_exact_stake"] = False
                            await tg_send(session, f"✅ Mise min score exact : ${v:,.0f}")
                            await menu_stakes(session)
                        except:
                            await tg_send(session, "⚠️ Format invalide. Exemple : 500")

                    elif state["wait_bigodds_stake"]:
                        try:
                            v = float(text.replace(",",".").replace("$",""))
                            state["bigodds_min_stake"] = v
                            state["wait_bigodds_stake"] = False
                            await tg_send(session, f"✅ Mise min grosse cote : ${v:,.0f}")
                            await menu_stakes(session)
                        except:
                            await tg_send(session, "⚠️ Format invalide. Exemple : 5000")

                    elif state["wait_sport_stake"]:
                        sport = state["wait_sport_stake"]
                        try:
                            v = float(text.replace(",",".").replace("$",""))
                            SPORT_CONFIG[sport]["min_stake"] = v
                            state["wait_sport_stake"] = None
                            cfg = SPORT_CONFIG[sport]
                            await tg_send(session, f"✅ {cfg['emoji']} {cfg['label']} : ${v:,.0f}")
                            await menu_stakes(session)
                        except:
                            await tg_send(session, "⚠️ Format invalide. Exemple : 8000")

                # Bouton
                elif "callback_query" in upd:
                    cb = upd["callback_query"]
                    cbd = cb.get("data","")
                    cid = str(cb.get("message",{}).get("chat",{}).get("id",""))
                    if cid != TELEGRAM_CHAT_ID:
                        continue
                    await tg_answer(session, cb["id"])

                    if cbd == "menu_main":       await menu_main(session)
                    elif cbd == "menu_vip":      await menu_vip(session)
                    elif cbd == "menu_settings": await menu_settings(session)
                    elif cbd == "menu_stakes":   await menu_stakes(session)
                    elif cbd == "menu_stats":    await menu_stats(session)
                    elif cbd == "close":         await tg_send(session, "Menu ferme. Tape /menu pour rouvrir.")
                    elif cbd.startswith("hist_"):
                        await menu_history(session, int(cbd.split("_")[1]))
                    elif cbd == "toggle_pause":
                        state["paused"] = not state["paused"]
                        await tg_send(session, f"{'⏸ Bot mis en pause.' if state['paused'] else '▶️ Bot repris.'}")
                        await menu_main(session)
                    elif cbd == "renew_token":
                        state["wait_token"] = True
                        await tg_send(session,
                            "🔑 *Renouveler le token Stake*\n\n"
                            "1. Ouvre Stake sur Chrome\n"
                            "2. F12 → Network\n"
                            "3. Recharge F5\n"
                            "4. Clique une requete graphql\n"
                            "5. Copie *x-access-token*\n\n"
                            "Envoie le token ici :"
                        )
                    elif cbd == "add_vip":
                        state["wait_add_vip"] = True
                        await tg_send(session, "Envoie le nom d'utilisateur Stake a ajouter :")
                    elif cbd == "del_vip_menu":
                        await menu_del_vip(session)
                    elif cbd == "list_vip":
                        if VIP_USERS:
                            lines = "\n".join([f"⭐ @{u}" for u in VIP_USERS])
                            await tg_send(session, f"📋 *Liste VIP ({len(VIP_USERS)})*\n\n{lines}")
                        else:
                            await tg_send(session, "Aucun VIP configure.")
                    elif cbd == "set_odd":
                        state["wait_min_odd"] = True
                        await tg_send(session, f"Cote actuelle : x{state['min_odd']}\nEnvoie la nouvelle valeur :")
                    elif cbd.startswith("tog_"):
                        sport = cbd[4:]
                        if sport in SPORT_CONFIG:
                            SPORT_CONFIG[sport]["active"] = not SPORT_CONFIG[sport]["active"]
                            s = "active" if SPORT_CONFIG[sport]["active"] else "desactive"
                            await tg_send(session, f"{SPORT_CONFIG[sport]['emoji']} {SPORT_CONFIG[sport]['label']} {s}.")
                            await menu_settings(session)
                    elif cbd.startswith("stk_"):
                        which = cbd[4:]
                        if which == "exact":
                            state["wait_exact_stake"] = True
                            await tg_send(session, f"Mise actuelle score exact : ${state['exact_min_stake']:,}\nEnvoie le nouveau montant :")
                        elif which == "bigodds":
                            state["wait_bigodds_stake"] = True
                            await tg_send(session, f"Mise actuelle grosse cote : ${state['bigodds_min_stake']:,}\nEnvoie le nouveau montant :")
                        elif which in SPORT_CONFIG:
                            state["wait_sport_stake"] = which
                            cfg = SPORT_CONFIG[which]
                            await tg_send(session, f"Mise actuelle {cfg['emoji']} {cfg['label']} : ${cfg['min_stake']:,}\nEnvoie le nouveau montant :")
                    elif cbd.startswith("rmv_"):
                        username = cbd[4:]
                        lower = [v.lower() for v in VIP_USERS]
                        if username.lower() in lower:
                            removed = VIP_USERS.pop(lower.index(username.lower()))
                            await tg_send(session, f"✅ *@{removed}* supprime.")
                            await menu_vip(session)

    except Exception as e:
        print(f"Updates erreur: {e}")

# ============================================================
# WEBSOCKET STAKE - TEMPS REEL
# ============================================================
WS_QUEUE = asyncio.Queue()

async def stake_websocket_listener():
    """Se connecte au WebSocket de Stake et recoit les paris en temps reel"""
    headers = {
        "Origin": "https://stake.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Cookie": f"session={STAKE_TOKEN}",
    }

    # Souscription aux paris sportifs publics (highrollerSportBets)
    subscribe_msg = json.dumps({
        "id": "bet-feed-1",
        "type": "subscribe",
        "payload": {
            "query": """
            subscription HighrollerSportBets {
              highrollerSportBets {
                id
                amount
                payout
                odds
                isLive
                isCashout
                user { name }
                outcomes {
                  odds
                  fixture { name sport { slug } }
                  market { marketType { name } }
                  selection { name }
                }
              }
            }
            """
        }
    })

    while True:
        try:
            print("  WS → Connexion a wss://stake.com/_api/websockets...")
            async with websockets.connect(
                STAKE_WS_URL,
                extra_headers=headers,
                subprotocols=["graphql-transport-ws"],
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                # Init connexion GraphQL WS
                await ws.send(json.dumps({"type": "connection_init", "payload": {"token": STAKE_TOKEN}}))
                ack = await ws.recv()
                print(f"  WS → {ack[:80]}")

                # Souscrire aux paris
                await ws.send(subscribe_msg)
                print("  WS → Souscription aux paris en temps reel !")

                # Recevoir les messages
                async for message in ws:
                    try:
                        data = json.loads(message)
                        msg_type = data.get("type", "")

                        if msg_type == "ping":
                            await ws.send(json.dumps({"type": "pong"}))

                        elif msg_type == "next":
                            payload = data.get("payload", {}).get("data", {})
                            bet_raw = payload.get("highrollerSportBets")
                            if bet_raw:
                                bet = parse_bet(bet_raw)
                                if bet:
                                    await WS_QUEUE.put(bet)
                                    print(f"  WS ← Pari recu : {bet.get('match')} | x{bet.get('odd')} | ${bet.get('stake'):,}")

                        elif msg_type == "error":
                            print(f"  WS erreur: {data}")

                    except Exception as e:
                        print(f"  WS parse erreur: {e}")

        except Exception as e:
            print(f"  WS connexion erreur: {e} — reconnexion dans 5s...")
            await asyncio.sleep(5)

def parse_bet(b):
    try:
        outcomes = b.get("outcomes", [])
        if not outcomes:
            return None
        first = outcomes[0]
        sport = map_sport(first.get("fixture",{}).get("sport",{}).get("slug",""))
        if not sport:
            return None
        market = first.get("market",{}).get("marketType",{}).get("name","")
        is_exact = "correct score" in market.lower() or "score exact" in market.lower()
        sels = []
        for o in outcomes:
            s = map_sport(o.get("fixture",{}).get("sport",{}).get("slug","")) or sport
            sels.append({
                "sport": s,
                "match": o.get("fixture",{}).get("name","N/A"),
                "market": o.get("market",{}).get("marketType",{}).get("name","N/A"),
                "pick": o.get("selection",{}).get("name","N/A"),
            })
        return {
            "id": b.get("id", ""),
            "user": b.get("user",{}).get("name","Anonymous") if b.get("user") else "Anonymous",
            "sport": sport,
            "market": market,
            "match": first.get("fixture",{}).get("name","N/A"),
            "pick": first.get("selection",{}).get("name","N/A"),
            "odd": float(b.get("odds",0)),
            "stake": float(b.get("amount",0)),
            "payout": float(b.get("payout",0)),
            "combo": len(outcomes) > 1,
            "live": b.get("isLive", False),
            "exact": is_exact,
            "selections": sels,
        }
    except:
        return None

# ============================================================
# DONNEES DE TEST
# ============================================================
SAMPLE_BETS = [
    {"id":"600001","user":"ShivamGudu910","sport":"football","market":"Score exact","match":"Real Madrid vs Barcelona","pick":"2-1","odd":7.50,"stake":500,"payout":3750,"combo":False,"live":False,"exact":True,"selections":[{"sport":"football","match":"Real Madrid vs Barcelona","market":"Score exact","pick":"2-1"}]},
    {"id":"600002","user":"OddsHunter","sport":"tennis","market":"Combine","match":"Djokovic vs Alcaraz + Ma Long vs Fan","pick":"Djokovic / 3-1","odd":16.50,"stake":8000,"payout":132000,"combo":True,"live":False,"exact":True,"selections":[{"sport":"tennis","match":"Djokovic vs Alcaraz","market":"Vainqueur match","pick":"Djokovic"},{"sport":"tennis-table","match":"Ma Long vs Fan Zhendong","market":"Score exact","pick":"3-1"}]},
    {"id":"600003","user":"MegaBettor","sport":"football","market":"Combine","match":"PSG vs Lyon + OM vs Nice","pick":"PSG / OM","odd":18.00,"stake":10000,"payout":180000,"combo":True,"live":False,"exact":False,"selections":[{"sport":"football","match":"PSG vs Lyon","market":"Victoire","pick":"PSG"},{"sport":"football","match":"OM vs Nice","market":"Victoire","pick":"OM"}]},
    {"id":"600004","user":"ExactKing","sport":"handball","market":"Score exact","match":"THW Kiel vs Flensburg","pick":"28-25","odd":22.00,"stake":7500,"payout":165000,"combo":False,"live":False,"exact":True,"selections":[{"sport":"handball","match":"THW Kiel vs Flensburg","market":"Score exact","pick":"28-25"}]},
    {"id":"600005","user":"Anonymous","sport":"basketball","market":"Victoire","match":"Lakers vs Warriors","pick":"Lakers","odd":2.10,"stake":18000,"payout":37800,"combo":False,"live":True,"exact":False,"selections":[{"sport":"basketball","match":"Lakers vs Warriors","market":"Victoire","pick":"Lakers"}]},
]

# ============================================================
# BOUCLE PRINCIPALE
# ============================================================
last_summary_date = datetime.now().date()

async def main():
    global last_summary_date
    state["session_start"] = datetime.now()

    print("="*50)
    print("  StakeScan — Demarre")
    print(f"  Cote min : x{state['min_odd']}")
    print(f"  Token : {STAKE_TOKEN[:20]}...")
    print("="*50)

    async with aiohttp.ClientSession() as session:
        # Message demarrage
        sent = await tg_send(session,
            "*StakeScan demarre !* 🚀\n\n"
            "Commandes :\n"
            "/menu — Menu principal\n"
            "/vip — Gerer les VIP\n"
            "/stats — Statistiques\n"
            "/historique — Historique\n"
            "/token — Renouveler token",
            {"inline_keyboard": [[{"text": "📋 Menu principal", "callback_data": "menu_main"}]]}
        )
        if sent:
            print("Telegram connecte !\n")
        else:
            print("Telegram erreur — verifie le token bot !\n")

        # Lancer le WebSocket en arriere-plan
        asyncio.create_task(stake_websocket_listener())
        print("WebSocket Stake lance en arriere-plan !\n")

        while True:
            # Resume quotidien a minuit
            today = datetime.now().date()
            now_t = datetime.now().time()
            if today != last_summary_date and time(0,0) <= now_t <= time(0,5):
                await send_daily_summary(session)
                last_summary_date = today

            # Traiter commandes Telegram
            await process_updates(session)

            if state["paused"]:
                await asyncio.sleep(1)
                continue

            # Traiter les paris recus via WebSocket
            bets_received = []
            try:
                while not WS_QUEUE.empty():
                    bets_received.append(await WS_QUEUE.get())
            except:
                pass

            if not bets_received:
                await asyncio.sleep(1)
                continue

            state["scan_count"] += len(bets_received)
            print(f"[#{state['scan_count']}] {datetime.now().strftime('%H:%M:%S')} | {len(bets_received)} pari(s) recu(s) | VIP:{len(VIP_USERS)}")

            for bet in bets_received:
                bid = f"{bet.get('match')}_{bet.get('pick')}_{bet.get('stake')}"
                if bid in seen_ids:
                    continue
                seen_ids.add(bid)

                # Historique
                stats["history"].append(bet)
                if len(stats["history"]) > 100:
                    stats["history"].pop(0)

                # Detection suspect
                suspicion = check_suspicious(bet)
                if suspicion:
                    print(f"  {suspicion['level']} : {bet.get('match')}")
                    await notify_suspicious(session, bet, suspicion)

                # Consensus
                key = f"{bet.get('match')}_{bet.get('pick')}"
                recent = [t for t in consensus_tracker.get(key,[]) if (datetime.now()-t).seconds < 300]
                if len(recent) >= 3:
                    print(f"  CONSENSUS : {bet.get('match')} — {len(recent)} parieurs")
                    await notify_consensus(session, bet, len(recent))

                # VIP
                if is_vip(bet.get("user","")):
                    user = bet.get("user","")
                    stats["vip_consecutive"][user] += 1
                    stats["vip_bets"][user] += 1
                    consecutive = stats["vip_consecutive"][user]
                    print(f"  ⭐ VIP @{user} | {bet.get('match')} | x{bet.get('odd')}")
                    sent = await notify_vip(session, bet, consecutive)
                    if sent:
                        stats["total_sent"] += 1
                        stats["daily_sent"] += 1

                # Filtres normaux
                if matches_filters(bet):
                    stats["total_detected"] += 1
                    stats["daily_detected"] += 1
                    cfg = SPORT_CONFIG.get(bet.get("sport",""), {})
                    print(f"  DETECTE {cfg.get('emoji','')} {bet.get('match')} | x{bet.get('odd')} | ${bet.get('stake',0):,}")
                    send, rule = should_notify(bet)
                    if send:
                        sent = await notify_normal(session, bet, rule)
                        if sent:
                            stats["total_sent"] += 1
                            stats["daily_sent"] += 1

            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot arrete.")
