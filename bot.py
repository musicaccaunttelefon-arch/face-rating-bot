import logging
import os
import numpy as np
import sqlite3
import random
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
RENDER_URL     = os.environ.get("RENDER_URL", "")
PORT           = int(os.environ.get("PORT", 8080))

user_data_store    = {}
matchmaking_queues = {"male": [], "female": []}
match_store        = {}

# ──────────────────────────────────────────────
# БД
# ──────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            score      REAL DEFAULT 0,
            category   TEXT DEFAULT '',
            gender     TEXT DEFAULT '',
            wins       INTEGER DEFAULT 0,
            losses     INTEGER DEFAULT 0,
            matches    INTEGER DEFAULT 0,
            in_leaderboard INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            score      REAL,
            category   TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Добавляем колонку если её нет (для старых БД)
    try:
        c.execute("ALTER TABLE players ADD COLUMN in_leaderboard INTEGER DEFAULT 1")
    except:
        pass
    conn.commit()
    conn.close()

def save_player(user_id, username, score, category, gender):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO players (user_id, username, score, category, gender, in_leaderboard)
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, score=excluded.score,
            category=excluded.category, gender=excluded.gender
    """, (user_id, username, score, category, gender))
    c.execute("INSERT INTO history (user_id, score, category) VALUES (?, ?, ?)",
              (user_id, score, category))
    conn.commit()
    conn.close()

def toggle_leaderboard(user_id):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("UPDATE players SET in_leaderboard = 1 - in_leaderboard WHERE user_id=?", (user_id,))
    conn.commit()
    c.execute("SELECT in_leaderboard FROM players WHERE user_id=?", (user_id,))
    val = c.fetchone()
    conn.close()
    return val[0] if val else 1

def update_match_result(winner_id, loser_id):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("UPDATE players SET wins=wins+1, matches=matches+1 WHERE user_id=?", (winner_id,))
    c.execute("UPDATE players SET losses=losses+1, matches=matches+1 WHERE user_id=?", (loser_id,))
    conn.commit()
    conn.close()

def get_leaderboard(gender=None):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    if gender:
        c.execute("""SELECT username,score,category,wins,losses FROM players
                     WHERE gender=? AND in_leaderboard=1 ORDER BY score DESC LIMIT 10""", (gender,))
    else:
        c.execute("""SELECT username,score,category,wins,losses FROM players
                     WHERE in_leaderboard=1 ORDER BY score DESC LIMIT 10""")
    rows = c.fetchall()
    conn.close()
    return rows

def get_player(user_id):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("""SELECT username,score,category,wins,losses,matches,gender,in_leaderboard
                 FROM players WHERE user_id=?""", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_history(user_id):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("""SELECT score, category, created_at FROM history
                 WHERE user_id=? ORDER BY created_at DESC LIMIT 10""", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

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
        "sub 3": [
            ("Масса тела", "Нормализовать массу тела.", "🔴 Высокий"),
            ("Кожа", "Решить проблемы с кожей у дерматолога.", "🔴 Высокий"),
            ("Осанка", "Исправить осанку.", "🔴 Высокий"),
            ("Волосы", "Подобрать подходящую стрижку.", "🟡 Средний"),
            ("Брови", "Следить за бровями.", "🟡 Средний"),
            ("Здоровье", "Нормализовать сон, питание, физическую активность.", "🔴 Высокий"),
            ("Стиль", "При выраженных особенностях — консультация ортодонта.", "🟡 Средний"),
        ],
        "sub 5": [
            ("Масса тела", "Снизить процент жира при его избытке.", "🔴 Высокий"),
            ("Физ. форма", "Набрать мышечную массу.", "🔴 Высокий"),
            ("Кожа", "Регулярный уход за кожей.", "🟡 Средний"),
            ("Волосы", "Экспериментировать со стрижкой.", "🟡 Средний"),
            ("Борода", "Если растёт густая борода — использовать для коррекции лица.", "🟢 Низкий"),
            ("Зубы", "Отбеливание зубов при необходимости.", "🟢 Низкий"),
        ],
        "ltn": [
            ("Волосы", "Улучшить причёску.", "🔴 Высокий"),
            ("Физ. форма", "Развивать шею и трапеции.", "🟡 Средний"),
            ("Кожа", "Уход за кожей.", "🟡 Средний"),
            ("Осанка", "Исправить осанку.", "🟡 Средний"),
            ("Стиль", "Подобрать стиль одежды.", "🟡 Средний"),
            ("Фото", "Улучшить качество фотографий.", "🟢 Низкий"),
        ],
        "mtn": [
            ("Масса тела", "Поддерживать низкий процент жира.", "🟡 Средний"),
            ("Кожа", "Следить за кожей.", "🟡 Средний"),
            ("Физ. форма", "Развивать спортивную форму.", "🟡 Средний"),
            ("Волосы", "Экспериментировать с причёской.", "🟢 Низкий"),
            ("Стиль", "При желании — линзы вместо очков.", "🟢 Низкий"),
            ("Харизма", "Работать над улыбкой.", "🟢 Низкий"),
        ],
        "htn": [
            ("Физ. форма", "Поддерживать форму.", "🟡 Средний"),
            ("Кожа", "Следить за кожей.", "🟢 Низкий"),
            ("Волосы", "Регулярно стричься.", "🟢 Низкий"),
            ("Стиль", "Подбирать одежду по фигуре.", "🟢 Низкий"),
            ("Харизма", "Работать над уверенностью.", "🟢 Низкий"),
        ],
        "chad": [
            ("Физ. форма", "Просто поддерживать текущую форму.", "🟢 Низкий"),
            ("Кожа", "Следить за здоровьем кожи.", "🟢 Низкий"),
            ("Волосы", "Не экспериментировать с неудачными стрижками.", "🟢 Низкий"),
        ],
        "true adam": [
            ("Здоровье", "Поддерживать здоровье.", "🟢 Низкий"),
            ("Физ. форма", "Сохранять физическую форму.", "🟢 Низкий"),
            ("Стиль", "Не терять индивидуальный стиль.", "🟢 Низкий"),
        ],
    },
    "female": {
        "sub 3": [
            ("Кожа", "Консультация дерматолога при проблемной коже.", "🔴 Высокий"),
            ("Волосы", "Подобрать причёску под форму лица.", "🔴 Высокий"),
            ("Уход", "Освоить базовый уход за кожей.", "🔴 Высокий"),
            ("Зубы", "При необходимости — консультация ортодонта.", "🟡 Средний"),
            ("Стиль", "Подобрать подходящую оправу очков или линзы.", "🟡 Средний"),
        ],
        "sub 5": [
            ("Волосы", "Улучшить уход за волосами.", "🔴 Высокий"),
            ("Кожа", "Следить за состоянием кожи.", "🔴 Высокий"),
            ("Макияж", "Лёгкий естественный макияж.", "🟡 Средний"),
            ("Брови", "Подобрать форму бровей.", "🟡 Средний"),
            ("Осанка", "Работать над осанкой.", "🟡 Средний"),
        ],
        "ltb": [
            ("Волосы", "Найти подходящую стрижку.", "🔴 Высокий"),
            ("Волосы", "Следить за качеством волос.", "🟡 Средний"),
            ("Кожа", "Использовать уходовую косметику.", "🟡 Средний"),
            ("Стиль", "Подобрать стиль одежды.", "🟡 Средний"),
        ],
        "mtb": [
            ("Кожа", "Регулярный уход за кожей.", "🟡 Средний"),
            ("Макияж", "Аккуратный макияж по желанию.", "🟢 Низкий"),
            ("Физ. форма", "Поддерживать физическую форму.", "🟡 Средний"),
            ("Стиль", "Экспериментировать с образом.", "🟢 Низкий"),
        ],
        "htb": [
            ("Уход", "Поддерживать текущий уход.", "🟢 Низкий"),
            ("Волосы", "Следить за здоровьем волос.", "🟢 Низкий"),
            ("Здоровье", "Хороший сон.", "🟢 Низкий"),
            ("Кожа", "Защита кожи от солнца.", "🟢 Низкий"),
            ("Фото", "Подбирать качественные фотографии.", "🟢 Низкий"),
        ],
        "stacy": [
            ("Физ. форма", "Поддерживать форму.", "🟢 Низкий"),
            ("Кожа", "Беречь кожу.", "🟢 Низкий"),
            ("Волосы", "Регулярный уход за волосами.", "🟢 Низкий"),
            ("Косметика", "Не злоупотреблять косметическими процедурами.", "🟢 Низкий"),
        ],
        "true eve": [
            ("Здоровье", "Поддерживать здоровье.", "🟢 Низкий"),
            ("Кожа", "Регулярный уход за кожей и волосами.", "🟢 Низкий"),
            ("Стиль", "Сохранять естественный внешний вид.", "🟢 Низкий"),
            ("Физ. форма", "Следить за физической формой.", "🟢 Низкий"),
        ],
    }
}

def get_tips_text(category, gender):
    tips = TIPS.get(gender, {}).get(category, [])
    if not tips:
        return "Советы не найдены."
    text = f"💡 *Советы для {category.upper()}:*\n\n"
    for area, tip, priority in tips:
        text += f"*{area}* — {priority}\n_{tip}_\n\n"
    return text

# ──────────────────────────────────────────────
# АНАЛИЗ ЛИЦА
# ──────────────────────────────────────────────

def analyze_face(image_bytes: bytes, gender: str) -> dict:
    try:
        import cv2
        nparr   = np.frombuffer(image_bytes, np.uint8)
        img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img_bgr is None:
            return {"error": "Не удалось прочитать изображение."}
        img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        h, w = img_bgr.shape[:2]

        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        eye_cascade  = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')
        faces = face_cascade.detectMultiScale(img_gray, 1.1, 5, minSize=(80,80))
        if len(faces) == 0:
            return {"error": "Лицо не обнаружено. Пришли чёткое фото анфас."}

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
            eye_dist  = abs((ex2+ew2//2)-(ex1+ew1//2))
            avg_eye_w = (ew1+ew2)/2
            scores["eye_spacing"] = max(0, 100-abs(eye_dist/max(avg_eye_w,1)-2.5)/2.5*100)
            scores["eye_level"]   = max(0, 100-abs(ey1-ey2)/max(fh,1)*500)
        else:
            scores["eye_spacing"] = 50
            scores["eye_level"]   = 50

        scores["face_clarity"] = min(100, fw*fh/max(w*h,1)*400)

        weights = {"symmetry":0.35,"golden_ratio":0.25,"eye_spacing":0.20,"eye_level":0.10,"face_clarity":0.10}
        total   = sum(scores[k]*weights[k] for k in weights)
        total   = max(0, min(100, total))
        return {"score": round(total,1), "details": scores, "error": None}

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}", exc_info=True)
        return {"error": f"Ошибка: {str(e)[:100]}"}

# ──────────────────────────────────────────────
# МЕНЮ
# ──────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Оценить внешность", callback_data="rate_me")],
        [InlineKeyboardButton("⚔️ ММ Мужчины", callback_data="mm_male"),
         InlineKeyboardButton("⚔️ ММ Женщины", callback_data="mm_female")],
        [InlineKeyboardButton("🏆 Топ мужчин",  callback_data="lb_male"),
         InlineKeyboardButton("🏆 Топ женщин",  callback_data="lb_female")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я оцениваю внешность по геометрии лица.\n\nВыбери действие:",
        reply_markup=main_menu_keyboard(),
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

    if mode == "match":
        gender = mm_gender
        keyboard = [[InlineKeyboardButton(
            "👨 Мужчина" if gender == "male" else "👩 Женщина",
            callback_data=f"gender_{gender}_match"
        )]]
        await update.message.reply_text("Фото получено! Нажми чтобы подтвердить:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard = [[
            InlineKeyboardButton("👨 Мужчина", callback_data="gender_male_rate"),
            InlineKeyboardButton("👩 Женщина", callback_data="gender_female_rate"),
        ]]
        await update.message.reply_text("Фото получено! Укажи пол:", reply_markup=InlineKeyboardMarkup(keyboard))

# ──────────────────────────────────────────────
# CALLBACKS
# ──────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = update.effective_user.id
    user    = update.effective_user

    if data == "rate_me":
        user_data_store[user_id] = {"mode": "rate"}
        await query.edit_message_text("📸 Пришли фото лица анфас!")
        return

    if data in ("lb_male", "lb_female"):
        gender = "male" if data == "lb_male" else "female"
        label  = "мужчин 👨" if gender == "male" else "женщин 👩"
        rows   = get_leaderboard(gender)
        if not rows:
            await query.edit_message_text(f"🏆 Топ {label} пуст!", reply_markup=main_menu_keyboard())
            return
        text   = f"🏆 *Топ {label}:*\n\n"
        medals = ["🥇","🥈","🥉"]
        for i, (uname, score, cat, wins, losses) in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} {uname or 'Аноним'} — *{cat}* ({score:.0f} б) | {wins}W/{losses}L\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data == "profile":
        row = get_player(user_id)
        if not row:
            await query.edit_message_text("👤 Профиля нет. Пройди оценку!", reply_markup=main_menu_keyboard())
            return
        uname, score, cat, wins, losses, matches, gender, in_lb = row
        winrate  = round(wins/matches*100) if matches > 0 else 0
        icon     = "👨" if gender == "male" else "👩"
        lb_status = "✅ Да" if in_lb else "❌ Нет"

        # История
        history = get_history(user_id)
        hist_text = ""
        if history:
            hist_text = "\n\n📈 *История оценок:*\n"
            for h_score, h_cat, h_date in history:
                date_str = h_date[:10] if h_date else "—"
                hist_text += f"• {date_str} — *{h_cat}* ({h_score:.0f} б)\n"

        text = (
            f"{icon} *Твой профиль*\n\n"
            f"🎯 Категория: *{cat.upper()}*\n"
            f"📊 Балл: *{score:.1f}/100*\n\n"
            f"⚔️ Матчей: {matches}\n"
            f"✅ Побед: {wins}\n"
            f"❌ Поражений: {losses}\n"
            f"📈 Винрейт: {winrate}%\n\n"
            f"🏆 В таблице лидеров: {lb_status}"
            f"{hist_text}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "👁 Скрыть из таблицы" if in_lb else "👁 Показать в таблице",
                callback_data="toggle_lb"
            )],
            [InlineKeyboardButton("🔙 Назад", callback_data="back_menu")],
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return

    if data == "toggle_lb":
        new_val = toggle_leaderboard(user_id)
        status  = "✅ Теперь ты в таблице лидеров!" if new_val else "❌ Ты скрыт из таблицы лидеров."
        await query.edit_message_text(status, reply_markup=main_menu_keyboard())
        return

    if data == "back_menu":
        await query.edit_message_text("Выбери действие:", reply_markup=main_menu_keyboard())
        return

    if data in ("mm_male", "mm_female"):
        mm_gender = "male" if data == "mm_male" else "female"
        await start_matchmaking(query, user_id, user, context, mm_gender)
        return

    if data == "cancel_queue":
        for q in matchmaking_queues.values():
            if user_id in q:
                q.remove(user_id)
        user_data_store.pop(user_id, None)
        await query.edit_message_text("❌ Поиск отменён.", reply_markup=main_menu_keyboard())
        return

    # Советы
    if data.startswith("tips_"):
        _, category, gender = data.split("|")
        text = get_tips_text(category, gender)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data.startswith("gender_"):
        parts  = data.split("_")
        gender = parts[1]
        mode   = parts[2]

        stored = user_data_store.get(user_id, {})
        if not stored.get("file_id"):
            await query.edit_message_text("❌ Фото не найдено. Пришли фото снова.")
            return

        file_id = stored["file_id"]
        user_data_store[user_id]["gender"] = gender
        await query.edit_message_text("⏳ Анализирую лицо...")

        try:
            file       = await context.bot.get_file(file_id)
            file_bytes = await file.download_as_bytearray()
            result     = analyze_face(bytes(file_bytes), gender)

            if result.get("error"):
                await query.edit_message_text(f"❌ {result['error']}", reply_markup=main_menu_keyboard())
                return

            score    = result["score"]
            details  = result["details"]
            category, emoji, desc = get_category(score, gender)
            username = user.first_name or user.username or "Аноним"
            save_player(user_id, username, score, category, gender)

            if mode == "rate":
                bar  = "█" * int(score/5) + "░" * (20 - int(score/5))
                text = (
                    f"{'👨' if gender=='male' else '👩'} *Результат оценки*\n\n"
                    f"🎯 Категория: *{category.upper()}* {emoji}\n\n"
                    f"📊 Балл: *{score}/100*\n"
                    f"`{bar}`\n\n"
                    f"_{desc}_\n\n"
                    f"📐 *Детали:*\n"
                    f"• Симметрия:       `{details.get('symmetry',0):.0f}/100`\n"
                    f"• Золотое сечение: `{details.get('golden_ratio',0):.0f}/100`\n"
                    f"• Расп. глаз:      `{details.get('eye_spacing',0):.0f}/100`\n"
                    f"• Уровень глаз:    `{details.get('eye_level',0):.0f}/100`\n"
                    f"• Чёткость фото:   `{details.get('face_clarity',0):.0f}/100`\n"
                )
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("💡 Получить советы по улучшению", callback_data=f"tips_|{category}|{gender}")],
                    [InlineKeyboardButton("🔙 В меню", callback_data="back_menu")],
                ])
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

            elif mode == "match":
                await process_match_result(query, user_id, score, category, emoji, context)

        except Exception as e:
            logger.error(f"Callback error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка: {str(e)[:150]}", reply_markup=main_menu_keyboard())
        return

    await query.edit_message_text("Выбери действие:", reply_markup=main_menu_keyboard())

# ──────────────────────────────────────────────
# МАТЧМЕЙКИНГ
# ──────────────────────────────────────────────

async def start_matchmaking(query, user_id, user, context, mm_gender):
    for q in matchmaking_queues.values():
        if user_id in q:
            await query.edit_message_text(
                "⏳ Ты уже в очереди...",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_queue")]])
            )
            return

    queue = matchmaking_queues[mm_gender]
    opponent_id = next((uid for uid in queue if uid != user_id), None)

    if opponent_id:
        queue.remove(opponent_id)
        opp_name = user_data_store.get(opponent_id, {}).get("first_name", "Аноним")

        match_store[user_id]     = {"opponent_id": opponent_id, "my_score": None, "opp_score": None,
                                     "my_cat": None, "opp_cat": None, "my_emoji": None, "opp_emoji": None,
                                     "opp_name": opp_name}
        match_store[opponent_id] = {"opponent_id": user_id, "my_score": None, "opp_score": None,
                                     "my_cat": None, "opp_cat": None, "my_emoji": None, "opp_emoji": None,
                                     "opp_name": user.first_name or "Аноним"}

        user_data_store[user_id]     = {"mode": "match", "mm_gender": mm_gender}
        user_data_store[opponent_id] = {"mode": "match", "mm_gender": mm_gender}

        await query.edit_message_text(
            f"✅ Соперник найден!\n\n⚔️ Против: *{opp_name}*\n\n📸 Пришли своё фото!",
            parse_mode="Markdown"
        )
        try:
            await context.bot.send_message(
                chat_id=opponent_id,
                text=f"✅ Соперник найден!\n\n⚔️ Против: *{user.first_name or 'Аноним'}*\n\n📸 Пришли своё фото!",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить соперника: {e}")
    else:
        queue.append(user_id)
        user_data_store[user_id] = {"mode": "match", "mm_gender": mm_gender, "first_name": user.first_name}
        label = "👨 мужчин" if mm_gender == "male" else "👩 женщин"
        await query.edit_message_text(
            f"🔍 Ищем соперника в очереди {label}...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_queue")]])
        )


async def process_match_result(query, user_id, score, category, emoji, context):
    if user_id not in match_store:
        await query.edit_message_text("❌ Матч не найден. Начни заново.", reply_markup=main_menu_keyboard())
        return

    match       = match_store[user_id]
    opponent_id = match["opponent_id"]

    match["my_score"] = score
    match["my_cat"]   = category
    match["my_emoji"] = emoji

    if opponent_id in match_store:
        match_store[opponent_id]["opp_score"] = score
        match_store[opponent_id]["opp_cat"]   = category
        match_store[opponent_id]["opp_emoji"] = emoji

    opp_match = match_store.get(opponent_id, {})
    opp_score = opp_match.get("my_score")

    if opp_score is None:
        await query.edit_message_text("✅ Фото принято! Ждём фото соперника...")
        try:
            await context.bot.send_message(chat_id=opponent_id, text="⏳ Соперник уже прислал фото. Пришли своё!")
        except:
            pass
        return

    my_score  = score
    my_cat    = category
    opp_cat   = opp_match.get("my_cat", "?")
    my_emoji  = emoji
    opp_emoji = opp_match.get("my_emoji", "")
    opp_name  = match.get("opp_name", "Соперник")
    my_name   = match_store[opponent_id].get("opp_name", "Соперник")

    my_rank  = CATEGORY_RANK.get(my_cat, 0)
    opp_rank = CATEGORY_RANK.get(opp_cat, 0)

    if my_rank > opp_rank:
        winner_id, loser_id = user_id, opponent_id
        my_result, opp_result = "🏆 *ПОБЕДА!*", "💀 *ПОРАЖЕНИЕ*"
    elif opp_rank > my_rank:
        winner_id, loser_id = opponent_id, user_id
        my_result, opp_result = "💀 *ПОРАЖЕНИЕ*", "🏆 *ПОБЕДА!*"
    else:
        winner_id = loser_id = None
        my_result = opp_result = "🤝 *НИЧЬЯ*"

    if winner_id:
        update_match_result(winner_id, loser_id)

    my_text = (
        f"⚔️ *Результат матча*\n\n{my_result}\n\n"
        f"👤 Ты: *{my_cat.upper()}* {my_emoji} ({my_score:.0f} б)\n"
        f"👤 {opp_name}: *{opp_cat.upper()}* {opp_emoji} ({opp_score:.0f} б)\n"
    )
    opp_text = (
        f"⚔️ *Результат матча*\n\n{opp_result}\n\n"
        f"👤 Ты: *{opp_cat.upper()}* {opp_emoji} ({opp_score:.0f} б)\n"
        f"👤 {my_name}: *{my_cat.upper()}* {my_emoji} ({my_score:.0f} б)\n"
    )

    await query.edit_message_text(my_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    try:
        await context.bot.send_message(chat_id=opponent_id, text=opp_text,
                                        parse_mode="Markdown", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Не удалось отправить результат: {e}")

    for uid in [user_id, opponent_id]:
        match_store.pop(uid, None)
        user_data_store.pop(uid, None)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_keyboard())

# ──────────────────────────────────────────────
# ВЕБ + САМОПИНГ
# ──────────────────────────────────────────────

async def health(request):
    return web.Response(text="OK")

async def run_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {PORT}")

async def self_ping():
    if not RENDER_URL:
        return
    import aiohttp
    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    logger.info(f"Самопинг: {resp.status}")
        except Exception as e:
            logger.warning(f"Самопинг не удался: {e}")
        await asyncio.sleep(20)

async def main():
    init_db()
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    await run_web()
    asyncio.create_task(self_ping())

    async with bot_app:
        await bot_app.start()
        await bot_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Бот запущен!")
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
