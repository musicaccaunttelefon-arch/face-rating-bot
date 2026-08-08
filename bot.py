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
    ]
    if not premium:
        buttons.append([InlineKeyboardButton("💎 Купить Premium", callback_data="buy_premium")])
    return InlineKeyboardMarkup(buttons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.username
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
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💡 Советы по улучшению", callback_data=f"tips_|{category}|{gender}")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_menu")],
                ])
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
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    # Удаляем вебхук чтобы не было конфликта
    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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

if __name__ == "__main__":
    asyncio.run(main())
