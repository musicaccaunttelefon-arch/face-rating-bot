import logging
import os
import numpy as np
import sqlite3
import asyncio
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

user_data_store = {}      # временные данные (фото, пол)
matchmaking_queue = {}    # очередь: user_id -> {gender, user_info}
active_matches = {}       # активные матчи: user_id -> opponent_id

# ──────────────────────────────────────────────
# БАЗА ДАННЫХ
# ──────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS players (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            score       REAL DEFAULT 0,
            category    TEXT DEFAULT '',
            gender      TEXT DEFAULT '',
            wins        INTEGER DEFAULT 0,
            losses      INTEGER DEFAULT 0,
            matches     INTEGER DEFAULT 0
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
            username=excluded.username,
            score=excluded.score,
            category=excluded.category,
            gender=excluded.gender
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

def get_leaderboard(gender=None):
    conn = sqlite3.connect("ratings.db")
    c = conn.cursor()
    if gender:
        c.execute("SELECT username, score, category, wins, losses FROM players WHERE gender=? ORDER BY score DESC LIMIT 10", (gender,))
    else:
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
    ("sub 3",     0,  20, "😔"),
    ("sub 5",    20,  35, "😐"),
    ("ltn",      35,  50, "🙂"),
    ("mtn",      50,  62, "😊"),
    ("htn",      62,  74, "😎"),
    ("chad",     74,  88, "🔥"),
    ("true adam",88, 101, "👑"),
]

FEMALE_CATEGORIES = [
    ("sub 3",    0,  20, "😔"),
    ("sub 5",   20,  35, "😐"),
    ("ltb",     35,  50, "🙂"),
    ("mtb",     50,  62, "😊"),
    ("htb",     62,  74, "😍"),
    ("stacy",   74,  88, "🔥"),
    ("true eve",88, 101, "👑"),
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
    for name, low, high, emoji in cats:
        if low <= score < high:
            return name, emoji
    return cats[-1][0], cats[-1][3]

# ──────────────────────────────────────────────
# АНАЛИЗ ЛИЦА
# ──────────────────────────────────────────────

def analyze_face(image_bytes: bytes, gender: str) -> dict:
    try:
        import cv2
        import mediapipe as mp
        import random

        nparr   = np.frombuffer(image_bytes, np.uint8)
        img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        h, w    = img_rgb.shape[:2]

        mp_face_mesh = mp.solutions.face_mesh
        with mp_face_mesh.FaceMesh(
            static_image_mode=True,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5
        ) as face_mesh:
            results = face_mesh.process(img_rgb)

        if not results.multi_face_landmarks:
            return {"error": "Лицо не обнаружено. Пришли чёткое фото анфас при хорошем освещении."}

        lm = results.multi_face_landmarks[0].landmark

        def pt(idx):
            return np.array([lm[idx].x * w, lm[idx].y * h])

        left_eye_inner   = pt(133); left_eye_outer  = pt(33)
        right_eye_inner  = pt(362); right_eye_outer = pt(263)
        left_eye_center  = (left_eye_inner  + left_eye_outer)  / 2
        right_eye_center = (right_eye_inner + right_eye_outer) / 2
        nose_tip  = pt(4);  nose_base = pt(2)
        mouth_left = pt(61); mouth_right = pt(291)
        mouth_center = (mouth_left + mouth_right) / 2
        chin = pt(152); forehead = pt(10)
        cheek_left = pt(234); cheek_right = pt(454)

        scores = {}
        face_width  = np.linalg.norm(cheek_right - cheek_left)
        face_height = np.linalg.norm(chin - forehead)

        face_cx   = (cheek_left[0] + cheek_right[0]) / 2
        sym_eyes  = abs(left_eye_center[0]  - (face_cx - (right_eye_center[0] - face_cx)))
        sym_mouth = abs(mouth_center[0] - face_cx)
        sym_nose  = abs(nose_tip[0] - face_cx)
        scores["symmetry"] = max(0, 100 - ((sym_eyes + sym_mouth + sym_nose) / max(face_width,1)) * 300)

        phi_diff = abs(face_height / max(face_width,1) - 1.618) / 1.618
        scores["golden_ratio"] = max(0, 100 - phi_diff * 200)

        eye_dist  = np.linalg.norm(right_eye_center - left_eye_center)
        avg_eye_w = (np.linalg.norm(left_eye_outer - left_eye_inner) + np.linalg.norm(right_eye_outer - right_eye_inner)) / 2
        scores["eye_spacing"] = max(0, 100 - abs(eye_dist / max(avg_eye_w,1) - 3.0) / 3.0 * 150)

        eye_line = (left_eye_center[1] + right_eye_center[1]) / 2
        t1 = eye_line - forehead[1]; t2 = nose_base[1] - eye_line; t3 = chin[1] - nose_base[1]
        total_h = t1 + t2 + t3
        if total_h > 0:
            ideal = 1/3
            scores["thirds"] = max(0, 100 - (abs(t1/total_h-ideal)+abs(t2/total_h-ideal)+abs(t3/total_h-ideal))*200)
        else:
            scores["thirds"] = 50

        mouth_width = np.linalg.norm(mouth_right - mouth_left)
        scores["mouth_ratio"] = max(0, 100 - abs(mouth_width / max(avg_eye_w*2.5,1) - 1.0) * 100)

        weights = {"symmetry":0.35,"golden_ratio":0.20,"eye_spacing":0.20,"thirds":0.15,"mouth_ratio":0.10}
        total_score = sum(scores[k]*weights[k] for k in weights)
        total_score = max(0, min(100, total_score + random.uniform(-3, 3)))

        return {"score": round(total_score, 1), "details": scores, "error": None}

    except ImportError as e:
        return {"error": f"Не установлена библиотека: {e}"}
    except Exception as e:
        logger.error(f"Ошибка анализа: {e}", exc_info=True)
        return {"error": f"Ошибка при анализе: {str(e)[:120]}"}

# ──────────────────────────────────────────────
# ГЛАВНОЕ МЕНЮ
# ──────────────────────────────────────────────

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Оценить внешность", callback_data="rate_me")],
        [InlineKeyboardButton("⚔️ Матчмейкинг", callback_data="matchmaking")],
        [InlineKeyboardButton("🏆 Таблица лидеров", callback_data="leaderboard")],
        [InlineKeyboardButton("👤 Мой профиль", callback_data="profile")],
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
# ОЦЕНКА ВНЕШНОСТИ
# ──────────────────────────────────────────────

async def ask_gender(query_or_message, user_id, mode="rate"):
    keyboard = [[
        InlineKeyboardButton("👨 Мужчина", callback_data=f"gender_male_{mode}"),
        InlineKeyboardButton("👩 Женщина", callback_data=f"gender_female_{mode}"),
    ]]
    text = "Укажи пол для правильной оценки:"
    if hasattr(query_or_message, 'edit_message_text'):
        await query_or_message.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query_or_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    photo   = update.message.photo[-1]

    mode = user_data_store.get(user_id, {}).get("mode", "rate")
    user_data_store[user_id] = {
        "file_id": photo.file_id,
        "mode": mode
    }

    keyboard = [[
        InlineKeyboardButton("👨 Мужчина", callback_data=f"gender_male_{mode}"),
        InlineKeyboardButton("👩 Женщина", callback_data=f"gender_female_{mode}"),
    ]]
    await update.message.reply_text(
        "Фото получено! Укажи пол:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user_id = update.effective_user.id
    user    = update.effective_user

    # ── Главное меню ──
    if data == "rate_me":
        user_data_store[user_id] = {"mode": "rate"}
        await query.edit_message_text("📸 Пришли фото лица анфас — оценю внешность!")
        return

    if data == "leaderboard":
        rows = get_leaderboard()
        if not rows:
            await query.edit_message_text("🏆 Таблица пока пуста. Пройди оценку первым!", reply_markup=main_menu_keyboard())
            return
        text = "🏆 *Топ-10 по внешности:*\n\n"
        medals = ["🥇","🥈","🥉"]
        for i, (uname, score, cat, wins, losses) in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            text += f"{medal} @{uname or 'Аноним'} — *{cat}* ({score:.0f} баллов) | ⚔️ {wins}W/{losses}L\n"
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())
        return

    if data == "profile":
        row = get_player(user_id)
        if not row:
            await query.edit_message_text("👤 У тебя ещё нет профиля. Пройди оценку!", reply_markup=main_menu_keyboard())
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
                "⚔️ Для матчмейкинга нужно сначала пройти оценку внешности!\n\nНажми '📸 Оценить внешность'.",
                reply_markup=main_menu_keyboard()
            )
            return
        await start_matchmaking(query, user_id, user, context)
        return

    if data == "cancel_queue":
        matchmaking_queue.pop(user_id, None)
        await query.edit_message_text("❌ Поиск отменён.", reply_markup=main_menu_keyboard())
        return

    # ── Выбор пола ──
    if data.startswith("gender_"):
        parts  = data.split("_")
        gender = parts[1]   # male / female
        mode   = parts[2]   # rate / match

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

            score   = result["score"]
            details = result["details"]
            category, emoji = get_category(score, gender)
            username = user.username or user.first_name or "Аноним"

            save_player(user_id, username, score, category, gender)

            if mode == "rate":
                bar  = "█" * int(score/5) + "░" * (20 - int(score/5))
                text = (
                    f"{'👨' if gender=='male' else '👩'} *Результат оценки*\n\n"
                    f"🎯 Категория: *{category.upper()}* {emoji}\n\n"
                    f"📊 Балл: *{score}/100*\n"
                    f"`{bar}`\n\n"
                    f"📐 *Детали:*\n"
                    f"• Симметрия:       `{details.get('symmetry',0):.0f}/100`\n"
                    f"• Золотое сечение: `{details.get('golden_ratio',0):.0f}/100`\n"
                    f"• Расп. глаз:      `{details.get('eye_spacing',0):.0f}/100`\n"
                    f"• Правило третей:  `{details.get('thirds',0):.0f}/100`\n"
                    f"• Пропорции рта:   `{details.get('mouth_ratio',0):.0f}/100`\n"
                )
                await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

            elif mode == "match":
                # Сохраняем результат и запускаем финал матча
                user_data_store[user_id]["score"]    = score
                user_data_store[user_id]["category"] = category
                user_data_store[user_id]["emoji"]    = emoji
                await finalize_match(query, user_id, context)

        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Ошибка: {str(e)[:150]}", reply_markup=main_menu_keyboard())

        return

    # ── Меню ──
    await query.edit_message_text("Выбери действие:", reply_markup=main_menu_keyboard())

# ──────────────────────────────────────────────
# МАТЧМЕЙКИНГ
# ──────────────────────────────────────────────

async def start_matchmaking(query, user_id, user, context):
    # Уже в очереди?
    if user_id in matchmaking_queue:
        await query.edit_message_text(
            "⏳ Ты уже в очереди поиска...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="cancel_queue")]])
        )
        return

    # Ищем соперника в очереди
    opponent_id = None
    for qid in list(matchmaking_queue.keys()):
        if qid != user_id:
            opponent_id = qid
            break

    if opponent_id:
        # Нашли соперника — создаём матч
        opponent_info = matchmaking_queue.pop(opponent_id)
        active_matches[user_id]      = opponent_id
        active_matches[opponent_id]  = user_id

        # Просим обоих прислать фото
        user_data_store[user_id]     = {"mode": "match", "opponent": opponent_id}
        user_data_store[opponent_id] = {"mode": "match", "opponent": user_id, **opponent_info}

        await query.edit_message_text(
            f"✅ Соперник найден!\n\n"
            f"⚔️ Ты сражаешься против **{opponent_info.get('name','Аноним')}**\n\n"
            f"📸 Пришли своё фото для оценки!",
            parse_mode="Markdown"
        )

        # Уведомляем соперника
        try:
            await context.bot.send_message(
                chat_id=opponent_id,
                text=f"✅ Соперник найден!\n\n"
                     f"⚔️ Ты сражаешься против **{user.first_name}**\n\n"
                     f"📸 Пришли своё фото для оценки!",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Не удалось уведомить соперника: {e}")

    else:
        # Встаём в очередь
        matchmaking_queue[user_id] = {"name": user.first_name or "Аноним", "username": user.username}

        await query.edit_message_text(
            "🔍 Ищем соперника...\n\nКак только найдём — пришлёшь фото для боя!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить поиск", callback_data="cancel_queue")]])
        )


async def finalize_match(query, user_id, context):
    opponent_id = user_data_store.get(user_id, {}).get("opponent")
    if not opponent_id:
        await query.edit_message_text("❌ Ошибка матча. Попробуй снова.", reply_markup=main_menu_keyboard())
        return

    my_data  = user_data_store.get(user_id, {})
    opp_data = user_data_store.get(opponent_id, {})

    my_score  = my_data.get("score")
    opp_score = opp_data.get("score")

    # Ждём пока оба загрузят фото
    if my_score is None:
        await query.edit_message_text("✅ Фото принято! Ждём фото соперника...")
        return

    if opp_score is None:
        await query.edit_message_text("✅ Фото принято! Ждём фото соперника...")
        # Уведомляем соперника что ждём его
        try:
            await context.bot.send_message(
                chat_id=opponent_id,
                text="⏳ Соперник уже прислал фото. Твоя очередь — пришли фото!"
            )
        except:
            pass
        return

    # Оба прислали — подводим итог
    my_cat   = my_data.get("category", "?")
    opp_cat  = opp_data.get("category", "?")
    my_emoji = my_data.get("emoji", "")
    opp_emoji= opp_data.get("emoji", "")

    my_rank  = CATEGORY_RANK.get(my_cat, 0)
    opp_rank = CATEGORY_RANK.get(opp_cat, 0)

    opp_name = user_data_store.get(opponent_id, {}).get("name", "Соперник")
    my_name  = "Ты"

    if my_rank > opp_rank:
        winner_id, loser_id = user_id, opponent_id
        my_result  = "🏆 *ПОБЕДА!*"
        opp_result = "💀 *ПОРАЖЕНИЕ*"
    elif opp_rank > my_rank:
        winner_id, loser_id = opponent_id, user_id
        my_result  = "💀 *ПОРАЖЕНИЕ*"
        opp_result = "🏆 *ПОБЕДА!*"
    else:
        winner_id, loser_id = None, None
        my_result  = "🤝 *НИЧЬЯ*"
        opp_result = "🤝 *НИЧЬЯ*"

    if winner_id:
        update_match_result(winner_id, loser_id)

    my_text = (
        f"⚔️ *Результат матча*\n\n"
        f"{my_result}\n\n"
        f"👤 Ты: *{my_cat.upper()}* {my_emoji} ({my_score:.0f} баллов)\n"
        f"👤 Соперник: *{opp_cat.upper()}* {opp_emoji} ({opp_score:.0f} баллов)\n"
    )
    opp_text = (
        f"⚔️ *Результат матча*\n\n"
        f"{opp_result}\n\n"
        f"👤 Ты: *{opp_cat.upper()}* {opp_emoji} ({opp_score:.0f} баллов)\n"
        f"👤 Соперник: *{my_cat.upper()}* {my_emoji} ({my_score:.0f} баллов)\n"
    )

    await query.edit_message_text(my_text, parse_mode="Markdown", reply_markup=main_menu_keyboard())

    try:
        await context.bot.send_message(chat_id=opponent_id, text=opp_text, parse_mode="Markdown",
                                       reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Не удалось отправить результат сопернику: {e}")

    # Чистим данные
    user_data_store.pop(user_id, None)
    user_data_store.pop(opponent_id, None)
    active_matches.pop(user_id, None)
    active_matches.pop(opponent_id, None)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери действие:", reply_markup=main_menu_keyboard())


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
