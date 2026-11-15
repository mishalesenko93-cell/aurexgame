"""
AUREX Miner — бот + API для оплаты Telegram Stars.
Без внешних зависимостей (только стандартная библиотека Python 3.8+).

Переменные окружения:
  BOT_TOKEN   — токен бота из @BotFather
  WEBAPP_URL  — https://mishalesenko93-cell.github.io/aurexgame/
  PORT        — 8080
  ALLOW_ORIGIN — https://mishalesenko93-cell.github.io
  DATA_DIR    — каталог постоянного диска (если настроен)
"""
import os, json, hmac, hashlib, time, threading, urllib.request, urllib.parse, math, secrets, uuid
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://mishalesenko93-cell.github.io/aurexgame/")
WEBAPP_URL += ("&" if "?" in WEBAPP_URL else "?") + "v=20261115-referrals-players"
PORT = int(os.environ.get("PORT", "8080"))
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "https://mishalesenko93-cell.github.io")
CHANNEL = "https://t.me/AurexCompany"
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "purchases.json")
SCORES_FILE = os.path.join(DATA_DIR, "leaderboard.json")
WINTER_FILE = os.path.join(DATA_DIR, "winter_2026.json")
REFERRALS_FILE = os.path.join(DATA_DIR, "referrals.json")
PLAYERS_FILE = os.path.join(DATA_DIR, "players.json")
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"

# Цены в Telegram Stars (XTR). Должны совпадать с index.html
ITEMS = {
    "turbo": {"title": "AUREX Turbo ×2",     "desc": "Все доходы ×2 навсегда",        "stars": 49},
    "auto":  {"title": "Авто-майнер",        "desc": "+250 монет/сек навсегда",        "stars": 99},
    "skin":  {"title": "Неоновая монета",    "desc": "Эксклюзивный скин AUREX",        "stars": 75},
    "pack":  {"title": "Пакет 100 000",      "desc": "Мгновенно +100 000 монет",       "stars": 25},
    "don10": {"title": "Поддержать AUREX",   "desc": "Спасибо за развитие! +10 000",   "stars": 10},
    "don50": {"title": "Большая поддержка",  "desc": "Респект от AurexCompany! +75 000","stars": 50},
    "platinum_case": {"title": "Platinium case", "desc": "Премиальный кейс AUREX", "stars": 100},
    "winter_pass": {"title": "AUREX WINTER 2026 Premium", "desc": "Premium-ветка зимнего сезона AUREX", "stars": 149},
}

lock = threading.Lock()

# Kyiv stays UTC+2 during this season; the end is exclusive (Mar 1, 00:00 Kyiv).
WINTER_START = datetime(2026, 11, 30, 22, 0, tzinfo=timezone.utc).timestamp()
WINTER_END = datetime(2027, 2, 28, 22, 0, tzinfo=timezone.utc).timestamp()
ARENA_DURATION = 30
ARENA_HIT_COOLDOWN = 0.3
DAILY_BP_XP_CAP = 250
REFERRAL_REWARD_COINS = 5000
REFERRAL_TAP_GOAL = 100
WINTER_DAILY_QUEST_POOL = [
    {"key": "first_round", "title": "Первый снежок", "description": "Заверши раунд в Snowball Arena", "counter": "rounds", "goal": 1, "xp": 35, "coins": 5000},
    {"key": "sharp_15", "title": "Меткий бросок", "description": "Попади по 15 мишеням", "counter": "hits", "goal": 15, "xp": 40, "coins": 7500},
    {"key": "two_rounds", "title": "Двойной залп", "description": "Заверши 2 раунда за день", "counter": "rounds", "goal": 2, "xp": 45, "coins": 8000},
    {"key": "snowstorm", "title": "Снежная буря", "description": "Попади по 30 мишеням", "counter": "hits", "goal": 30, "xp": 70, "coins": 15000},
    {"key": "three_rounds", "title": "На арене", "description": "Заверши 3 раунда за день", "counter": "rounds", "goal": 3, "xp": 65, "coins": 12000},
    {"key": "long_shift", "title": "Зимний марафон", "description": "Заверши 5 раундов за день", "counter": "rounds", "goal": 5, "xp": 100, "coins": 25000},
    {"key": "sharp_45", "title": "Снайпер метели", "description": "Попади по 45 мишеням", "counter": "hits", "goal": 45, "xp": 100, "coins": 25000},
]
WINTER_WEEKLY_QUEST_POOL = [
    {"key": "weekly_rounds", "title": "Ледяной марафон", "description": "Заверши 10 раундов за неделю", "counter": "rounds", "goal": 10, "xp": 250, "coins": 100000},
    {"key": "weekly_hits", "title": "Снежный снайпер", "description": "Попади по 200 мишеням за неделю", "counter": "hits", "goal": 200, "xp": 300, "coins": 120000},
    {"key": "weekly_master", "title": "Арена-мастер", "description": "Заверши 15 раундов за неделю", "counter": "rounds", "goal": 15, "xp": 350, "coins": 175000},
    {"key": "weekly_accuracy", "title": "Мастер меткости", "description": "Попади по 300 мишеням за неделю", "counter": "hits", "goal": 300, "xp": 400, "coins": 200000},
]


class WinterError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def winter_active(now=None):
    now = time.time() if now is None else now
    return WINTER_START <= now < WINTER_END


def winter_load():
    try:
        with open(WINTER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"users": {}, "runs": {}}
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict) or not isinstance(data.get("runs"), dict):
        raise ValueError("Invalid Winter season data")
    return data


def winter_save(data):
    json_save(WINTER_FILE, data)


def winter_periods(now):
    kyiv = datetime.fromtimestamp(now, timezone.utc) + timedelta(hours=2)
    iso = kyiv.isocalendar()
    return kyiv.date().isoformat(), f"{iso.year}-W{iso.week:02d}"


def winter_profile(data, user, now):
    uid = str(user["id"])
    name = " ".join(
        part.strip() for part in (user.get("first_name", ""), user.get("last_name", ""))
        if isinstance(part, str) and part.strip()
    )[:64] or (user.get("username", "Игрок")[:64] if isinstance(user.get("username"), str) else "Игрок")
    profile = data["users"].setdefault(uid, {
        "xp": 0,
        "premium": False,
        "offerDismissed": False,
        "claimed": {"free": [], "premium": []},
        "inventory": {
            "snowflakes": 0, "cases": 0, "boosters": 0,
            "avatars": [], "titles": [], "effects": [], "winterVip": False,
        },
        "daily": {},
        "weekly": {},
        "taskReceipts": {},
        "currencyReceipts": {},
        "caseOpenings": {},
        "arenaBest": 0,
        "boosterUntil": 0,
        "name": name,
    })
    profile["name"] = name
    profile.setdefault("xp", 0)
    profile.setdefault("premium", False)
    profile.setdefault("offerDismissed", False)
    profile.setdefault("claimed", {"free": [], "premium": []})
    profile.setdefault("inventory", {
        "snowflakes": 0, "cases": 0, "boosters": 0,
        "avatars": [], "titles": [], "effects": [], "winterVip": False,
    })
    profile.setdefault("caseOpenings", {})
    profile.setdefault("taskReceipts", {})
    profile.setdefault("currencyReceipts", {})
    profile.setdefault("arenaBest", 0)
    profile.setdefault("boosterUntil", 0)
    day, week = winter_periods(now)
    if profile.get("daily", {}).get("period") != day:
        profile["daily"] = {"period": day, "rounds": 0, "hits": 0, "xp": 0, "claimed": []}
    if profile.get("weekly", {}).get("period") != week:
        profile["weekly"] = {"period": week, "rounds": 0, "hits": 0, "claimed": []}
    return profile


def winter_reward(track, level):
    if track == "free":
        if level in (10, 30, 50):
            return {"id": f"free_case_{level}", "kind": "case", "name": "Зимний кейс", "amount": 1}
        if level in (15, 35):
            name = "Аватар «Ледяной волк»" if level == 15 else "Аватар «Снежная сова»"
            return {"id": f"free_avatar_{level}", "kind": "avatar", "name": name}
        if level in (25, 45):
            name = "Титул «Снежный боец»" if level == 25 else "Титул «Хранитель зимы»"
            return {"id": f"free_title_{level}", "kind": "title", "name": name}
        if level % 5 == 0:
            return {"id": f"free_booster_{level}", "kind": "booster", "name": "Бустер клика", "amount": 1}
        return {"id": f"free_snowflakes_{level}", "kind": "snowflakes", "name": "Снежинки", "amount": 15 + level}

    if level == 50:
        return {"id": "premium_winter_vip", "kind": "vip", "name": "Эксклюзивный Winter VIP"}
    if level in (10, 30):
        return {"id": f"premium_case_{level}", "kind": "case", "name": "Премиум зимний кейс", "amount": 2}
    if level in (15, 35):
        name = "Премиум-аватар «Полярная звезда»" if level == 15 else "Премиум-аватар «Аурора»"
        return {"id": f"premium_avatar_{level}", "kind": "avatar", "name": name}
    if level in (20, 40):
        name = "Золотой титул «Повелитель снега»" if level == 20 else "Золотой титул «Ледяная легенда»"
        return {"id": f"premium_title_{level}", "kind": "title", "name": name}
    if level in (25, 45):
        name = "Эффект «Ледяной след»" if level == 25 else "Эффект «Полярное сияние»"
        return {"id": f"premium_effect_{level}", "kind": "effect", "name": name}
    if level % 5 == 0:
        return {"id": f"premium_booster_{level}", "kind": "booster", "name": "Премиум-бустер клика", "amount": 2}
    return {"id": f"premium_snowflakes_{level}", "kind": "snowflakes", "name": "Снежинки", "amount": 40 + level * 3}


def winter_reward_catalog():
    return [
        {"level": level, "free": winter_reward("free", level), "premium": winter_reward("premium", level)}
        for level in range(1, 51)
    ]


def winter_add_xp(profile, amount):
    daily = profile["daily"]
    allowed = max(0, DAILY_BP_XP_CAP - daily["xp"])
    granted = min(max(0, int(amount)), allowed)
    profile["xp"] += granted
    daily["xp"] += granted
    return granted


def winter_add_quest_xp(profile, amount):
    granted = max(0, int(amount))
    profile["xp"] += granted
    profile["daily"]["xp"] += granted
    return granted


def winter_grant_reward(profile, reward):
    inventory = profile["inventory"]
    kind = reward["kind"]
    if kind == "snowflakes":
        inventory["snowflakes"] += reward["amount"]
    elif kind == "case":
        inventory["cases"] += reward["amount"]
    elif kind == "booster":
        inventory["boosters"] += reward["amount"]
    elif kind == "avatar" and reward["name"] not in inventory["avatars"]:
        inventory["avatars"].append(reward["name"])
    elif kind == "title" and reward["name"] not in inventory["titles"]:
        inventory["titles"].append(reward["name"])
    elif kind == "effect" and reward["name"] not in inventory["effects"]:
        inventory["effects"].append(reward["name"])
    elif kind == "vip":
        inventory["winterVip"] = True


def winter_active_quests(now):
    day, week = winter_periods(now)
    day_index = (datetime.fromisoformat(day).date().toordinal() * 3) % len(WINTER_DAILY_QUEST_POOL)
    week_year, week_number = int(week[:4]), int(week[6:])
    week_index = (week_year * 53 + week_number) % len(WINTER_WEEKLY_QUEST_POOL)
    return {
        "daily": [
            {
                **WINTER_DAILY_QUEST_POOL[(day_index + offset) % len(WINTER_DAILY_QUEST_POOL)],
                "id": f"daily_{day}_{WINTER_DAILY_QUEST_POOL[(day_index + offset) % len(WINTER_DAILY_QUEST_POOL)]['key']}",
                "period": "daily",
            }
            for offset in range(3)
        ],
        "weekly": [
            {
                **WINTER_WEEKLY_QUEST_POOL[week_index],
                "id": f"weekly_{week}_{WINTER_WEEKLY_QUEST_POOL[week_index]['key']}",
                "period": "weekly",
            }
        ],
    }


def winter_tasks(profile, now):
    result = {}
    for period, definitions in winter_active_quests(now).items():
        counters = profile["daily" if period == "daily" else "weekly"]
        result[period] = [
            {
                **task,
                "progress": min(counters.get(task["counter"], 0), task["goal"]),
                "claimed": task["id"] in counters["claimed"],
            }
            for task in definitions
        ]
    return result


def winter_state(profile, now):
    return {
        "active": True,
        "serverTime": now,
        "startAt": WINTER_START,
        "endAt": WINTER_END,
        "xp": profile["xp"],
        "level": min(50, profile["xp"] // 100),
        "xpInLevel": profile["xp"] % 100 if profile["xp"] < 5000 else 100,
        "premium": bool(profile["premium"]),
        "offerAvailable": not profile["premium"] and not profile["offerDismissed"],
        "claimed": profile["claimed"],
        "inventory": profile["inventory"],
        "tasks": winter_tasks(profile, now),
        "currencyReceipts": [
            receipt for receipt in profile["currencyReceipts"].values()
            if not receipt["acknowledged"]
        ],
        "arenaBest": profile["arenaBest"],
        "boosterUntil": profile["boosterUntil"],
        "rewards": winter_reward_catalog(),
    }


# ---------- хранилище покупок ----------
def db_load():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def json_save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def db_add(user_id, item, charge_id):
    with lock:
        db = db_load()
        rec = db.setdefault(str(user_id), [])
        if any(r.get("charge") == charge_id for r in rec):  # защита от дублей
            return
        rec.append({"item": item, "charge": charge_id, "ts": int(time.time())})
        json_save(DB_FILE, db)


def db_counts(user_id):
    with lock:
        rec = db_load().get(str(user_id), [])
    out = {}
    for r in rec:
        out[r["item"]] = out.get(r["item"], 0) + 1
    return out


def scores_load():
    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise RuntimeError("Could not read leaderboard data") from error
    if not isinstance(data, dict):
        raise RuntimeError("Invalid leaderboard data")
    return data


def score_upsert(user, score, taps=0):
    if isinstance(taps, bool) or not isinstance(taps, int) or not 0 <= taps <= 10**15:
        raise ValueError("Invalid tap count")
    uid = str(user["id"])
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part).strip()
    name = name or user.get("username") or "Игрок"
    with lock:
        scores = scores_load()
        for item in scores.values():
            if not isinstance(item, dict):
                raise RuntimeError("Invalid leaderboard player")
            if "tapped" not in item:
                item["tapped"] = float(item.get("score", 0)) > 0
        previous = scores.get(uid, {})
        tapped = previous.get("tapped", False) or taps > 0
        scores[uid] = {
            "name": name[:64],
            "score": max(float(previous.get("score", 0)), score),
            "tapped": tapped,
        }
        json_save(SCORES_FILE, scores)
        ordered = sorted(
            ((player_id, item) for player_id, item in scores.items() if item.get("tapped")),
            key=lambda entry: entry[1].get("score", 0),
            reverse=True,
        )
        rank = next((i for i, (player_id, _) in enumerate(ordered, 1) if player_id == uid), 0)
        players = [
            {"name": item.get("name", "Игрок"), "score": item.get("score", 0), "isMe": player_id == uid}
            for player_id, item in ordered
        ]
    return {"players": players, "rank": rank, "count": len(players)}


def player_count_record(user_id):
    uid = str(user_id)
    if not uid.isdigit():
        raise ValueError("Invalid Telegram user ID")
    with lock:
        try:
            with open(PLAYERS_FILE, "r", encoding="utf-8") as file:
                players = json.load(file)
        except FileNotFoundError:
            players = {}
        if not isinstance(players, dict):
            raise RuntimeError("Invalid player count data")
        if uid not in players:
            players[uid] = int(time.time())
            json_save(PLAYERS_FILE, players)
        return len(players)


# ---------- Telegram API ----------
def tg(method, **params):
    req = urllib.request.Request(
        API + method,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


bot_username_cache = None
bot_username_lock = threading.Lock()


def bot_username():
    global bot_username_cache
    with bot_username_lock:
        if bot_username_cache:
            return bot_username_cache
        response = tg("getMe")
        username = response.get("result", {}).get("username") if response.get("ok") else None
        if not isinstance(username, str) or not username:
            raise RuntimeError("Telegram did not return the bot username")
        bot_username_cache = username
        return username


def referrals_load():
    try:
        with open(REFERRALS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {"invitees": {}, "receipts": {}}
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("invitees"), dict)
        or not isinstance(data.get("receipts"), dict)
    ):
        raise RuntimeError("Invalid referral data")
    return data


def referral_register(inviter_id, invitee_id, now=None):
    inviter_id, invitee_id = str(inviter_id), str(invitee_id)
    if not inviter_id.isdigit() or not invitee_id.isdigit() or inviter_id == invitee_id:
        return False
    with lock:
        data = referrals_load()
        if invitee_id in data["invitees"] or invitee_id in scores_load():
            return False
        data["invitees"][invitee_id] = {
            "inviterId": inviter_id,
            "createdAt": int(time.time() if now is None else now),
            "activated": False,
        }
        json_save(REFERRALS_FILE, data)
    return True


def referral_activate(invitee_id, taps):
    invitee_id = str(invitee_id)
    if isinstance(taps, bool) or not isinstance(taps, int) or taps < REFERRAL_TAP_GOAL:
        return False
    with lock:
        data = referrals_load()
        invite = data["invitees"].get(invitee_id)
        if not invite or invite.get("activated"):
            return False
        invite["activated"] = True
        inviter_id = invite["inviterId"]
        receipt_id = uuid.uuid4().hex
        data["receipts"].setdefault(inviter_id, {})[receipt_id] = {
            "id": receipt_id,
            "amount": REFERRAL_REWARD_COINS,
            "inviteeId": invitee_id,
            "acknowledged": False,
        }
        json_save(REFERRALS_FILE, data)
    return True


def referral_state(user):
    uid = str(user["id"])
    username = bot_username()
    with lock:
        data = referrals_load()
        invitees = [
            item for item in data["invitees"].values()
            if item.get("inviterId") == uid
        ]
        pending_receipts = [
            receipt for receipt in data["receipts"].get(uid, {}).values()
            if not receipt.get("acknowledged")
        ]
        own_invite = data["invitees"].get(uid)
    return {
        "inviteLink": f"https://t.me/{username}?start=ref_{uid}",
        "invited": len(invitees),
        "activated": sum(1 for item in invitees if item.get("activated")),
        "rewardCoins": REFERRAL_REWARD_COINS,
        "tapGoal": REFERRAL_TAP_GOAL,
        "myReferral": (
            {"activated": bool(own_invite.get("activated"))}
            if own_invite else None
        ),
        "receipts": pending_receipts,
    }


def referral_ack(user_id, receipt_id):
    if not isinstance(receipt_id, str) or not receipt_id:
        return False
    uid = str(user_id)
    with lock:
        data = referrals_load()
        receipt = data["receipts"].get(uid, {}).get(receipt_id)
        if not receipt:
            return False
        receipt["acknowledged"] = True
        json_save(REFERRALS_FILE, data)
    return True


def referral_api(path, payload, user):
    if path == "/api/referrals/state":
        return 200, referral_state(user)
    if path == "/api/referrals/activate":
        taps = payload.get("taps")
        if isinstance(taps, bool) or not isinstance(taps, int) or not 0 <= taps <= 10_000_000:
            return 400, {"error": "invalid_taps"}
        return 200, {"activated": referral_activate(user["id"], taps)}
    if path == "/api/referrals/receipt/ack":
        if not referral_ack(user["id"], payload.get("receiptId")):
            return 404, {"error": "receipt_not_found"}
        return 200, {"ok": True}
    return 404, {"error": "not_found"}


def validate_init_data(init_data, max_age=86400):
    """Проверка подписи Telegram WebApp initData. Возвращает dict пользователя или None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got_hash = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs["user"])
    except Exception:
        return None


def winter_api(path, payload, user):
    now = time.time()
    if path == "/api/winter/state":
        if not winter_active(now):
            with lock:
                data = winter_load()
                profile = data["users"].get(str(user["id"]), {})
                receipts = [
                    receipt for receipt in profile.get("currencyReceipts", {}).values()
                    if not receipt["acknowledged"]
                ]
            return 200, {"active": False, "serverTime": now, "currencyReceipts": receipts}
        purchased_pass = db_counts(user["id"]).get("winter_pass", 0) > 0
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            if purchased_pass:
                profile["premium"] = True
            winter_save(data)
            return 200, winter_state(profile, now)

    if path == "/api/winter/receipt/ack":
        receipt_id = payload.get("receiptId")
        if not isinstance(receipt_id, str) or len(receipt_id) > 64:
            raise WinterError(400, "invalid_receipt")
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            receipt = profile["currencyReceipts"].get(receipt_id)
            if not receipt:
                raise WinterError(404, "receipt_not_found")
            receipt["acknowledged"] = True
            winter_save(data)
            return 200, {"ok": True}

    if not winter_active(now):
        raise WinterError(403, "season_not_active")

    if path == "/api/winter/offer-dismiss":
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            if not profile["premium"]:
                profile["offerDismissed"] = True
            winter_save(data)
            return 200, winter_state(profile, now)

    if path == "/api/winter/claim-task":
        task_id = payload.get("taskId")
        if not isinstance(task_id, str):
            raise WinterError(400, "invalid_task")
        task = next(
            (item for group in winter_active_quests(now).values() for item in group if item["id"] == task_id),
            None,
        )
        if task is None:
            raise WinterError(400, "invalid_task")
        period = task["period"]
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            counters = profile[period]
            if task_id in counters["claimed"]:
                return 200, {"alreadyClaimed": True, "state": winter_state(profile, now)}
            if counters.get(task["counter"], 0) < task["goal"]:
                raise WinterError(409, "task_not_completed")
            counters["claimed"].append(task_id)
            granted_xp = winter_add_quest_xp(profile, task["xp"])
            receipt_id = str(uuid.uuid4())
            profile["taskReceipts"][task_id] = receipt_id
            profile["currencyReceipts"][receipt_id] = {
                "id": receipt_id,
                "taskId": task_id,
                "amount": task["coins"],
                "acknowledged": False,
            }
            winter_save(data)
            return 200, {"xpGranted": granted_xp, "coinsAwarded": task["coins"], "state": winter_state(profile, now)}

    if path == "/api/winter/claim-reward":
        track, level = payload.get("track"), payload.get("level")
        if track not in ("free", "premium") or isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 50:
            raise WinterError(400, "invalid_reward")
        purchased_pass = track == "premium" and db_counts(user["id"]).get("winter_pass", 0) > 0
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            if purchased_pass:
                profile["premium"] = True
            if level > min(50, profile["xp"] // 100):
                raise WinterError(409, "level_not_reached")
            if track == "premium" and not profile["premium"]:
                raise WinterError(403, "premium_required")
            claimed = profile["claimed"].setdefault(track, [])
            if level in claimed:
                return 200, {"alreadyClaimed": True, "state": winter_state(profile, now)}
            reward = winter_reward(track, level)
            winter_grant_reward(profile, reward)
            claimed.append(level)
            winter_save(data)
            return 200, {"reward": reward, "state": winter_state(profile, now)}

    if path == "/api/winter/case/open":
        request_id = payload.get("requestId")
        if not isinstance(request_id, str) or len(request_id) > 64:
            raise WinterError(400, "invalid_request_id")
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            raise WinterError(400, "invalid_request_id")
        pool = [
            {"kind": "snowflakes", "name": "Снежинки", "amount": 25},
            {"kind": "snowflakes", "name": "Снежинки", "amount": 50},
            {"kind": "booster", "name": "Бустер клика", "amount": 1},
            {"kind": "avatar", "name": "Аватар «Снежный лис»"},
            {"kind": "title", "name": "Титул «Повелитель метели»"},
            {"kind": "effect", "name": "Эффект «Снежный вихрь»"},
        ]
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            previous = profile["caseOpenings"].get(request_id)
            if previous:
                return 200, {"reward": previous, "alreadyOpened": True, "state": winter_state(profile, now)}
            if profile["inventory"]["cases"] < 1:
                raise WinterError(409, "no_winter_cases")
            profile["inventory"]["cases"] -= 1
            reward = dict(secrets.choice(pool))
            inventory_key = {"avatar": "avatars", "title": "titles", "effect": "effects"}.get(reward["kind"])
            if inventory_key and reward["name"] in profile["inventory"][inventory_key]:
                reward = {"kind": "snowflakes", "name": "Снежинки", "amount": 25}
            reward["id"] = request_id
            winter_grant_reward(profile, reward)
            profile["caseOpenings"][request_id] = reward
            winter_save(data)
            return 200, {"reward": reward, "state": winter_state(profile, now)}

    if path == "/api/winter/booster/use":
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            inventory = profile["inventory"]
            if inventory["boosters"] < 1:
                raise WinterError(409, "no_winter_boosters")
            inventory["boosters"] -= 1
            profile["boosterUntil"] = min(max(now, profile["boosterUntil"]) + 60, now + 600)
            winter_save(data)
            return 200, winter_state(profile, now)

    if path == "/api/winter/arena/start":
        with lock:
            data = winter_load()
            profile = winter_profile(data, user, now)
            previous_id = profile.get("activeRun")
            previous = data["runs"].get(previous_id) if previous_id else None
            if previous and previous.get("status") == "active":
                if now <= previous["startedAt"] + ARENA_DURATION + 5:
                    raise WinterError(409, "arena_already_running")
                previous["status"] = "expired"
            for run_id, run in list(data["runs"].items()):
                if now - run.get("startedAt", now) > 14 * 86400 and run.get("status") != "active":
                    del data["runs"][run_id]
            run_id = str(uuid.uuid4())
            tokens = [secrets.token_urlsafe(18) for _ in range(30)]
            data["runs"][run_id] = {
                "userId": str(user["id"]),
                "startedAt": now,
                "status": "active",
                "tokens": tokens,
                "used": [],
                "hits": 0,
                "lastHitAt": 0,
            }
            profile["activeRun"] = run_id
            winter_save(data)
            return 200, {"runId": run_id, "duration": ARENA_DURATION, "targetTokens": tokens}

    if path == "/api/winter/arena/hit":
        run_id, token = payload.get("runId"), payload.get("targetToken")
        if not isinstance(run_id, str) or not isinstance(token, str):
            raise WinterError(400, "invalid_hit")
        with lock:
            data = winter_load()
            run = data["runs"].get(run_id)
            if not run or run.get("userId") != str(user["id"]):
                raise WinterError(404, "arena_run_not_found")
            if run.get("status") != "active" or now > run["startedAt"] + ARENA_DURATION:
                raise WinterError(409, "arena_run_closed")
            if token not in run["tokens"]:
                raise WinterError(400, "invalid_target")
            if token in run["used"]:
                raise WinterError(409, "target_already_hit")
            if now - run["lastHitAt"] < ARENA_HIT_COOLDOWN:
                raise WinterError(429, "hit_too_fast")
            run["used"].append(token)
            run["hits"] += 1
            run["lastHitAt"] = now
            winter_save(data)
            return 200, {"hits": run["hits"], "score": run["hits"] * 10}

    if path == "/api/winter/arena/finish":
        run_id = payload.get("runId")
        if not isinstance(run_id, str):
            raise WinterError(400, "invalid_run")
        with lock:
            data = winter_load()
            run = data["runs"].get(run_id)
            if not run or run.get("userId") != str(user["id"]):
                raise WinterError(404, "arena_run_not_found")
            if run.get("status") == "finished":
                return 200, {"alreadyFinished": True, "result": run["result"]}
            if run.get("status") != "active":
                raise WinterError(409, "arena_run_closed")
            if now < run["startedAt"] + ARENA_DURATION:
                raise WinterError(409, "arena_too_early")
            if now > run["startedAt"] + ARENA_DURATION + 15:
                run["status"] = "expired"
                winter_save(data)
                raise WinterError(410, "arena_run_expired")
            profile = winter_profile(data, user, now)
            hits = run["hits"]
            daily, weekly = profile["daily"], profile["weekly"]
            daily["rounds"] += 1
            daily["hits"] += hits
            weekly["rounds"] += 1
            weekly["hits"] += hits
            xp_granted = winter_add_xp(profile, min(45, 10 + hits))
            score = hits * 10
            profile["arenaBest"] = max(profile["arenaBest"], score)
            result = {
                "hits": hits,
                "score": score,
                "xpGranted": xp_granted,
                "arenaBest": profile["arenaBest"],
            }
            run["status"] = "finished"
            run["result"] = result
            winter_save(data)
            return 200, {"result": result}

    if path == "/api/winter/arena/leaderboard":
        with lock:
            data = winter_load()
            entries = [
                {"name": profile.get("name", "Игрок"), "score": profile.get("arenaBest", 0), "isMe": uid == str(user["id"])}
                for uid, profile in data["users"].items()
                if profile.get("arenaBest", 0) > 0
            ]
        entries.sort(key=lambda entry: entry["score"], reverse=True)
        return 200, {"players": entries[:10]}

    raise WinterError(404, "not_found")


# ---------- HTTP API для мини-аппа ----------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        self._send(200, {"ok": True, "service": "AUREX Miner API"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})

        user = validate_init_data(data.get("initData", ""))
        if not user:
            return self._send(401, {"error": "bad initData"})
        uid = user["id"]

        if self.path.startswith("/api/referrals/"):
            try:
                status, result = referral_api(self.path, data, user)
                return self._send(status, result)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                print("Referral storage/API error:", error)
                return self._send(500, {"error": "referral_service_error"})

        if self.path == "/api/players/open":
            try:
                return self._send(200, {"count": player_count_record(uid)})
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                print("Player count storage error:", error)
                return self._send(500, {"error": "player_count_unavailable"})

        if self.path.startswith("/api/winter/"):
            try:
                status, result = winter_api(self.path, data, user)
                return self._send(status, result)
            except WinterError as error:
                return self._send(error.status, {"error": error.code})
            except (OSError, ValueError) as error:
                print("Winter storage error:", error)
                return self._send(500, {"error": "winter_storage_error"})

        if self.path == "/api/invoice":
            item_id = data.get("item")
            item = ITEMS.get(item_id)
            if not item:
                return self._send(400, {"error": "unknown item"})
            if item_id == "winter_pass" and not winter_active():
                return self._send(403, {"error": "season_not_active"})
            if item_id == "winter_pass" and db_counts(uid).get("winter_pass", 0) > 0:
                return self._send(409, {"error": "winter_pass_already_owned"})
            try:
                res = tg(
                    "createInvoiceLink",
                    title=item["title"],
                    description=item["desc"],
                    payload=f"{item_id}:{uid}:{int(time.time())}",
                    currency="XTR",
                    prices=[{"label": item["title"], "amount": item["stars"]}],
                )
                if not res.get("ok"):
                    return self._send(500, {"error": res.get("description", "tg error")})
                return self._send(200, {"link": res["result"]})
            except Exception as e:
                return self._send(500, {"error": str(e)})

        if self.path == "/api/purchases":
            return self._send(200, {"items": db_counts(uid)})

        if self.path == "/api/score":
            score = data.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1e15 or not math.isfinite(score):
                return self._send(400, {"error": "invalid score"})
            taps = data.get("taps", 0)
            if isinstance(taps, bool) or not isinstance(taps, int) or not 0 <= taps <= 10**15:
                return self._send(400, {"error": "invalid taps"})
            try:
                return self._send(200, score_upsert(user, float(score), taps))
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                print("Leaderboard storage error:", error)
                return self._send(500, {"error": "leaderboard_unavailable"})

        if self.path == "/api/leaderboard":
            try:
                return self._send(200, score_upsert(user, 0))
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                print("Leaderboard storage error:", error)
                return self._send(500, {"error": "leaderboard_unavailable"})

        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


# ---------- бот (long polling) ----------
def handle_update(u):
    if "pre_checkout_query" in u:
        q = u["pre_checkout_query"]
        parts = q.get("invoice_payload", "").split(":")
        item_id = parts[0] if parts else ""
        item = ITEMS.get(item_id)
        try:
            owner_id = int(parts[1])
        except (IndexError, ValueError):
            owner_id = None
        ok = (
            item is not None
            and owner_id == q.get("from", {}).get("id")
            and q.get("currency") == "XTR"
            and q.get("total_amount") == item["stars"]
            and (item_id != "winter_pass" or winter_active())
        )
        tg("answerPreCheckoutQuery", pre_checkout_query_id=q["id"], ok=ok,
           **({} if ok else {"error_message": "Товар недоступен"}))
        return

    msg = u.get("message")
    if not msg:
        return
    chat_id = msg["chat"]["id"]

    sp = msg.get("successful_payment")
    if sp:
        parts = sp.get("invoice_payload", "").split(":")
        item_id, uid = parts[0], msg["from"]["id"]
        item = ITEMS.get(item_id)
        try:
            owner_id = int(parts[1])
        except (IndexError, ValueError):
            owner_id = None
        if (
            item is not None
            and owner_id == uid
            and sp.get("currency") == "XTR"
            and sp.get("total_amount") == item["stars"]
        ):
            db_add(uid, item_id, sp["telegram_payment_charge_id"])
            tg("sendMessage", chat_id=chat_id,
               text=f"✅ Оплата получена: {ITEMS[item_id]['title']}\n"
                    f"Спасибо, что поддерживаешь AUREX COMPANY ❤️\nОткрой игру — покупка уже ждёт!")
        else:
            print("Rejected Telegram payment with invalid item, owner, currency, amount, or season")
        return

    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        start_parts = text.split(maxsplit=1)
        payload = start_parts[1].strip() if len(start_parts) > 1 else ""
        if (
            (msg.get("chat") or {}).get("type") == "private"
            and payload.startswith("ref_")
            and payload[4:].isdigit()
        ):
            invited_user_id = (msg.get("from") or {}).get("id")
            if invited_user_id is not None:
                try:
                    referral_register(payload[4:], invited_user_id)
                except (OSError, RuntimeError, ValueError) as error:
                    print("Referral registration error:", error)
        tg("sendMessage", chat_id=chat_id,
           text="🪙 *AUREX Miner* — майни монеты, прокачивай IT-империю и лови багов!\n\n"
                "Игра от AUREX COMPANY. Жми кнопку ниже 👇",
           parse_mode="Markdown",
           reply_markup={"inline_keyboard": [
               [{"text": "🎮 Играть", "web_app": {"url": WEBAPP_URL}}],
               [{"text": "📢 Канал AurexCompany", "url": CHANNEL}],
           ]})
    elif text.startswith("/paysupport"):
        tg("sendMessage", chat_id=chat_id,
           text="По вопросам оплаты и возвратов Stars пиши в канал/владельцу AUREX COMPANY: "
                + CHANNEL + "\nУкажи ID платежа из чека Telegram.")
    elif text.startswith("/terms"):
        tg("sendMessage", chat_id=chat_id,
           text="Покупки в AUREX Miner — цифровые товары внутри игры, оплачиваются Telegram Stars.")


def poll():
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=30,
                     allowed_updates=["message", "pre_checkout_query"])
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception as e:
                    print("handler error:", e)
        except Exception as e:
            print("poll error:", e)
            time.sleep(3)


if __name__ == "__main__":
    # включаем кнопку меню «Играть» у бота
    try:
        tg("setChatMenuButton", menu_button={"type": "web_app", "text": "🎮 Играть", "web_app": {"url": WEBAPP_URL}})
    except Exception as e:
        print("menu button:", e)
    threading.Thread(target=poll, daemon=True).start()
    print(f"API on :{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
