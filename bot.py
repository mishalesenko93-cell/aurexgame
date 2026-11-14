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
import os, json, hmac, hashlib, time, threading, urllib.request, urllib.parse, math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://mishalesenko93-cell.github.io/aurexgame/")
WEBAPP_URL += ("&" if "?" in WEBAPP_URL else "?") + "v=20261114-case-opening"
PORT = int(os.environ.get("PORT", "8080"))
ALLOW_ORIGIN = os.environ.get("ALLOW_ORIGIN", "https://mishalesenko93-cell.github.io")
CHANNEL = "https://t.me/AurexCompany"
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "purchases.json")
SCORES_FILE = os.path.join(DATA_DIR, "leaderboard.json")
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
}

lock = threading.Lock()


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
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def score_upsert(user, score):
    uid = str(user["id"])
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part).strip()
    name = name or user.get("username") or "Игрок"
    with lock:
        scores = scores_load()
        previous = scores.get(uid, {})
        scores[uid] = {
            "name": name[:64],
            "score": max(float(previous.get("score", 0)), score),
        }
        json_save(SCORES_FILE, scores)
        ordered = sorted(scores.items(), key=lambda entry: entry[1].get("score", 0), reverse=True)
        rank = next(i for i, (player_id, _) in enumerate(ordered, 1) if player_id == uid)
        players = [
            {"name": item.get("name", "Игрок"), "score": item.get("score", 0), "isMe": player_id == uid}
            for player_id, item in ordered
        ]
    return {"players": players, "rank": rank, "count": len(players)}


# ---------- Telegram API ----------
def tg(method, **params):
    req = urllib.request.Request(
        API + method,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


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

        if self.path == "/api/invoice":
            item_id = data.get("item")
            item = ITEMS.get(item_id)
            if not item:
                return self._send(400, {"error": "unknown item"})
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
            return self._send(200, score_upsert(user, float(score)))

        if self.path == "/api/leaderboard":
            return self._send(200, score_upsert(user, 0))

        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


# ---------- бот (long polling) ----------
def handle_update(u):
    if "pre_checkout_query" in u:
        q = u["pre_checkout_query"]
        item_id = q.get("invoice_payload", "").split(":")[0]
        ok = item_id in ITEMS
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
        if item_id in ITEMS:
            db_add(uid, item_id, sp["telegram_payment_charge_id"])
            tg("sendMessage", chat_id=chat_id,
               text=f"✅ Оплата получена: {ITEMS[item_id]['title']}\n"
                    f"Спасибо, что поддерживаешь AUREX COMPANY ❤️\nОткрой игру — покупка уже ждёт!")
        return

    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
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
