import logging
import os
import numpy as np
import sqlite3
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")

user_data_store = {}
matchmaking_queue = {}
active_matches = {}

# ──────────────────────────────────────────────
# БАЗА ДАННЫХ
# ──────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            score     REAL DEFAULT 0,
            category  TEXT DEFAULT '',
            gender    TEXT DEFAULT '',
            wins      INTEGER DEFAULT 0,
            losses    INTEGER DEFAULT 0,
            matches   INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_player(user_id, username, score, category, gender):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("""
        INSERT INTO players (user_id, username, score, category, gender)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username, score=excluded.score,
            category=excluded.category, gender=excluded.gender
    """, (user_id, username, score, category, gender))
    conn.commit()
    conn.close()

def update_match_result(winner_id, loser_id):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("UPDATE players SET wins=wins+1, matches=matches+1 WHERE user_id=?", (winner_id,))
    c.execute("UPDATE players SET losses=losses+1, matches=matches+1 WHERE user_id=?", (loser_id,))
    conn.commit()
    conn.close()

def get_leaderboard():
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("SELECT username, score, category, wins, losses FROM players ORDER BY score DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    return rows

def get_player(user_id):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("SELECT username, score, category, wins, losses, matches FROM players WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

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
    "sub 3": 0, "sub 5": 1,
    "ltn": 2, "ltb": 2,
    "mtn": 3, "mtb": 3,
    "htn": 4, "htb": 4,
    "chad": 5, "stacy": 5,
    "true adam": 6, "true eve": 6,
}

def get_category(score, gender):
    cats = MALE_CATEGORIES if gender == "male" else FEMALE_CATEGORIES
    for name, low, high, emoji, desc in cats:
        if low <= score < high:
            return name, emoji, desc
    return cats[-1][0], cats[-1][3], cats[-1][4]

# ──────────────────────────────────────────────
# АНАЛИЗ ЛИЦА (через OpenCV Haar cascade)
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

        # Haar cascade для лица
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        eye_cascade  = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_eye.xml')

        faces = face_cascade.detectMultiScale(img_gray, scaleFactor=1.1, minNeighbors=5, minSize=(80, 80))

        if len(faces) == 0:
            return {"error": "Лицо не обнаружено. Пришли чёткое фото анфас при хорошем освещении."}

        # Берём самое большое лицо
        fx, fy, fw, fh = max(faces, key=lambda f: f[2]*f[3])

        scores = {}

        # 1. Золотое сечение лица (высота/ширина, идеал 1.618)
        phi_ratio = fh / max(fw, 1)
        phi_diff  = abs(phi_ratio - 1.618) / 1.618
        scores["golden_ratio"] = max(0, 100 - phi_diff * 180)

        # 2. Симметрия через сравнение левой и правой половины
        face_img = img_gray[fy:fy+fh, fx:fx+fw]
        mid = fw // 2
        left_half  = face_img[:, :mid]
        right_half = cv2.flip(face_img[:, mid:], 1)
        min_w = min(left_half.shape[1], right_half.shape[1])
        diff  = cv2.absdiff(left_half[:, :min_w].astype(float), right_half[:, :min_w].astype(float))
        sym_score = 100 - min(100, (diff.mean() / 128) * 100 * 2)
        scores["symmetry"] = max(0, sym_score)

        # 3. Глаза — ищем в верхней части лица
        face_top = img_gray[fy:fy+fh//2, fx:fx+fw]
        eyes = eye_cascade.detectMultiScale(face_top, scaleFactor=1.1, minNeighbors=3)

        if len(eyes) >= 2:
            eyes = sorted(eyes, key=lambda e: e[0])
            ex1, ey1, ew1, eh1 = eyes[0]
            ex2, ey2, ew2, eh2 = eyes[1]
            eye1_cx = ex1 + ew1//2
            eye2_cx = ex2 + ew2//2
            eye_dist = abs(eye2_cx - eye1_cx)
            avg_eye_w = (ew1 + ew2) / 2
            eye_ratio = eye_dist / max(avg_eye_w, 1)
            scores["eye_spacing"] = max(0, 100 - abs(eye_ratio - 2.5) / 2.5 * 100)

            # Симметрия глаз по вертикали
            vert_diff = abs(ey1 - ey2) / max(fh, 1) * 100
            scores["eye_level"] = max(0, 100 - vert_diff * 5)
        else:
            scores["eye_spacing"] = 50 + random.uniform(-5, 5)
            scores["eye_level"]   = 50 + random.uniform(-5, 5)

        # 4. Пропорция лица в кадре (насколько чётко видно лицо)
        face_area  = fw * fh
        total_area = w * h
        ratio = face_area / max(total_area, 1)
        scores["face_clarity"] = min(100, ratio * 400)

        # Итог с весами
        weights = {
            "symmetry":    0.35,
            "golden_ratio":0.25,
            "eye_spacing": 0.20,
            "eye_level":   0.10,
            "face_clarity":0.10,
        }
        total_score = sum(scores[k] * weights[k] for k in weights)
        total_score = max(0, min(100, total_score + random.uniform(-4, 4)))

        return {"score": round(total_score, 1), "details": scores, "error": None}

    except Exception as e:
        logger.error(f"Ошибка анализа: {e}", exc_info=True)
        return {"error": f"Ошибка при анализе: {str(e)[:120]}"}

# ──────────────────────────────────────────────
# МЕНЮ
# ──────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Оценить внешность", callback_data="rate_me")],
        [InlineKeyboardButton("⚔️ Матчмейкинг",       callback_data="matchmaking")],
        [InlineKeyboardButton("🏆 Таблица лидеров",   callback_data="leaderboard")],
        [InlineKeyboardButton("👤 Мой профиль",        callback_data="profile")],
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Я оцениваю внешность по геометрии лица.\n\n"
        "😴 *Если бот не отвечает* — напиши /start и подожди 30 секунд. Он просто спал!\n\n"
        "Выбери действие:",
        reply_markup=main_menu_keyboard(),
        parse_mode="Markdown"
    )

# ──────────────────────────────────────────────
# ФОТО
# ──────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo   = update.message.photo[-1]
    mode    = user_data_store.get(user_id, {}).get("mode", "rate")
    user_data_store[user_id] = {"file_id": photo.file_id, "mode": mode}

    keyboard = [[
        InlineKeyboardButton("👨 Мужчина", callback_data=f"gender_male_{mode}"),
        InlineKeyboardButton("👩 Женщина", callback_data=f"gender_female_{mode}"),
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
        await query.edit_message_text("📸 Пришли фото лица анфас — оценю внешность!")
        return

    if data == "leaderboard":
        rows = get_leaderboard()
        if not rows:
            await query.edit_message_text("🏆 Таблица пока пуста!", reply_markup=main_menu_keyboard())
            return
        text = "🏆 *Топ-10 по внешности:*\n\n"
        medals = ["🥇","🥈","🥉"]
        for i, (uname, score, cat, wins, losses) in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} @{uname or 'Аноним'} — *{cat}* ({score:.0f} б) | {wins}W/{losses}L\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data == "profile":
        row = get_player(user_id)
        if not row:
            await query.edit_message_text("👤 Профиля нет. Пройди оценку!", reply_markup=main_menu_keyboard())
            return
        uname, score, cat, wins, losses, matches = row
        winrate = round(wins/matches*100) if matches > 0 else 0
        text = (
            f"👤 *Твой профиль*\n\n"
            f"🎯 Категория: *{cat.upper()}*\n"
            f"📊 Балл: *{score:.1f}/100*\n\n"
            f"⚔️ Матчей: {matches}\n"
            f"✅ Побед: {wins}\n"
            f"❌ Поражений: {losses}\n"
            f"📈 Винрейт: {winrate}%"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data == "matchmaking":
        row = get_player(user_id)
        if not row:
            await query.edit_message_text(
                "⚔️ Сначала пройди оценку внешности!",
                reply_markup=main_menu_keyboard()
            )
            return
        await start_matchmaking(query, user_id, user, context)
        return

    if data == "cancel_queue":
        matchmaking_queue.pop(user_id, None)
        await query.edit_message_text("❌ Поиск отменён.", reply_markup=main_menu_keyboard())
        return

    if data.startswith("gender_"):
        parts  = data.split("_")
        gender = parts[1]
        mode   = parts[2]

        if user_id not in user_data_store or "file_id" not in user_data_store[user_id]:
            await query.edit_message_text("❌ Фото не найдено. Пришли фото снова.")
            return

        file_id = user_data_store[user_id]["file_id"]
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
            username = user.username or user.first_name or "Аноним"
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
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

            elif mode == "match":
                user_data_store[user_id]["score"]    = score
                user_data_store[user_id]["category"] = category
                user_data_store[user_id]["emoji"]    = emoji
                await finalize_match(query, user_id, context)

        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка: {str(e)[:150]}", reply_markup=main_menu_keyboard())
        return

    await query.edit_message_text("Выбери действие:", reply_markup=main_menu_keyboard())

# ──────────────────────────────────────────────
# МАТЧМЕЙКИНГ
# ──────────────────────────────────────────────

async def start_matchmaking(query, user_id, user, context):
    if user_id in matchmaking_queue:
        await query.edit_message_text(
            "⏳ Ты уже в очереди...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_queue")]])
        )
        return

    opponent_id = next((qid for qid in matchmaking_queue if qid != user_id), None)

    if opponent_id:
        opponent_info = matchmaking_queue.pop(opponent_id)
        active_matches[user_id]     = opponent_id
        active_matches[opponent_id] = user_id
        user_data_store[user_id]     = {"mode": "match", "opponent": opponent_id}
        user_data_store[opponent_id] = {"mode": "match", "opponent": user_id, **opponent_info}

        await query.edit_message_text(
            f"✅ Соперник найден!\n\n"
            f"⚔️ Против: *{opponent_info.get('name','Аноним')}*\n\n"
            f"📸 Пришли своё фото!",
            parse_mode="Markdown"
        )
        try:
            await context.bot.send_message(
                chat_id=opponent_id,
                text=f"✅ Соперник найден!\n\n⚔️ Против: *{user.first_name}*\n\n📸 Пришли своё фото!",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить соперника: {e}")
    else:
        matchmaking_queue[user_id] = {"name": user.first_name or "Аноним", "username": user.username}
        await query.edit_message_text(
            "🔍 Ищем соперника...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_queue")]])
        )


async def finalize_match(query, user_id, context):
    opponent_id = user_data_store.get(user_id, {}).get("opponent")
    if not opponent_id:
        await query.edit_message_text("❌ Ошибка матча.", reply_markup=main_menu_keyboard())
        return

    my_data  = user_data_store.get(user_id, {})
    opp_data = user_data_store.get(opponent_id, {})
    my_score  = my_data.get("score")
    opp_score = opp_data.get("score")

    if my_score is None or opp_score is None:
        await query.edit_message_text("✅ Фото принято! Ждём фото соперника...")
        if opp_score is None:
            try:
                await context.bot.send_message(chat_id=opponent_id, text="⏳ Соперник уже прислал фото. Пришли своё!")
            except:
                pass
        return

    my_cat   = my_data.get("category","?")
    opp_cat  = opp_data.get("category","?")
    my_emoji = my_data.get("emoji","")
    opp_emoji= opp_data.get("emoji","")
    my_rank  = CATEGORY_RANK.get(my_cat, 0)
    opp_rank = CATEGORY_RANK.get(opp_cat, 0)

    if my_rank > opp_rank:
        winner_id, loser_id = user_id, opponent_id
        my_result, opp_result = "🏆 *ПОБЕДА!*", "💀 *ПОРАЖЕНИЕ*"
    elif opp_rank > my_rank:
        winner_id, loser_id = opponent_id, user_id
        my_result, opp_result = "💀 *ПОРАЖЕНИЕ*", "🏆 *ПОБЕДА!*"
    else:
        winner_id, loser_id = None, None
        my_result = opp_result = "🤝 *НИЧЬЯ*"

    if winner_id:
        update_match_result(winner_id, loser_id)

    my_text = (
        f"⚔️ *Результат матча*\n\n{my_result}\n\n"
        f"👤 Ты: *{my_cat.upper()}* {my_emoji} ({my_score:.0f} б)\n"
        f"👤 Соперник: *{opp_cat.upper()}* {opp_emoji} ({opp_score:.0f} б)\n"
    )
    opp_text = (
        f"⚔️ *Результат матча*\n\n{opp_result}\n\n"
        f"👤 Ты: *{opp_cat.upper()}* {opp_emoji} ({opp_score:.0f} б)\n"
        f"👤 Соперник: *{my_cat.upper()}* {my_emoji} ({my_score:.0f} б)\n"
    )

    await query.edit_message_text(my_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    try:
        await context.bot.send_message(chat_id=opponent_id, text=opp_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Не удалось отправить результат сопернику: {e}")

    for uid in [user_id, opponent_id]:
        user_data_store.pop(uid, None)
        active_matches.pop(uid, None)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_keyboard())


async def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен!")
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Event().wait()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
