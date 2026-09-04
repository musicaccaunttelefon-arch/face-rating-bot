import logging
import os
import numpy as np
import asyncio
import hashlib
from datetime import datetime, timedelta
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
import pg8000
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
RENDER_URL     = os.environ.get("RENDER_URL", "")
DATABASE_URL   = os.environ.get("DATABASE_URL", "")
PORT           = int(os.environ.get("PORT", 8080))
ADMIN_USERNAME = "nerealnytalanty"
ADMIN_CARD     = "2202208039479622\nZELENTSOV IVAN"

user_data_store    = {}
matchmaking_queues = {"male": [], "female": [], "premium_male": [], "premium_female": []}
match_store        = {}

# ──────────────────────────────────────────────
# БД
# ──────────────────────────────────────────────

def get_conn():
    from urllib.parse import urlparse
    p = urlparse(DATABASE_URL)
    return pg8000.connect(
        host=p.hostname,
        port=p.port or 5432,
        database=p.path.lstrip("/"),
        user=p.username,
        password=p.password,
        ssl_context=True,
    )


import re as _re

class _Cur:
    def __init__(self, c): self._c = c
    def execute(self, sql, params=None):
        if params:
            i = [0]
            def rep(m): i[0]+=1; return f"${i[0]}"
            sql = _re.sub(r"%s", rep, sql)
        self._c.execute(sql, list(params) if params else [])
    def fetchone(self): return self._c.fetchone()
    def fetchall(self): return self._c.fetchall()

class _Conn:
    def __init__(self, c): self._c = c
    def cursor(self): return _Cur(self._c.cursor())
    def commit(self): self._c.commit()
    def close(self): self._c.close()

def new_conn():
    return _Conn(get_conn())

def init_db():
    conn = new_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id         BIGINT PRIMARY KEY,
            username        TEXT,
            score           REAL DEFAULT 0,
            category        TEXT DEFAULT '',
            gender          TEXT DEFAULT '',
            wins            INTEGER DEFAULT 0,
            losses          INTEGER DEFAULT 0,
            matches         INTEGER DEFAULT 0,
            in_leaderboard  INTEGER DEFAULT 1,
            profile_file_id TEXT DEFAULT NULL,
            is_premium      BOOLEAN DEFAULT FALSE,
            premium_until   TIMESTAMP DEFAULT NULL,
            is_beta         BOOLEAN DEFAULT FALSE
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT,
            score      REAL,
            category   TEXT,
            photo_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS updates (
            id         SERIAL PRIMARY KEY,
            title      TEXT,
            content    TEXT,
            level      TEXT DEFAULT 'public',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS premium_requests (
            id         SERIAL PRIMARY KEY,
            user_id    BIGINT,
            username   TEXT,
            plan       TEXT,
            status     TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def get_player(user_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("""SELECT username,score,category,wins,losses,matches,gender,in_leaderboard,
                        profile_file_id,is_premium,premium_until,is_beta
                 FROM players WHERE user_id=%s""", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def save_player(user_id, username, score, category, gender):
    conn = new_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO players (user_id,username,score,category,gender)
        VALUES (%s,%s,%s,%s,%s)
        ON CONFLICT (user_id) DO UPDATE SET
            username=EXCLUDED.username, score=EXCLUDED.score,
            category=EXCLUDED.category, gender=EXCLUDED.gender
    """, (user_id, username, score, category, gender))
    conn.commit()
    conn.close()

def save_history(user_id, score, category, photo_hash):
    conn = new_conn()
    c = conn.cursor()
    c.execute("INSERT INTO history (user_id,score,category,photo_hash) VALUES (%s,%s,%s,%s)",
              (user_id, score, category, photo_hash))
    conn.commit()
    conn.close()

def is_duplicate_photo(user_id, photo_hash):
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM history WHERE user_id=%s AND photo_hash=%s", (user_id, photo_hash))
    row = c.fetchone()
    conn.close()
    return row is not None

def set_profile_photo(user_id, file_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("UPDATE players SET profile_file_id=%s WHERE user_id=%s", (file_id, user_id))
    conn.commit()
    conn.close()

def toggle_leaderboard(user_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("UPDATE players SET in_leaderboard=1-in_leaderboard WHERE user_id=%s", (user_id,))
    conn.commit()
    c.execute("SELECT in_leaderboard FROM players WHERE user_id=%s", (user_id,))
    val = c.fetchone()
    conn.close()
    return val[0] if val else 1

def update_match_result(winner_id, loser_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("UPDATE players SET wins=wins+1, matches=matches+1 WHERE user_id=%s", (winner_id,))
    c.execute("UPDATE players SET losses=losses+1, matches=matches+1 WHERE user_id=%s", (loser_id,))
    conn.commit()
    conn.close()

def get_leaderboard(gender=None, premium_only=False):
    conn = new_conn()
    c = conn.cursor()
    base = "SELECT username,score,category,wins,losses,is_premium FROM players WHERE in_leaderboard=1"
    params = []
    if gender:
        base += " AND gender=%s"; params.append(gender)
    if premium_only:
        base += " AND is_premium=TRUE"
    base += " ORDER BY score DESC LIMIT 10"
    c.execute(base, params)
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_profiles(offset=0, premium_only=False):
    conn = new_conn()
    c = conn.cursor()
    if premium_only:
        c.execute("""SELECT user_id,score,category,gender,wins,losses,matches,profile_file_id,is_premium
                     FROM players WHERE is_premium=TRUE ORDER BY score DESC LIMIT 1 OFFSET %s""", (offset,))
        c2 = conn.cursor()
        c2.execute("SELECT COUNT(*) FROM players WHERE is_premium=TRUE")
    else:
        c.execute("""SELECT user_id,score,category,gender,wins,losses,matches,profile_file_id,is_premium
                     FROM players ORDER BY score DESC LIMIT 1 OFFSET %s""", (offset,))
        c2 = conn.cursor()
        c2.execute("SELECT COUNT(*) FROM players")
    row   = c.fetchone()
    total = c2.fetchone()[0]
    conn.close()
    return row, total

def get_history(user_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT score,category,created_at FROM history WHERE user_id=%s ORDER BY created_at DESC LIMIT 10", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_user_ids():
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM players")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_premium_user_ids():
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM players WHERE is_premium=TRUE")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def get_beta_user_ids():
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM players WHERE is_beta=TRUE")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

def set_premium(user_id, months):
    conn = new_conn()
    c = conn.cursor()
    until = datetime.now() + timedelta(days=30*months)
    c.execute("UPDATE players SET is_premium=TRUE, premium_until=%s WHERE user_id=%s", (until, user_id))
    conn.commit()
    conn.close()
    return until

def revoke_premium(user_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("UPDATE players SET is_premium=FALSE, premium_until=NULL WHERE user_id=%s", (user_id,))
    conn.commit()
    conn.close()

def set_beta(user_id, val):
    conn = new_conn()
    c = conn.cursor()
    c.execute("UPDATE players SET is_beta=%s WHERE user_id=%s", (val, user_id))
    conn.commit()
    conn.close()

def is_admin(username):
    return (username or "").lower() == ADMIN_USERNAME.lower()

def is_beta_access(user_id):
    row = get_player(user_id)
    if not row: return False
    return row[11] or is_admin(row[0])

def is_premium_user(user_id):
    row = get_player(user_id)
    if not row: return False
    return bool(row[9])

# ── Запросы на премиум ──
def save_premium_request(user_id, username, plan):
    conn = new_conn()
    c = conn.cursor()
    c.execute("INSERT INTO premium_requests (user_id,username,plan) VALUES (%s,%s,%s)", (user_id, username, plan))
    conn.commit()
    conn.close()

def get_pending_requests():
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT id,user_id,username,plan,created_at FROM premium_requests WHERE status='pending' ORDER BY created_at")
    rows = c.fetchall()
    conn.close()
    return rows

def approve_request(req_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT user_id,plan FROM premium_requests WHERE id=%s", (req_id,))
    row = c.fetchone()
    if row:
        user_id, plan = row
        months = {"1m":1,"3m":3,"6m":6,"12m":12}.get(plan, 1)
        until = datetime.now() + timedelta(days=30*months)
        c.execute("UPDATE players SET is_premium=TRUE, premium_until=%s WHERE user_id=%s", (until, user_id))
        c.execute("UPDATE premium_requests SET status='approved' WHERE id=%s", (req_id,))
        conn.commit()
        conn.close()
        return user_id, months
    conn.close()
    return None, None

def reject_request(req_id):
    conn = new_conn()
    c = conn.cursor()
    c.execute("SELECT user_id FROM premium_requests WHERE id=%s", (req_id,))
    row = c.fetchone()
    c.execute("UPDATE premium_requests SET status='rejected' WHERE id=%s", (req_id,))
    conn.commit()
    conn.close()
    return row[0] if row else None

# ── Апдейты ──
def save_update(title, content, level):
    conn = new_conn()
    c = conn.cursor()
    c.execute("INSERT INTO updates (title,content,level) VALUES (%s,%s,%s)", (title, content, level))
    conn.commit()
    conn.close()

def get_updates(level="public"):
    conn = new_conn()
    c = conn.cursor()
    if level == "admin":
        c.execute("SELECT title,content,level,created_at FROM updates ORDER BY created_at DESC LIMIT 5")
    elif level == "premium":
        c.execute("SELECT title,content,level,created_at FROM updates WHERE level IN ('public','premium','beta') ORDER BY created_at DESC LIMIT 5")
    elif level == "beta":
        c.execute("SELECT title,content,level,created_at FROM updates WHERE level IN ('public','premium','beta') ORDER BY created_at DESC LIMIT 5")
    else:
        c.execute("SELECT title,content,level,created_at FROM updates WHERE level='public' ORDER BY created_at DESC LIMIT 5")
    rows = c.fetchall()
    conn.close()
    return rows

# ── Сброс рейтинга ──
def reset_all_ratings():
    conn = new_conn()
    c = conn.cursor()
    c.execute("UPDATE players SET score=0, category='', wins=0, losses=0, matches=0")
    c.execute("DELETE FROM history")
    conn.commit()
    conn.close()

# ──────────────────────────────────────────────
# КАТЕГОРИИ
# ──────────────────────────────────────────────

MALE_CATEGORIES = [
    ("sub 3",     0,  20, "😔", "Очень низкая привлекательность."),
    ("sub 5",    20,  35, "😐", "Ниже среднего. Слабые черты лица."),
    ("ltn",      35,  50, "🙂", "Обычный парень. Среднестатистическая внешность."),
    ("mtn",      50,  62, "😊", "Чуть выше среднего. Аккуратные черты."),
    ("htn",      62,  74, "😎", "Привлекательный мужчина. Хорошая симметрия."),
    ("chad",     74,  88, "🔥", "Очень привлекательный. Сильные мужские черты."),
    ("true adam",88, 101, "👑", "Идеальный мужчина. Эталонные черты лица."),
]
FEMALE_CATEGORIES = [
    ("sub 3",    0,  20, "😔", "Очень низкая привлекательность."),
    ("sub 5",   20,  35, "😐", "Ниже среднего. Нет женственности в чертах."),
    ("ltb",     35,  50, "🙂", "Обычная девушка. Нейтральные черты."),
    ("mtb",     50,  62, "😊", "Чуть выше среднего. Мягкие приятные черты."),
    ("htb",     62,  74, "😍", "Привлекательная. Хорошие женственные черты."),
    ("stacy",   74,  88, "🔥", "Красивая девушка. Выраженные красивые черты."),
    ("true eve",88, 101, "👑", "Идеальная женщина. Безупречная симметрия и гармония."),
]
CATEGORY_RANK = {
    "sub 3":0,"sub 5":1,"ltn":2,"ltb":2,"mtn":3,"mtb":3,
    "htn":4,"htb":4,"chad":5,"stacy":5,"true adam":6,"true eve":6,
}

def get_category(score, gender):
    cats = MALE_CATEGORIES if gender == "male" else FEMALE_CATEGORIES
    for name, low, high, emoji, desc in cats:
        if low <= score < high:
            return name, emoji, desc
    return cats[-1][0], cats[-1][3], cats[-1][4]

# ──────────────────────────────────────────────
# СОВЕТЫ
# ──────────────────────────────────────────────

TIPS = {
    "male": {
        "sub 3":     [("Масса тела","Нормализовать массу тела.","🔴"),("Кожа","Решить проблемы с кожей у дерматолога.","🔴"),("Осанка","Исправить осанку.","🔴"),("Волосы","Подобрать подходящую стрижку.","🟡"),("Здоровье","Нормализовать сон, питание, физическую активность.","🔴"),],
        "sub 5":     [("Масса тела","Снизить процент жира.","🔴"),("Физ. форма","Набрать мышечную массу.","🔴"),("Кожа","Регулярный уход за кожей.","🟡"),("Волосы","Экспериментировать со стрижкой.","🟡"),("Зубы","Отбеливание зубов при необходимости.","🟢"),],
        "ltn":       [("Волосы","Улучшить причёску.","🔴"),("Физ. форма","Развивать шею и трапеции.","🟡"),("Кожа","Уход за кожей.","🟡"),("Осанка","Исправить осанку.","🟡"),("Стиль","Подобрать стиль одежды.","🟡"),],
        "mtn":       [("Масса тела","Поддерживать низкий процент жира.","🟡"),("Кожа","Следить за кожей.","🟡"),("Физ. форма","Развивать спортивную форму.","🟡"),("Волосы","Экспериментировать с причёской.","🟢"),],
        "htn":       [("Физ. форма","Поддерживать форму.","🟡"),("Кожа","Следить за кожей.","🟢"),("Волосы","Регулярно стричься.","🟢"),("Харизма","Работать над уверенностью.","🟢"),],
        "chad":      [("Физ. форма","Просто поддерживать текущую форму.","🟢"),("Кожа","Следить за здоровьем кожи.","🟢"),],
        "true adam": [("Здоровье","Поддерживать здоровье.","🟢"),("Стиль","Не терять индивидуальный стиль.","🟢"),],
    },
    "female": {
        "sub 3":     [("Кожа","Консультация дерматолога при проблемной коже.","🔴"),("Волосы","Подобрать причёску под форму лица.","🔴"),("Уход","Освоить базовый уход за кожей.","🔴"),],
        "sub 5":     [("Волосы","Улучшить уход за волосами.","🔴"),("Кожа","Следить за состоянием кожи.","🔴"),("Макияж","Лёгкий естественный макияж.","🟡"),("Брови","Подобрать форму бровей.","🟡"),],
        "ltb":       [("Волосы","Найти подходящую стрижку.","🔴"),("Кожа","Использовать уходовую косметику.","🟡"),("Стиль","Подобрать стиль одежды.","🟡"),],
        "mtb":       [("Кожа","Регулярный уход за кожей.","🟡"),("Физ. форма","Поддерживать физическую форму.","🟡"),("Стиль","Экспериментировать с образом.","🟢"),],
        "htb":       [("Уход","Поддерживать текущий уход.","🟢"),("Кожа","Защита кожи от солнца.","🟢"),],
        "stacy":     [("Физ. форма","Поддерживать форму.","🟢"),("Кожа","Беречь кожу.","🟢"),("Косметика","Не злоупотреблять косметическими процедурами.","🟢"),],
        "true eve":  [("Здоровье","Поддерживать здоровье.","🟢"),("Стиль","Сохранять естественный внешний вид.","🟢"),],
    }
}

def get_tips_text(category, gender):
    tips = TIPS.get(gender, {}).get(category, [])
    if not tips: return "Советы не найдены."
    text = f"💡 *Советы для {category.upper()}:*\n\n"
    for area, tip, priority in tips:
        text += f"{priority} *{area}*\n_{tip}_\n\n"
    return text

# ──────────────────────────────────────────────
# АНАЛИЗ ФОТО
# ──────────────────────────────────────────────

def get_photo_hash(image_bytes):
    return hashlib.md5(image_bytes).hexdigest()

def check_edit_level(img_gray):
    import cv2
    return round(max(0, min(100, 100 - cv2.Laplacian(img_gray, cv2.CV_64F).var() / 5)), 1)

def check_screen_photo(img_bgr):
    import cv2
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    fshift = np.fft.fftshift(np.fft.fft2(gray))
    magnitude = 20 * np.log(np.abs(fshift) + 1)
    h, w = magnitude.shape
    center = magnitude[h//2-10:h//2+10, w//2-10:w//2+10]
    outer  = magnitude.copy(); outer[h//2-20:h//2+20, w//2-20:w//2+20] = 0
    return (outer.mean() / max(center.mean(), 1)) > 0.15

def analyze_face(image_bytes, gender):
    try:
        import cv2
        nparr    = np.frombuffer(image_bytes, np.uint8)
        img_bgr  = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img_bgr is None: return {"error": "Не удалось прочитать изображение."}
        img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        h, w     = img_bgr.shape[:2]
        is_screen  = check_screen_photo(img_bgr)
        edit_level = check_edit_level(img_gray)
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        eye_cascade  = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
        faces = face_cascade.detectMultiScale(img_gray, 1.1, 5, minSize=(80,80))
        if len(faces) == 0: return {"error": "Лицо не обнаружено. Пришли чёткое фото анфас."}
        fx, fy, fw, fh = max(faces, key=lambda f: f[2]*f[3])
        scores = {}
        scores["golden_ratio"] = max(0, 100 - abs(fh/max(fw,1)-1.618)/1.618*180)
        face_img   = img_gray[fy:fy+fh, fx:fx+fw]
        mid        = fw//2
        left_half  = face_img[:, :mid]
        right_half = cv2.flip(face_img[:, mid:], 1)
        min_w      = min(left_half.shape[1], right_half.shape[1])
        diff       = cv2.absdiff(left_half[:,:min_w].astype(float), right_half[:,:min_w].astype(float))
        scores["symmetry"] = max(0, 100 - min(100, diff.mean()/128*200))
        face_top = img_gray[fy:fy+fh//2, fx:fx+fw]
        eyes = eye_cascade.detectMultiScale(face_top, 1.1, 3)
        if len(eyes) >= 2:
            eyes = sorted(eyes, key=lambda e: e[0])
            ex1,ey1,ew1,_ = eyes[0]; ex2,ey2,ew2,_ = eyes[1]
            scores["eye_spacing"] = max(0, 100-abs(abs((ex2+ew2//2)-(ex1+ew1//2))/max((ew1+ew2)/2,1)-2.5)/2.5*100)
            scores["eye_level"]   = max(0, 100-abs(ey1-ey2)/max(fh,1)*500)
        else:
            scores["eye_spacing"] = 50; scores["eye_level"] = 50
        scores["face_clarity"] = min(100, fw*fh/max(w*h,1)*400)
        weights = {"symmetry":0.35,"golden_ratio":0.25,"eye_spacing":0.20,"eye_level":0.10,"face_clarity":0.10}
        total = max(0, min(100, sum(scores[k]*weights[k] for k in weights)))
        return {"score": round(total,1), "details": scores, "edit_level": edit_level, "is_screen": is_screen, "error": None}
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}", exc_info=True)
        return {"error": f"Ошибка: {str(e)[:100]}"}

# ──────────────────────────────────────────────
# МЕНЮ
# ──────────────────────────────────────────────

PREMIUM_TEXT = """💎 *Premium «Здесь моггают»*

Premium на старте проекта даёт несколько преимуществ:

• ⚡ *Приоритет в матчмейкинге* — поиск соперника проходит быстрее за счёт повышенного приоритета в очереди.

• 🔒 *Ранний доступ к бета-обновлениям* — новые функции раньше остальных.

• 💎 *Premium-статус* — специальная отметка в профиле.

• ❤️ *Поддержка проекта* — средства идут на развитие «Здесь моггают».

━━━━━━━━━━━━━━━
💳 *Тарифы:*
• 1 месяц — 99 ₽
• 3 месяца — 249 ₽
• 6 месяцев — 399 ₽
• 1 год — 699 ₽
━━━━━━━━━━━━━━━

Выбери тариф и оплати по реквизитам:
`{card}`

После оплаты нажми *«Я оплатил»* — владелец подтвердит вручную."""

def main_menu_keyboard(user_id=None, username=None):
    premium = is_premium_user(user_id) if user_id else False
    buttons = [
        [InlineKeyboardButton("📸 Оценить внешность", callback_data="rate_me")],
        [InlineKeyboardButton("⚔️ ММ Мужчины", callback_data="mm_male"),
         InlineKeyboardButton("⚔️ ММ Женщины", callback_data="mm_female")],
        [InlineKeyboardButton("🏆 Топ мужчин", callback_data="lb_male"),
         InlineKeyboardButton("🏆 Топ женщин", callback_data="lb_female")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
         InlineKeyboardButton("🔍 Найти профиль", callback_data="browse_0")],
        [InlineKeyboardButton("💎 Premium профили", callback_data="premium_browse_0"),
         InlineKeyboardButton("📢 Обновления", callback_data="updates")],
        [InlineKeyboardButton("💘 Знакомства", callback_data="dt_toggle"),
         InlineKeyboardButton("⚡ Пригласить друга", callback_data="referral_link")],
    ]
    if not premium:
        buttons.append([InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium")])
    return InlineKeyboardMarkup(buttons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.username
    # Deep Link реферал
    args = context.args
    if args and args[0].startswith("ref_"):
        try:
            referrer_id = int(args[0][4:])
            if referrer_id != user_id and is_new_user(user_id):
                if register_referral(user_id, referrer_id):
                    try:
                        await context.bot.send_message(chat_id=referrer_id, text="🎉 По вашей ссылке зарегистрировался друг!\nВам начислено *+3 расширенных анализа!* ⚡", parse_mode="Markdown")
                    except Exception:
                        pass
        except Exception:
            pass
    await update.message.reply_text(
        "👋 Привет! Я оцениваю внешность по геометрии лица.\n\nВыбери действие:",
        reply_markup=main_menu_keyboard(user_id, username),
        parse_mode="Markdown"
    )

# ──────────────────────────────────────────────
# ФОТО
# ──────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    photo     = update.message.photo[-1]
    mode      = user_data_store.get(user_id, {}).get("mode", "rate")
    mm_gender = user_data_store.get(user_id, {}).get("mm_gender")
    user_data_store[user_id] = {"file_id": photo.file_id, "mode": mode, "mm_gender": mm_gender}

    # Dating FSM photo
    if mode == "dating_fsm" and user_data_store.get(user_id, {}).get("step") == "photo":
        if await handle_dating_photo(user_id, photo.file_id, update, context): return

    if mode == "set_profile_photo":
        set_profile_photo(user_id, photo.file_id)
        user_data_store.pop(user_id, None)
        await update.message.reply_text("✅ Фото профиля обновлено!", reply_markup=main_menu_keyboard(user_id, update.effective_user.username))
        return

    if mode == "match":
        keyboard = [[InlineKeyboardButton("👨 Мужчина" if mm_gender=="male" else "👩 Женщина", callback_data=f"gender_{mm_gender}_match")]]
        await update.message.reply_text("Фото получено! Нажми чтобы подтвердить:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard = [[InlineKeyboardButton("👨 Мужчина", callback_data="gender_male_rate"), InlineKeyboardButton("👩 Женщина", callback_data="gender_female_rate")]]
        await update.message.reply_text("Фото получено! Укажи пол:", reply_markup=InlineKeyboardMarkup(keyboard))

# ──────────────────────────────────────────────
# CALLBACKS
# ──────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    data     = query.data
    user_id  = update.effective_user.id
    user     = update.effective_user
    username = user.username or ""

    def menu(): return main_menu_keyboard(user_id, username)

    if data == "rate_me":
        user_data_store[user_id] = {"mode": "rate"}
        await query.edit_message_text("📸 Пришли фото лица анфас!")
        return

    # ── Таблицы лидеров ──
    if data in ("lb_male","lb_female","lb_premium"):
        premium_only = data == "lb_premium"
        gender = "male" if data=="lb_male" else ("female" if data=="lb_female" else None)
        label  = "Premium 💎" if premium_only else ("мужчин 👨" if gender=="male" else "женщин 👩")
        rows   = get_leaderboard(gender, premium_only)
        if not rows:
            await query.edit_message_text(f"🏆 Топ {label} пуст!", reply_markup=menu()); return
        text   = f"🏆 *Топ {label}:*\n\n"
        medals = ["🥇","🥈","🥉"]
        for i, (uname, score, cat, wins, losses, is_prem) in enumerate(rows):
            medal  = medals[i] if i < 3 else f"{i+1}."
            p_icon = " 💎" if is_prem else ""
            text  += f"{medal} {uname or 'Аноним'}{p_icon} — *{cat}* ({score:.0f} б) | {wins}W/{losses}L\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=menu()); return

    # ── Профиль ──
    if data == "profile":
        row = get_player(user_id)
        if not row:
            await query.edit_message_text("👤 Профиля нет. Пройди оценку!", reply_markup=menu()); return
        uname, score, cat, wins, losses, matches, gender, in_lb, profile_pic, is_prem, prem_until, is_beta = row
        winrate   = round(wins/matches*100) if matches > 0 else 0
        icon      = "👨" if gender=="male" else "👩"
        prem_icon = " 💎" if is_prem else ""
        lb_status = "✅ Да" if in_lb else "❌ Нет"
        prem_text = f"\n💎 Premium до: {str(prem_until)[:10]}" if is_prem and prem_until else ""
        history   = get_history(user_id)
        hist_text = ""
        if history:
            hist_text = "\n\n📈 *История оценок:*\n"
            for h_score, h_cat, h_date in history:
                hist_text += f"• {str(h_date)[:10]} — *{h_cat}* ({h_score:.0f} б)\n"
        text = (
            f"{icon} *Твой профиль*{prem_icon}\n\n"
            f"🎯 Категория: *{cat.upper()}*\n"
            f"📊 Балл: *{score:.1f}/100*\n\n"
            f"⚔️ Матчей: {matches} | ✅ {wins}W / ❌ {losses}L\n"
            f"📈 Винрейт: {winrate}%\n"
            f"🏆 В таблице: {lb_status}{prem_text}"
            f"{hist_text}"
        )
        btns = [
            [InlineKeyboardButton("🖼 Сменить фото профиля", callback_data="set_pfp")],
            [InlineKeyboardButton("👁 Скрыть из таблицы" if in_lb else "👁 Показать в таблице", callback_data="toggle_lb")],
        ]
        if is_admin(username):
            btns.append([InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")])
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="back_menu")])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns)); return

    if data == "set_pfp":
        user_data_store[user_id] = {"mode": "set_profile_photo"}
        await query.edit_message_text("📸 Пришли фото которое хочешь поставить на профиль!"); return

    if data == "toggle_lb":
        new_val = toggle_leaderboard(user_id)
        await query.edit_message_text("✅ Ты теперь в таблице!" if new_val else "❌ Скрыт из таблицы.", reply_markup=menu()); return

    if data == "back_menu":
        await query.edit_message_text("Выбери действие:", reply_markup=menu()); return

    # ── Обновления ──
    if data == "updates":
        row = get_player(user_id)
        if row and row[9]: level = "premium"
        elif row and row[11]: level = "beta"
        elif is_admin(username): level = "admin"
        else: level = "public"
        rows = get_updates(level)
        if not rows:
            await query.edit_message_text("📢 Обновлений пока нет.", reply_markup=menu()); return
        text = "📢 *Последние обновления:*\n\n"
        level_icons = {"admin":"👑","beta":"🔒","premium":"💎","public":"📢"}
        for title, content, lvl, created_at in rows:
            text += f"{level_icons.get(lvl,'📢')} *{title}* ({str(created_at)[:10]})\n{content}\n\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=menu()); return

    # ── Premium ──
    if data == "buy_premium":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("1 месяц — 99 ₽",    callback_data="request_premium_1m")],
            [InlineKeyboardButton("3 месяца — 249 ₽",  callback_data="request_premium_3m")],
            [InlineKeyboardButton("6 месяцев — 399 ₽", callback_data="request_premium_6m")],
            [InlineKeyboardButton("1 год — 699 ₽",     callback_data="request_premium_12m")],
            [InlineKeyboardButton("🔙 Назад",          callback_data="back_menu")],
        ])
        await query.edit_message_text(
            PREMIUM_TEXT.format(card=ADMIN_CARD),
            parse_mode="Markdown", reply_markup=keyboard
        ); return

    if data.startswith("request_premium_"):
        plan = data.replace("request_premium_", "")
        plan_names = {"1m":"1 месяц","3m":"3 месяца","6m":"6 месяцев","12m":"1 год"}
        save_premium_request(user_id, username, plan)
        await query.edit_message_text(
            f"✅ Запрос на Premium ({plan_names.get(plan,'?')}) отправлен!\n\n"
            f"💳 Оплати по реквизитам:\n`{ADMIN_CARD}`\n\n"
            f"После оплаты нажми кнопку ниже.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Я оплатил", callback_data=f"paid_premium_{plan}")]])
        )
        # Уведомление админу
        try:
            admin_row = get_player(user_id)
            uname_display = f"@{username}" if username else f"ID:{user_id}"
            await context.bot.send_message(
                chat_id=(await context.bot.get_chat(f"@{ADMIN_USERNAME}")).id,
                text=f"💎 Новый запрос на Premium!\n\n👤 {uname_display}\n📦 Тариф: {plan_names.get(plan,'?')}"
            )
        except: pass
        return

    if data.startswith("paid_premium_"):
        plan = data.replace("paid_premium_", "")
        plan_names = {"1m":"1 месяц","3m":"3 месяца","6m":"6 месяцев","12m":"1 год"}
        await query.edit_message_text(
            f"⏳ Ожидай подтверждения оплаты от владельца.\nТариф: {plan_names.get(plan,'?')}",
            reply_markup=menu()
        )
        try:
            uname_display = f"@{username}" if username else f"ID:{user_id}"
            await context.bot.send_message(
                chat_id=(await context.bot.get_chat(f"@{ADMIN_USERNAME}")).id,
                text=f"💳 Пользователь {uname_display} сообщил об оплате Premium ({plan_names.get(plan,'?')})!\n\nID: {user_id}\n\nПроверь и подтверди в Админ-панели."
            )
        except: pass
        return

    # ── Просмотр профилей ──
    if data.startswith("browse_") or data.startswith("premium_browse_"):
        premium_only = data.startswith("premium_browse_")
        offset = int(data.split("_")[-1])
        row, total = get_all_profiles(offset, premium_only)
        if not row or total == 0:
            await query.edit_message_text("😔 Профилей пока нет.", reply_markup=menu()); return
        uid, score, cat, gender, wins, losses, matches, profile_file_id, is_prem = row
        icon    = "👨" if gender=="male" else "👩"
        p_icon  = " 💎" if is_prem else ""
        winrate = round(wins/matches*100) if matches > 0 else 0
        text    = (
            f"{icon}{p_icon} *Профиль #{offset+1} из {total}*\n\n"
            f"🎯 Категория: *{cat.upper()}*\n"
            f"📊 Балл: *{score:.0f}/100*\n"
            f"⚔️ {matches} матчей | {wins}W/{losses}L | {winrate}% WR"
        )
        prefix = "premium_browse" if premium_only else "browse"
        nav = []
        if offset > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_{offset-1}"))
        if offset < total-1: nav.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_{offset+1}"))
        keyboard = InlineKeyboardMarkup([nav, [InlineKeyboardButton("🔙 В меню", callback_data="back_menu")]])
        if profile_file_id:
            try:
                await query.message.delete()
                await context.bot.send_photo(chat_id=query.message.chat_id, photo=profile_file_id,
                                              caption=text, parse_mode="Markdown", reply_markup=keyboard)
                return
            except: pass
        await query.edit_message_text(text + "\n\n_Фото профиля не установлено_", parse_mode="Markdown", reply_markup=keyboard); return

    # ── ММ ──
    if data in ("mm_male","mm_female"):
        await start_matchmaking(query, user_id, user, context, "male" if data=="mm_male" else "female"); return

    if data == "cancel_queue":
        for q in matchmaking_queues.values():
            if user_id in q: q.remove(user_id)
        user_data_store.pop(user_id, None)
        await query.edit_message_text("❌ Поиск отменён.", reply_markup=menu()); return

    # ── Советы ──
    if data.startswith("tips_"):
        _, category, gender = data.split("|")
        await query.edit_message_text(get_tips_text(category, gender), parse_mode="Markdown", reply_markup=menu()); return

    # ── Оценка ──
    if data.startswith("gender_"):
        parts  = data.split("_")
        gender = parts[1]; mode = parts[2]
        stored = user_data_store.get(user_id, {})
        if not stored.get("file_id"):
            await query.edit_message_text("❌ Фото не найдено. Пришли фото снова."); return
        await query.edit_message_text("⏳ Анализирую лицо...")
        try:
            file        = await context.bot.get_file(stored["file_id"])
            file_bytes  = await file.download_as_bytearray()
            image_bytes = bytes(file_bytes)
            photo_hash  = get_photo_hash(image_bytes)
            if is_duplicate_photo(user_id, photo_hash):
                await query.edit_message_text("⚠️ Ты уже отправлял это фото!\nДля честного результата пришли новое.", reply_markup=menu()); return
            result = analyze_face(image_bytes, gender)
            if result.get("error"):
                await query.edit_message_text(f"❌ {result['error']}", reply_markup=menu()); return
            score      = result["score"]
            details    = result["details"]
            edit_level = result.get("edit_level", 0)
            is_screen  = result.get("is_screen", False)
            category, emoji, desc = get_category(score, gender)
            uname = user.first_name or username or "Аноним"
            save_player(user_id, uname, score, category, gender)
            save_history(user_id, score, category, photo_hash)
            screen_warn = "\n⚠️ *Похоже на фото с экрана!*\n" if is_screen else ""
            edit_text   = "🟢 Минимальная" if edit_level < 30 else ("🟡 Умеренная" if edit_level < 60 else "🔴 Сильная")
            if mode == "rate":
                bar  = "█" * int(score/5) + "░" * (20 - int(score/5))
                text = (
                    f"{'👨' if gender=='male' else '👩'} *Результат оценки*\n{screen_warn}\n"
                    f"🎯 Категория: *{category.upper()}* {emoji}\n\n"
                    f"📊 Балл: *{score}/100*\n`{bar}`\n\n_{desc}_\n\n"
                    f"📐 *Детали:*\n"
                    f"• Симметрия:       `{details.get('symmetry',0):.0f}/100`\n"
                    f"• Золотое сечение: `{details.get('golden_ratio',0):.0f}/100`\n"
                    f"• Расп. глаз:      `{details.get('eye_spacing',0):.0f}/100`\n"
                    f"• Уровень глаз:    `{details.get('eye_level',0):.0f}/100`\n"
                    f"• Чёткость фото:   `{details.get('face_clarity',0):.0f}/100`\n\n"
                    f"🖼 Обработка: {edit_text} ({edit_level:.0f}%)\n"
                )
                _left = get_analyses_left(user_id)
                _prem = is_premium_user(user_id)
                _btn  = f"🔬 Расширенный анализ ({'∞' if _prem else _left} ⚡)"
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💡 Советы по улучшению", callback_data=f"tips_|{category}|{gender}")],
                    [InlineKeyboardButton(_btn, callback_data=f"premium_analysis|{gender}")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_menu")],
                ])
                _stored = user_data_store.get(user_id, {})
                _stored["last_photo_bytes"] = image_bytes
                _stored["last_gender"]      = gender
                user_data_store[user_id]    = _stored
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
            elif mode == "match":
                await process_match_result(query, user_id, score, category, emoji, context)
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка: {str(e)[:150]}", reply_markup=menu())
        return

    # ── Админ-панель ──
    if data == "admin":
        if not is_admin(username):
            await query.edit_message_text("❌ Нет доступа."); return
        pending = get_pending_requests()
        pending_text = f"\n\n⏳ Ожидают подтверждения: {len(pending)}" if pending else ""
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Апдейт → Всем",     callback_data="pub_public")],
            [InlineKeyboardButton("💎 Апдейт → Premium",  callback_data="pub_premium")],
            [InlineKeyboardButton("🔒 Апдейт → Бета",     callback_data="pub_beta")],
            [InlineKeyboardButton("👑 Апдейт → Только мне",callback_data="pub_admin")],
            [InlineKeyboardButton("💎 Выдать Premium",     callback_data="give_premium")],
            [InlineKeyboardButton("❌ Забрать Premium",    callback_data="revoke_premium_admin")],
            [InlineKeyboardButton("🔒 Выдать Бета-доступ",callback_data="give_beta")],
            [InlineKeyboardButton(f"⏳ Заявки на Premium ({len(pending)})", callback_data="pending_premium")],
            [InlineKeyboardButton("🗑 Сбросить рейтинг",  callback_data="reset_ratings")],
            [InlineKeyboardButton("🔙 Назад",             callback_data="profile")],
        ])
        await query.edit_message_text(f"⚙️ *Админ-панель*{pending_text}", parse_mode="Markdown", reply_markup=keyboard); return

    if data == "pending_premium":
        if not is_admin(username): return
        pending = get_pending_requests()
        if not pending:
            await query.edit_message_text("✅ Заявок нет.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin")]])); return
        btns = []
        for req_id, req_user_id, req_uname, plan, created_at in pending:
            plan_names = {"1m":"1мес","3m":"3мес","6m":"6мес","12m":"1год"}
            label = f"@{req_uname or req_user_id} — {plan_names.get(plan,'?')}"
            btns.append([
                InlineKeyboardButton(f"✅ {label}", callback_data=f"approve_{req_id}"),
                InlineKeyboardButton(f"❌", callback_data=f"reject_{req_id}"),
            ])
        btns.append([InlineKeyboardButton("🔙 Назад", callback_data="admin")])
        await query.edit_message_text("⏳ *Заявки на Premium:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns)); return

    if data.startswith("approve_"):
        if not is_admin(username): return
        req_id = int(data.split("_")[1])
        approved_user_id, months = approve_request(req_id)
        if approved_user_id:
            try:
                await context.bot.send_message(chat_id=approved_user_id, text=f"🎉 Твой Premium подтверждён на {months} мес.! Спасибо за поддержку! 💎")
            except: pass
        await query.edit_message_text("✅ Premium выдан!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="pending_premium")]])); return

    if data.startswith("reject_"):
        if not is_admin(username): return
        req_id = int(data.split("_")[1])
        rejected_uid = reject_request(req_id)
        if rejected_uid:
            try:
                await context.bot.send_message(chat_id=rejected_uid, text="❌ Твоя заявка на Premium отклонена. Свяжись с @nerealnytalanty.")
            except: pass
        await query.edit_message_text("❌ Заявка отклонена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="pending_premium")]])); return

    if data == "reset_ratings":
        if not is_admin(username): return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Да, сбросить всё", callback_data="confirm_reset")],
            [InlineKeyboardButton("❌ Отмена", callback_data="admin")],
        ])
        await query.edit_message_text("⚠️ Ты уверен? Все рейтинги и история будут удалены!", reply_markup=keyboard); return

    if data == "confirm_reset":
        if not is_admin(username): return
        reset_all_ratings()
        await query.edit_message_text("✅ Рейтинг сброшен.", reply_markup=menu()); return

    if data in ("pub_public","pub_premium","pub_beta","pub_admin"):
        if not is_admin(username): return
        level_map = {"pub_public":"public","pub_premium":"premium","pub_beta":"beta","pub_admin":"admin"}
        level = level_map[data]
        user_data_store[user_id] = {"mode": "awaiting_update_title", "update_level": level}
        level_labels = {"public":"всем","premium":"Premium","beta":"бета-тестерам","admin":"только тебе"}
        await query.edit_message_text(f"📝 Введи *заголовок* апдейта для {level_labels[level]}:", parse_mode="Markdown"); return

    if data == "give_premium":
        if not is_admin(username): return
        user_data_store[user_id] = {"mode": "awaiting_give_premium"}
        await query.edit_message_text("Введи ID пользователя и количество месяцев через пробел:\nПример: `123456789 3`", parse_mode="Markdown"); return

    if data == "revoke_premium_admin":
        if not is_admin(username): return
        user_data_store[user_id] = {"mode": "awaiting_revoke_premium"}
        await query.edit_message_text("Введи ID пользователя у которого забрать Premium:"); return

    if data == "give_beta":
        if not is_admin(username): return
        user_data_store[user_id] = {"mode": "awaiting_give_beta"}
        await query.edit_message_text("Введи ID пользователя которому дать бета-доступ:"); return

    # Dating callbacks
    if data.startswith("dt_"):
        await handle_dating_callback(query, data, user_id, user, context)
        return

    # Реферальная ссылка
    if data == "referral_link":
        bot_info  = await context.bot.get_me()
        ref_link  = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
        try:
            conn = new_conn(); c = conn.cursor()
            c.execute("SELECT referral_count, analyses_left FROM players WHERE user_id=$1", [user_id])
            ref_row   = c.fetchone(); conn.close()
            ref_count = ref_row[0] if ref_row else 0
            left      = ref_row[1] if ref_row else 0
        except Exception:
            ref_count = 0; left = 0
        prem = is_premium_user(user_id)
        await query.edit_message_text(
            f"⚡ *Реферальная программа*\n\nПриглашай друзей → *+3 расширенных анализа* за каждого!\n\n🔗 Твоя ссылка:\n`{ref_link}`\n\n👥 Приглашено: {ref_count}\n⚡ Анализов: {'∞ (Premium)' if prem else left}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_menu")]])
        ); return

    # Расширенный анализ
    if data.startswith("premium_analysis"):
        parts  = data.split("|")
        gender = parts[1] if len(parts) > 1 else user_data_store.get(user_id, {}).get("last_gender", "male")
        img_b  = user_data_store.get(user_id, {}).get("last_photo_bytes")
        if not img_b:
            await query.edit_message_text("❌ Фото не найдено. Пройди оценку снова.", reply_markup=menu()); return
        if not spend_analysis(user_id):
            await query.edit_message_text(
                "❌ У тебя *0 очков* расширенного анализа.\n\nПригласи друга → *+3 анализа бесплатно!*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚡ Получить ссылку", callback_data="referral_link")],
                    [InlineKeyboardButton("💎 Купить Premium",  callback_data="buy_premium")],
                    [InlineKeyboardButton("🔙 Назад",           callback_data="back_menu")],
                ])
            ); return
        await query.edit_message_text("🔬 Запускаю расширенный анализ...")
        result = get_premium_analysis(img_b, gender)
        if result.get("error"):
            await query.edit_message_text(f"❌ {result['error']}", reply_markup=menu()); return
        left = get_analyses_left(user_id); prem = is_premium_user(user_id)
        text = (
            f"🔬 *Расширенный анализ*\n\n"
            f"🎯 Тир: *{result['tier']}* ({result['score']}/10)\n\n"
            f"📐 *Метрики:*\n"
            f"• fWHR: `{result['fwhr']}`\n"
            f"• Симметрия: `{result['symmetry']}%`\n"
            f"• Баланс третей: `{result['thirds_balance']}%`\n\n"
            f"👁 *Глаза:*\n"
            f"• Canthal Tilt: `{result['canthal_tilt']}`\n"
            f"• Угол: `{result['canthal_angle']}°`\n\n"
            f"🦷 *Челюсть:*\n"
            f"• Гониальный угол: `{result['gonial_angle']}°`\n"
            f"• Тип: `{result['gonial_class']}`\n\n"
            f"👤 *Профиль:* `{result['profile_type']}`\n\n"
            f"⚡ Осталось: {'∞' if prem else left}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=menu()); return

    await query.edit_message_text("Выбери действие:", reply_markup=menu())

# ──────────────────────────────────────────────
# ТЕКСТ
# ──────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.username or ""
    text     = update.message.text.strip()
    mode     = user_data_store.get(user_id, {}).get("mode", "")

    def menu(): return main_menu_keyboard(user_id, username)

    if mode == "awaiting_update_title":
        user_data_store[user_id]["title"] = text
        user_data_store[user_id]["mode"]  = "awaiting_update_content"
        await update.message.reply_text("Теперь введи *текст* апдейта:", parse_mode="Markdown"); return

    if mode == "awaiting_update_content":
        title = user_data_store[user_id].get("title","Обновление")
        level = user_data_store[user_id].get("update_level","public")
        save_update(title, text, level)
        user_data_store.pop(user_id, None)
        level_labels = {"public":"всем","premium":"Premium","beta":"бета-тестерам","admin":"только тебе"}
        await update.message.reply_text(f"✅ Апдейт опубликован для {level_labels.get(level,'?')}!", reply_markup=menu())
        level_icons = {"admin":"👑","beta":"🔒","premium":"💎","public":"📢"}
        msg = f"📢 {level_icons.get(level,'')} *Новый апдейт: {title}*\n\n{text}"
        if level == "public":
            targets = get_all_user_ids()
        elif level == "premium":
            targets = get_premium_user_ids()
        elif level == "beta":
            targets = get_beta_user_ids()
        else:
            targets = []
        for uid in targets:
            if uid == user_id: continue
            try: await context.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
            except: pass
        return

    if mode == "awaiting_give_premium":
        try:
            parts   = text.split()
            uid     = int(parts[0])
            months  = int(parts[1]) if len(parts) > 1 else 1
            until   = set_premium(uid, months)
            user_data_store.pop(user_id, None)
            await update.message.reply_text(f"✅ Premium выдан пользователю {uid} до {str(until)[:10]}!", reply_markup=menu())
            try: await context.bot.send_message(chat_id=uid, text=f"🎉 Тебе выдан Premium на {months} мес.! 💎")
            except: pass
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    if mode == "awaiting_revoke_premium":
        try:
            uid = int(text)
            revoke_premium(uid)
            user_data_store.pop(user_id, None)
            await update.message.reply_text(f"✅ Premium забран у пользователя {uid}.", reply_markup=menu())
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    if mode == "awaiting_give_beta":
        try:
            uid = int(text)
            set_beta(uid, True)
            user_data_store.pop(user_id, None)
            await update.message.reply_text(f"✅ Бета-доступ выдан пользователю {uid}.", reply_markup=menu())
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
        return

    await update.message.reply_text("Выбери действие:", reply_markup=menu())

# ──────────────────────────────────────────────
# МАТЧМЕЙКИНГ
# ──────────────────────────────────────────────

async def start_matchmaking(query, user_id, user, context, mm_gender):
    for q in matchmaking_queues.values():
        if user_id in q:
            await query.edit_message_text("⏳ Ты уже в очереди...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_queue")]])); return

    premium = is_premium_user(user_id)
    # Premium идут в приоритетную очередь
    queue_key = f"premium_{mm_gender}" if premium else mm_gender

    # Сначала ищем в своей очереди, потом в обычной (для premium)
    opponent_id = None
    search_queues = [queue_key]
    if premium:
        search_queues.append(mm_gender)  # premium может матчиться с обычными если нет premium

    for qk in search_queues:
        for uid in matchmaking_queues[qk]:
            if uid != user_id:
                opponent_id = uid
                matchmaking_queues[qk].remove(uid)
                break
        if opponent_id:
            break

    if opponent_id:
        opp_name = user_data_store.get(opponent_id, {}).get("first_name", "Аноним")
        match_store[user_id]     = {"opponent_id": opponent_id, "my_score": None, "opp_score": None, "my_cat": None, "opp_cat": None, "my_emoji": None, "opp_emoji": None, "opp_name": opp_name}
        match_store[opponent_id] = {"opponent_id": user_id,     "my_score": None, "opp_score": None, "my_cat": None, "opp_cat": None, "my_emoji": None, "opp_emoji": None, "opp_name": user.first_name or "Аноним"}
        user_data_store[user_id]     = {"mode": "match", "mm_gender": mm_gender}
        user_data_store[opponent_id] = {"mode": "match", "mm_gender": mm_gender}
        await query.edit_message_text(f"✅ Соперник найден!\n\n⚔️ Против: *{opp_name}*\n\n📸 Пришли своё фото!", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=opponent_id, text=f"✅ Соперник найден!\n\n⚔️ Против: *{user.first_name or 'Аноним'}*\n\n📸 Пришли своё фото!", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка уведомления: {e}")
    else:
        matchmaking_queues[queue_key].append(user_id)
        user_data_store[user_id] = {"mode": "match", "mm_gender": mm_gender, "first_name": user.first_name}
        label = "👨 мужчин" if mm_gender=="male" else "👩 женщин"
        prem_note = "\n💎 У тебя приоритет в очереди!" if premium else ""
        await query.edit_message_text(f"🔍 Ищем соперника в очереди {label}...{prem_note}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_queue")]]))


async def process_match_result(query, user_id, score, category, emoji, context):
    if user_id not in match_store:
        await query.edit_message_text("❌ Матч не найден.", reply_markup=main_menu_keyboard(user_id)); return
    match = match_store[user_id]; opponent_id = match["opponent_id"]
    match["my_score"] = score; match["my_cat"] = category; match["my_emoji"] = emoji
    if opponent_id in match_store:
        match_store[opponent_id]["opp_score"] = score
        match_store[opponent_id]["opp_cat"]   = category
        match_store[opponent_id]["opp_emoji"] = emoji
    opp_match = match_store.get(opponent_id, {})
    opp_score = opp_match.get("my_score")
    if opp_score is None:
        await query.edit_message_text("✅ Фото принято! Ждём фото соперника...")
        try: await context.bot.send_message(chat_id=opponent_id, text="⏳ Соперник уже прислал фото. Пришли своё!")
        except: pass
        return
    my_cat = category; opp_cat = opp_match.get("my_cat","?")
    opp_emoji2 = opp_match.get("my_emoji",""); opp_name = match.get("opp_name","Соперник")
    my_name = match_store[opponent_id].get("opp_name","Соперник")
    my_rank = CATEGORY_RANK.get(my_cat,0); opp_rank = CATEGORY_RANK.get(opp_cat,0)
    if my_rank > opp_rank:   winner_id,loser_id=user_id,opponent_id; my_result,opp_result="🏆 *ПОБЕДА!*","💀 *ПОРАЖЕНИЕ*"
    elif opp_rank > my_rank: winner_id,loser_id=opponent_id,user_id; my_result,opp_result="💀 *ПОРАЖЕНИЕ*","🏆 *ПОБЕДА!*"
    else:                    winner_id=loser_id=None; my_result=opp_result="🤝 *НИЧЬЯ*"
    if winner_id: update_match_result(winner_id, loser_id)
    my_text  = f"⚔️ *Результат матча*\n\n{my_result}\n\n👤 Ты: *{my_cat.upper()}* {emoji} ({score:.0f} б)\n👤 {opp_name}: *{opp_cat.upper()}* {opp_emoji2} ({opp_score:.0f} б)"
    opp_text = f"⚔️ *Результат матча*\n\n{opp_result}\n\n👤 Ты: *{opp_cat.upper()}* {opp_emoji2} ({opp_score:.0f} б)\n👤 {my_name}: *{my_cat.upper()}* {emoji} ({score:.0f} б)"
    await query.edit_message_text(my_text, parse_mode="Markdown", reply_markup=main_menu_keyboard(user_id))
    try: await context.bot.send_message(chat_id=opponent_id, text=opp_text, parse_mode="Markdown", reply_markup=main_menu_keyboard(opponent_id))
    except Exception as e: logger.error(f"Ошибка результата: {e}")
    for uid in [user_id, opponent_id]:
        match_store.pop(uid, None); user_data_store.pop(uid, None)

# ──────────────────────────────────────────────
# АВТОСБРОС РЕЙТИНГА (первый понедельник месяца)
# ──────────────────────────────────────────────

async def auto_reset_scheduler(bot):
    while True:
        now = datetime.now()
        # Первый понедельник месяца
        first_day = now.replace(day=1)
        days_until_monday = (7 - first_day.weekday()) % 7
        first_monday = first_day + timedelta(days=days_until_monday)
        first_monday = first_monday.replace(hour=3, minute=0, second=0, microsecond=0)
        if now >= first_monday:
            first_monday = (first_monday.replace(month=first_monday.month%12+1, day=1)
                            if first_monday.month < 12
                            else first_monday.replace(year=first_monday.year+1, month=1, day=1))
        wait_seconds = (first_monday - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        reset_all_ratings()
        logger.info("Автосброс рейтинга выполнен!")
        try:
            admin_chat = await bot.get_chat(f"@{ADMIN_USERNAME}")
            await bot.send_message(chat_id=admin_chat.id, text="🗑 Автосброс рейтинга выполнен (первый понедельник месяца).")
        except: pass

# ──────────────────────────────────────────────
# ВЕБ + САМОПИНГ
# ──────────────────────────────────────────────

async def health(request):
    return web.Response(text="OK")

async def run_web():
    app_web = web.Application()
    app_web.router.add_get("/", health)
    runner = web.AppRunner(app_web)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info(f"Веб-сервер запущен на порту {PORT}")

async def self_ping():
    if not RENDER_URL: return
    import aiohttp
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(RENDER_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    logger.info(f"Самопинг: {r.status}")
        except Exception as e:
            logger.warning(f"Самопинг не удался: {e}")
        await asyncio.sleep(20)

async def main():
    init_db()
    init_dating_db()
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    # Удаляем вебхук чтобы не было конфликта
    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    bot_app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, handle_video_note))
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    await run_web()
    asyncio.create_task(self_ping())
    async with bot_app:
        await bot_app.start()
        asyncio.create_task(auto_reset_scheduler(bot_app.bot))
        await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Бот запущен!")
        await asyncio.Event().wait()




# ══════════════════════════════════════════════════════════════════
# МОДУЛЬ 1: РАСШИРЕННЫЙ OPENCV АНАЛИЗ
# ══════════════════════════════════════════════════════════════════

def get_base_analysis(image_bytes: bytes, gender: str) -> dict:
    """Базовый анализ — бесплатный. fWHR + базовый тир."""
    try:
        import cv2
        nparr   = np.frombuffer(image_bytes, np.uint8)
        img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            return {"error": "Не удалось прочитать изображение."}
        img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        faces = face_cascade.detectMultiScale(img_gray, 1.1, 5, minSize=(80,80))
        if len(faces) == 0:
            return {"error": "Лицо не обнаружено. Пришли чёткое фото анфас."}
        fx, fy, fw, fh = max(faces, key=lambda f: f[2]*f[3])
        fwhr = round(fw / max(fh * 0.4, 1), 2)
        if fwhr >= 1.9:   tier, score = "Chad",     8.0
        elif fwhr >= 1.7: tier, score = "Chadlite", 7.0
        elif fwhr >= 1.5: tier, score = "HTN",      6.0
        elif fwhr >= 1.3: tier, score = "Normie",   5.0
        elif fwhr >= 1.1: tier, score = "Sub-5",    4.0
        else:             tier, score = "Sub-3",    2.5
        face_img   = img_gray[fy:fy+fh, fx:fx+fw]
        mid        = fw // 2
        left_half  = face_img[:, :mid]
        right_half = cv2.flip(face_img[:, mid:], 1)
        min_w      = min(left_half.shape[1], right_half.shape[1])
        diff       = cv2.absdiff(left_half[:,:min_w].astype(float), right_half[:,:min_w].astype(float))
        symmetry   = round(max(0, 100 - diff.mean() / 128 * 200), 1)
        return {"type": "base", "fwhr": fwhr, "tier": tier, "score": score, "symmetry": symmetry, "error": None}
    except Exception as e:
        return {"error": str(e)[:120]}


def get_premium_analysis(image_bytes: bytes, gender: str) -> dict:
    """Расширенный анализ — fWHR + Canthal Tilt + Gonial Angle + профиль."""
    try:
        import cv2
        nparr    = np.frombuffer(image_bytes, np.uint8)
        img_bgr  = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            return {"error": "Не удалось прочитать изображение."}
        img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        h, w     = img_bgr.shape[:2]
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        eye_cascade  = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
        faces = face_cascade.detectMultiScale(img_gray, 1.1, 5, minSize=(80,80))
        if len(faces) == 0:
            return {"error": "Лицо не обнаружено. Пришли чёткое фото анфас."}
        fx, fy, fw, fh = max(faces, key=lambda f: f[2]*f[3])
        face_img = img_gray[fy:fy+fh, fx:fx+fw]
        fwhr = round(fw / max(fh * 0.4, 1), 2)
        mid        = fw // 2
        left_half  = face_img[:, :mid]
        right_half = cv2.flip(face_img[:, mid:], 1)
        min_w      = min(left_half.shape[1], right_half.shape[1])
        diff       = cv2.absdiff(left_half[:,:min_w].astype(float), right_half[:,:min_w].astype(float))
        symmetry   = round(max(0, 100 - diff.mean() / 128 * 200), 1)
        third = fh / 3
        thirds_balance = round(100 - abs(third - (fh - 2*third)) / fh * 100, 1)
        face_top = img_gray[fy:fy+fh//2, fx:fx+fw]
        eyes = eye_cascade.detectMultiScale(face_top, 1.1, 3, minSize=(20,20))
        canthal_tilt  = "Neutral ➡️"
        canthal_angle = 0.0
        if len(eyes) >= 2:
            eyes = sorted(eyes, key=lambda e: e[0])
            ex1,ey1,ew1,eh1 = eyes[0]; ex2,ey2,ew2,eh2 = eyes[1]
            dx = (ex2+ew2//2) - (ex1+ew1//2)
            dy = (ey2+eh2//2) - (ey1+eh1//2)
            canthal_angle = round(np.degrees(np.arctan2(dy, max(dx,1))), 2)
            if canthal_angle > 2:   canthal_tilt = "Negative ❌"
            elif canthal_angle < -2: canthal_tilt = "Positive ✅"
        lower_face = img_gray[fy+fh*2//3:fy+fh, fx:fx+fw]
        edges    = cv2.Canny(lower_face, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        gonial_angle = 125.0
        if contours:
            largest = max(contours, key=cv2.contourArea)
            if len(largest) >= 5:
                rect  = cv2.minAreaRect(largest)
                angle = abs(rect[2])
                if angle > 45: angle = 90 - angle
                gonial_angle = round(90 + angle * 0.8, 1)
        if gonial_angle < 115:   gonial_class = "Low Gonial ✅ (Chad-черта)"
        elif gonial_angle < 125: gonial_class = "Average Gonial ➡️"
        else:                    gonial_class = "High Gonial ❌ (рецессивная)"
        if fwhr > 1.7 and thirds_balance > 70: profile_type = "Straight ✅"
        elif fwhr < 1.3:                        profile_type = "Convex ❌"
        else:                                   profile_type = "Neutral ➡️"
        score = 5.0
        if fwhr >= 1.9:   score += 1.5
        elif fwhr >= 1.7: score += 1.0
        elif fwhr >= 1.5: score += 0.5
        elif fwhr < 1.2:  score -= 1.0
        if "Positive" in canthal_tilt: score += 1.0
        elif "Negative" in canthal_tilt: score -= 0.5
        if "Low" in gonial_class: score += 0.5
        elif "High" in gonial_class: score -= 0.5
        if symmetry > 80: score += 0.5
        elif symmetry < 50: score -= 0.5
        if "Straight" in profile_type: score += 0.3
        elif "Convex" in profile_type: score -= 0.3
        score = round(max(1.0, min(10.0, score)), 1)
        if score >= 8.0:   tier = "Chad 👑"
        elif score >= 7.0: tier = "Chadlite 🔥"
        elif score >= 6.0: tier = "HTN 😎"
        elif score >= 5.0: tier = "Normie 🙂"
        elif score >= 4.0: tier = "Sub-5 😐"
        else:              tier = "Sub-3 😔"
        return {
            "type": "premium", "fwhr": fwhr, "symmetry": symmetry,
            "thirds_balance": thirds_balance, "canthal_tilt": canthal_tilt,
            "canthal_angle": canthal_angle, "gonial_angle": gonial_angle,
            "gonial_class": gonial_class, "profile_type": profile_type,
            "score": score, "tier": tier, "error": None
        }
    except Exception as e:
        logger.error(f"Premium analysis error: {e}", exc_info=True)
        return {"error": str(e)[:120]}


# ══════════════════════════════════════════════════════════════════
# МОДУЛЬ 2: РЕФЕРАЛЬНАЯ СИСТЕМА
# ══════════════════════════════════════════════════════════════════

def get_analyses_left(user_id):
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("SELECT analyses_left FROM players WHERE user_id=$1", [user_id])
        row = c.fetchone(); conn.close()
        return row[0] if row and row[0] else 0
    except Exception: return 0

def add_analyses(user_id, count):
    conn = new_conn(); c = conn.cursor()
    c.execute("UPDATE players SET analyses_left=COALESCE(analyses_left,0)+$1 WHERE user_id=$2", [count, user_id])
    conn.commit(); conn.close()

def spend_analysis(user_id):
    """Списывает 1 анализ. Premium — безлимит. Возвращает True если можно."""
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("SELECT analyses_left, is_premium FROM players WHERE user_id=$1", [user_id])
        row = c.fetchone()
        if not row: conn.close(); return False
        left, is_prem = row[0] or 0, row[1] or False
        if is_prem: conn.close(); return True
        if left <= 0: conn.close(); return False
        c.execute("UPDATE players SET analyses_left=analyses_left-1 WHERE user_id=$1", [user_id])
        conn.commit(); conn.close(); return True
    except Exception: return False

def register_referral(new_user_id, referrer_id):
    """Начисляет +3 анализа рефереру."""
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("SELECT referred_by FROM players WHERE user_id=$1", [new_user_id])
        row = c.fetchone()
        if row and row[0]: conn.close(); return False
        c.execute("UPDATE players SET referred_by=$1 WHERE user_id=$2", [referrer_id, new_user_id])
        c.execute("UPDATE players SET analyses_left=COALESCE(analyses_left,0)+3, referral_count=COALESCE(referral_count,0)+1 WHERE user_id=$1", [referrer_id])
        conn.commit(); conn.close(); return True
    except Exception: return False

def is_new_user(user_id):
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("SELECT user_id FROM players WHERE user_id=$1", [user_id])
        row = c.fetchone(); conn.close()
        return row is None
    except Exception: return False


# ══════════════════════════════════════════════════════════════════
# МОДУЛЬ 3: DATING СИСТЕМА
# ══════════════════════════════════════════════════════════════════

def init_dating_db():
    tables = [
        """CREATE TABLE IF NOT EXISTS dating_profiles (
            user_id BIGINT PRIMARY KEY, name TEXT, age INTEGER,
            city TEXT, bio TEXT, photo_fid TEXT, is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS dating_reactions (
            id SERIAL PRIMARY KEY, from_id BIGINT, to_id BIGINT,
            reaction TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(from_id,to_id))""",
        """CREATE TABLE IF NOT EXISTS dating_matches (
            id SERIAL PRIMARY KEY, user1_id BIGINT, user2_id BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user1_id,user2_id))""",
    ]
    for sql in tables:
        try:
            conn = new_conn(); c = conn.cursor()
            c.execute(sql); conn.commit(); conn.close()
        except Exception as e:
            logger.warning(f"Dating table: {e}")

    cols = ["is_dating BOOLEAN DEFAULT FALSE",
            "analyses_left INTEGER DEFAULT 0",
            "referred_by BIGINT DEFAULT NULL",
            "referral_count INTEGER DEFAULT 0"]
    for col in cols:
        try:
            conn = new_conn(); c = conn.cursor()
            c.execute(f"ALTER TABLE players ADD COLUMN {col}")
            conn.commit(); conn.close()
        except Exception:
            pass

def get_dating_profile(user_id):
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("SELECT user_id,name,age,city,bio,photo_fid,is_active FROM dating_profiles WHERE user_id=$1", [user_id])
        row = c.fetchone(); conn.close(); return row
    except Exception: return None

def save_dating_profile(user_id, name, age, city, bio, photo_fid):
    conn = new_conn(); c = conn.cursor()
    c.execute("""INSERT INTO dating_profiles (user_id,name,age,city,bio,photo_fid)
        VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT (user_id) DO UPDATE SET
        name=EXCLUDED.name, age=EXCLUDED.age, city=EXCLUDED.city,
        bio=EXCLUDED.bio, photo_fid=EXCLUDED.photo_fid, is_active=TRUE""",
        [user_id, name, age, city, bio, photo_fid])
    conn.commit(); conn.close()

def toggle_dating(user_id):
    conn = new_conn(); c = conn.cursor()
    c.execute("UPDATE players SET is_dating=NOT COALESCE(is_dating,FALSE) WHERE user_id=$1", [user_id])
    conn.commit()
    c.execute("SELECT is_dating FROM players WHERE user_id=$1", [user_id])
    val = c.fetchone(); conn.close()
    return val[0] if val else False

def add_reaction(from_id, to_id, reaction):
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("INSERT INTO dating_reactions (from_id,to_id,reaction) VALUES ($1,$2,$3) ON CONFLICT (from_id,to_id) DO UPDATE SET reaction=EXCLUDED.reaction", [from_id, to_id, reaction])
        conn.commit(); conn.close()
    except Exception: pass

def check_match(user1_id, user2_id):
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM dating_reactions WHERE from_id=$1 AND to_id=$2 AND reaction IN ('like','card')", [user2_id, user1_id])
        count = c.fetchone()[0]; conn.close(); return count > 0
    except Exception: return False

def save_match(user1_id, user2_id):
    try:
        conn = new_conn(); c = conn.cursor()
        u1,u2 = min(user1_id,user2_id), max(user1_id,user2_id)
        c.execute("INSERT INTO dating_matches (user1_id,user2_id) VALUES ($1,$2) ON CONFLICT DO NOTHING", [u1,u2])
        conn.commit(); conn.close()
    except Exception: pass

def find_next_profile(user_id):
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("SELECT age,city FROM dating_profiles WHERE user_id=$1", [user_id])
        me = c.fetchone()
        if not me: conn.close(); return None
        my_age, my_city = me
        c.execute("SELECT to_id FROM dating_reactions WHERE from_id=$1", [user_id])
        seen = [r[0] for r in c.fetchall()] + [user_id]
        seen_str = ','.join(str(x) for x in seen)
        queries = [
            (f"SELECT dp.user_id FROM dating_profiles dp JOIN players p ON p.user_id=dp.user_id WHERE dp.is_active=TRUE AND p.is_dating=TRUE AND dp.user_id NOT IN ({seen_str}) AND LOWER(dp.city)=LOWER($1) AND ABS(dp.age-$2)<=2 ORDER BY RANDOM() LIMIT 1", [my_city, my_age]),
            (f"SELECT dp.user_id FROM dating_profiles dp JOIN players p ON p.user_id=dp.user_id WHERE dp.is_active=TRUE AND p.is_dating=TRUE AND dp.user_id NOT IN ({seen_str}) AND LOWER(dp.city)=LOWER($1) ORDER BY RANDOM() LIMIT 1", [my_city]),
            (f"SELECT dp.user_id FROM dating_profiles dp JOIN players p ON p.user_id=dp.user_id WHERE dp.is_active=TRUE AND p.is_dating=TRUE AND dp.user_id NOT IN ({seen_str}) AND ABS(dp.age-$1)<=2 ORDER BY RANDOM() LIMIT 1", [my_age]),
            (f"SELECT dp.user_id FROM dating_profiles dp JOIN players p ON p.user_id=dp.user_id WHERE dp.is_active=TRUE AND p.is_dating=TRUE AND dp.user_id NOT IN ({seen_str}) ORDER BY RANDOM() LIMIT 1", []),
        ]
        result = None
        for q, params in queries:
            try:
                c.execute(q, params); row = c.fetchone()
                if row: result = row[0]; break
            except Exception: pass
        conn.close(); return result
    except Exception: return None

def get_full_dating_card(target_id):
    try:
        conn = new_conn(); c = conn.cursor()
        c.execute("""SELECT dp.name,dp.age,dp.city,dp.bio,dp.photo_fid,p.category,p.username
            FROM dating_profiles dp LEFT JOIN players p ON p.user_id=dp.user_id WHERE dp.user_id=$1""", [target_id])
        row = c.fetchone(); conn.close(); return row
    except Exception: return None

def dating_card_keyboard(target_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Дизлайк", callback_data=f"dt_dislike_{target_id}"),
         InlineKeyboardButton("❤️ Лайк",    callback_data=f"dt_like_{target_id}")],
        [InlineKeyboardButton("💌 Открытка", callback_data=f"dt_card_{target_id}"),
         InlineKeyboardButton("🏠 Меню",     callback_data="back_menu")],
    ])

def dating_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👀 Смотреть анкеты",    callback_data="dt_browse")],
        [InlineKeyboardButton("📝 Моя анкета",         callback_data="dt_my_profile")],
        [InlineKeyboardButton("✏️ Редактировать",      callback_data="dt_edit")],
        [InlineKeyboardButton("🔴 Выйти из знакомств", callback_data="dt_toggle_off")],
        [InlineKeyboardButton("🏠 Главное меню",       callback_data="back_menu")],
    ])

DT_EXIT_KB = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Выйти в меню", callback_data="dt_exit_fsm")]])

async def dating_show_card(user_id, chat_id, context):
    target_id = find_next_profile(user_id)
    if not target_id:
        await context.bot.send_message(chat_id=chat_id, text="😔 Анкеты закончились. Попробуй позже!", reply_markup=dating_menu_keyboard())
        return
    card = get_full_dating_card(target_id)
    if not card: return
    name, age, city, bio, photo_fid, category, username = card
    rating_text = f" | 📊 {category.upper()}" if category else ""
    caption = f"👤 *{name}*, {age}, {city}{rating_text}\n\n_{bio}_"
    try:
        await context.bot.send_photo(chat_id=chat_id, photo=photo_fid, caption=caption, parse_mode="Markdown", reply_markup=dating_card_keyboard(target_id))
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown", reply_markup=dating_card_keyboard(target_id))

async def handle_dating_callback(query, data, user_id, user, context):
    username = user.username or ""
    if data == "dt_toggle":
        row = get_player(user_id)
        if not row:
            await query.edit_message_text("❌ Сначала пройди оценку внешности!"); return
        is_on = toggle_dating(user_id)
        if is_on:
            profile = get_dating_profile(user_id)
            if not profile:
                user_data_store[user_id] = {"mode": "dating_fsm", "step": "name"}
                await query.edit_message_text("💘 *Добро пожаловать в знакомства!*\n\nШаг 1/5 — Введи имя или никнейм:", parse_mode="Markdown", reply_markup=DT_EXIT_KB)
            else:
                await query.edit_message_text("✅ Знакомства включены!", reply_markup=dating_menu_keyboard())
        else:
            await query.edit_message_text("🔴 Знакомства выключены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_menu")]]))
        return
    if data == "dt_toggle_off":
        toggle_dating(user_id)
        await query.edit_message_text("🔴 Знакомства выключены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_menu")]])); return
    if data == "dt_exit_fsm":
        user_data_store.pop(user_id, None)
        await query.edit_message_text("❌ Выход.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="back_menu")]])); return
    if data == "dt_browse":
        await query.edit_message_text("👀 Ищу анкеты...")
        await dating_show_card(user_id, query.message.chat_id, context); return
    if data == "dt_my_profile":
        profile = get_dating_profile(user_id)
        if not profile:
            await query.edit_message_text("📝 Анкеты нет.", reply_markup=dating_menu_keyboard()); return
        _, name, age, city, bio, photo_fid, _ = profile
        text = f"👤 *{name}*, {age}, {city}\n\n_{bio}_"
        try:
            await query.message.delete()
            await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo_fid, caption=text, parse_mode="Markdown", reply_markup=dating_menu_keyboard())
        except Exception:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=dating_menu_keyboard())
        return
    if data == "dt_edit":
        user_data_store[user_id] = {"mode": "dating_fsm", "step": "name"}
        await query.edit_message_text("✏️ Редактируем анкету.\n\nШаг 1/5 — Имя или никнейм:", reply_markup=DT_EXIT_KB); return
    if data.startswith("dt_dislike_"):
        target_id = int(data.split("_")[2])
        add_reaction(user_id, target_id, "dislike")
        await query.edit_message_text("⏭ Пропущено.")
        await dating_show_card(user_id, query.message.chat_id, context); return
    if data.startswith("dt_like_"):
        target_id = int(data.split("_")[2])
        add_reaction(user_id, target_id, "like")
        if check_match(user_id, target_id):
            save_match(user_id, target_id)
            tc = get_full_dating_card(target_id)
            tuname = f"@{tc[6]}" if tc and tc[6] else "без username"
            tname  = tc[0] if tc else "Аноним"
            await query.edit_message_text(f"🎉 *Мэтч!*\n\n👤 {tname} — {tuname}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👀 Дальше", callback_data="dt_browse")]]))
            try: await context.bot.send_message(chat_id=target_id, text=f"🎉 *Мэтч!* Тебя лайкнули!\n\n👤 @{username}", parse_mode="Markdown")
            except Exception: pass
        else:
            await query.edit_message_text("❤️ Лайк отправлен!")
            await dating_show_card(user_id, query.message.chat_id, context)
        return
    if data.startswith("dt_card_"):
        target_id = int(data.split("_")[2])
        user_data_store[user_id] = {"mode": "dating_card_fsm", "target_id": target_id}
        await query.edit_message_text("💌 *Открытка*\n\nОтправь текст или видео-кружок — улетит получателю!", parse_mode="Markdown", reply_markup=DT_EXIT_KB); return
    if data.startswith("dt_reply_"):
        target_id = int(data.split("_")[2])
        add_reaction(user_id, target_id, "like")
        if check_match(user_id, target_id):
            save_match(user_id, target_id)
            tc = get_full_dating_card(target_id)
            tuname = f"@{tc[6]}" if tc and tc[6] else "без username"
            await query.edit_message_text(f"🎉 *Мэтч!*\n\n👤 {tuname}", parse_mode="Markdown")
            try: await context.bot.send_message(chat_id=target_id, text="🎉 Тебе ответили взаимностью!")
            except Exception: pass
        else:
            await query.edit_message_text("❤️ Ответ отправлен!")
        return

async def handle_dating_text(user_id, username, text, update, context):
    stored = user_data_store.get(user_id, {})
    mode   = stored.get("mode")
    if mode == "dating_fsm":
        step = stored.get("step")
        if step == "name":
            if len(text.strip()) < 2:
                await update.message.reply_text("❌ Имя слишком короткое:", reply_markup=DT_EXIT_KB); return True
            user_data_store[user_id]["name"] = text.strip()
            user_data_store[user_id]["step"] = "age"
            await update.message.reply_text("Шаг 2/5 — Сколько тебе лет?", reply_markup=DT_EXIT_KB); return True
        if step == "age":
            try:
                age = int(text.strip())
                if age < 13 or age > 100: raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Возраст от 13 до 100:", reply_markup=DT_EXIT_KB); return True
            user_data_store[user_id]["age"]  = age
            user_data_store[user_id]["step"] = "city"
            await update.message.reply_text("Шаг 3/5 — Из какого ты города?", reply_markup=DT_EXIT_KB); return True
        if step == "city":
            user_data_store[user_id]["city"] = text.strip()
            user_data_store[user_id]["step"] = "bio"
            await update.message.reply_text("Шаг 4/5 — Расскажи о себе:", reply_markup=DT_EXIT_KB); return True
        if step == "bio":
            user_data_store[user_id]["bio"]  = text.strip()
            user_data_store[user_id]["step"] = "photo"
            await update.message.reply_text("Шаг 5/5 — Пришли своё фото:", reply_markup=DT_EXIT_KB); return True
        return False
    if mode == "dating_card_fsm":
        target_id  = stored.get("target_id")
        my_profile = get_dating_profile(user_id)
        my_name    = my_profile[1] if my_profile else "Аноним"
        my_card    = get_full_dating_card(user_id)
        my_rating  = my_card[5].upper() if my_card and my_card[5] else ""
        add_reaction(user_id, target_id, "card")
        notify = f"💌 Открытка от *{my_name}*{f' [{my_rating}]' if my_rating else ''}!\n\n{text}"
        reply_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❤️ Ответить взаимностью", callback_data=f"dt_reply_{user_id}")]])
        try: await context.bot.send_message(chat_id=target_id, text=notify, parse_mode="Markdown", reply_markup=reply_kb)
        except Exception: pass
        user_data_store.pop(user_id, None)
        await update.message.reply_text("💌 Открытка отправлена!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👀 Дальше", callback_data="dt_browse")]])); return True
    return False

async def handle_dating_photo(user_id, photo_fid, update, context):
    stored = user_data_store.get(user_id, {})
    if stored.get("mode") != "dating_fsm" or stored.get("step") != "photo": return False
    name = stored.get("name"); age = stored.get("age")
    city = stored.get("city"); bio = stored.get("bio")
    if not all([name, age, city, bio]):
        await update.message.reply_text("❌ Что-то пошло не так. Начни заново.")
        user_data_store.pop(user_id, None); return True
    save_dating_profile(user_id, name, age, city, bio, photo_fid)
    conn = new_conn(); c = conn.cursor()
    c.execute("UPDATE players SET is_dating=TRUE WHERE user_id=$1", [user_id])
    conn.commit(); conn.close()
    user_data_store.pop(user_id, None)
    await update.message.reply_text(f"✅ *Анкета создана!*\n\n👤 *{name}*, {age}, {city}\n_{bio}_\n\nТы в системе знакомств! 💘", parse_mode="Markdown", reply_markup=dating_menu_keyboard())
    return True

async def handle_dating_video(user_id, file_id, update, context):
    stored = user_data_store.get(user_id, {})
    if stored.get("mode") != "dating_card_fsm": return False
    target_id  = stored.get("target_id")
    my_profile = get_dating_profile(user_id)
    my_name    = my_profile[1] if my_profile else "Аноним"
    my_card    = get_full_dating_card(user_id)
    my_rating  = my_card[5].upper() if my_card and my_card[5] else ""
    add_reaction(user_id, target_id, "card")
    notify = f"💌 Видео-открытка от *{my_name}*{f' [{my_rating}]' if my_rating else ''}!"
    reply_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❤️ Ответить взаимностью", callback_data=f"dt_reply_{user_id}")]])
    try:
        await context.bot.send_message(chat_id=target_id, text=notify, parse_mode="Markdown")
        await context.bot.send_video_note(chat_id=target_id, video_note=file_id, reply_markup=reply_kb)
    except Exception:
        try: await context.bot.send_video(chat_id=target_id, video=file_id, caption=notify, parse_mode="Markdown", reply_markup=reply_kb)
        except Exception: pass
    user_data_store.pop(user_id, None)
    await update.message.reply_text("💌 Видео-открытка отправлена!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👀 Дальше", callback_data="dt_browse")]])); return True



async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает видео и кружки для открыток Dating."""
    user_id = update.effective_user.id
    file_id = None
    if update.message.video_note:
        file_id = update.message.video_note.file_id
    elif update.message.video:
        file_id = update.message.video.file_id
    if file_id:
        if await handle_dating_video(user_id, file_id, update, context):
            return
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_keyboard(user_id, update.effective_user.username))


if __name__ == "__main__":
    asyncio.run(main())
