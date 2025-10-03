import os
import asyncio
from typing import List, Optional, Tuple, Dict
import aiosqlite
import httpx
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes
)

load_dotenv()

# ===== ENV =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TMDB_KEY = os.getenv("TMDB_API_KEY", "")
LANG = os.getenv("TMDB_LANG", "ru-RU")  # можно "en-US"
IMG_BASE = "https://image.tmdb.org/t/p/w500"
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN or not TMDB_KEY:
    raise SystemExit("Set BOT_TOKEN and TMDB_API_KEY env vars.")

# ===== Главная клавиатура =====
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("🔎 Поиск"), KeyboardButton("🎭 Жанр"), KeyboardButton("🌍 Страна")]],
    resize_keyboard=True
)

# ===== Страны (ISO 3166-1 alpha-2) =====
COUNTRIES = [
    ("US", "USA"), ("GB", "United Kingdom"), ("RU", "Russia"), ("UZ", "Uzbekistan"),
    ("FR", "France"), ("DE", "Germany"), ("ES", "Spain"), ("IT", "Italy"),
    ("JP", "Japan"), ("KR", "South Korea"), ("IN", "India"), ("CN", "China"), ("CA", "Canada")
]

# ===== Кэш жанров TMDb =====
_genres_cache: Dict[int, str] = {}  # id -> name

async def tmdb_genres() -> Dict[int, str]:
    """Загрузить и кэшировать жанры TMDb."""
    global _genres_cache
    if _genres_cache:
        return _genres_cache
    url = "https://api.themoviedb.org/3/genre/movie/list"
    params = {"api_key": TMDB_KEY, "language": LANG}
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url, params=params)
        data = r.json()
    _genres_cache = {g["id"]: g["name"] for g in data.get("genres", [])}
    return _genres_cache

# ===== TMDb API =====
async def tmdb_search(query: str, page: int = 1) -> dict:
    """Поиск фильмов по названию."""
    url = "https://api.themoviedb.org/3/search/movie"
    params = {"api_key": TMDB_KEY, "language": LANG, "query": query, "page": page, "include_adult": "false"}
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url, params=params)
        return r.json()

async def tmdb_discover_by_genre(genre_id: int, page: int = 1) -> dict:
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_KEY, "language": LANG, "with_genres": str(genre_id),
        "sort_by": "popularity.desc", "page": page, "include_adult": "false"
    }
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url, params=params)
        return r.json()

async def tmdb_discover_by_country(country_code: str, page: int = 1) -> dict:
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_KEY, "language": LANG, "with_origin_country": country_code,
        "sort_by": "popularity.desc", "page": page, "include_adult": "false"
    }
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url, params=params)
        return r.json()

async def tmdb_details(movie_id: int) -> Optional[dict]:
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": TMDB_KEY, "language": LANG}
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url, params=params)
        data = r.json()
    if data.get("id"):
        return data
    return None

# Провайдеры и видео
async def tmdb_watch_providers(movie_id: int) -> dict:
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/watch/providers"
    params = {"api_key": TMDB_KEY}
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url, params=params)
        return r.json().get("results", {})

async def tmdb_videos(movie_id: int) -> list:
    url = f"https://api.themoviedb.org/3/movie/{movie_id}/videos"
    params = {"api_key": TMDB_KEY, "language": LANG}
    async with httpx.AsyncClient(timeout=20) as cl:
        r = await cl.get(url, params=params)
        return r.json().get("results", [])

# ===== Вспомогалки UI =====
def movies_to_keyboard(results: List[dict]) -> InlineKeyboardMarkup:
    btns = []
    for m in results[:10]:
        title = m.get("title") or m.get("original_title") or "Untitled"
        movie_id = m.get("id")
        btns.append([InlineKeyboardButton(f"Подробнее: {title}", callback_data=f"det:{movie_id}")])
    return InlineKeyboardMarkup(btns)

def page_nav_cb(kind: str, page: int, payload: str) -> str:
    """Упаковать короткий callback_data: kind s/g/c, page int, payload (query|genreId|country)."""
    payload = str(payload)[:40]
    return f"pg:{kind}:{page}:{payload}"

def parse_page_cb(data: str):
    _, kind, page, payload = data.split(":", 3)
    return kind, int(page), payload

async def show_list_with_pagination(edit_target, kind: str, page: int, payload: str, title_line: str):
    # kind: "s" (search), "g" (genre), "c" (country)
    if kind == "s":
        data = await tmdb_search(payload, page)
    elif kind == "g":
        data = await tmdb_discover_by_genre(int(payload), page)
    elif kind == "c":
        data = await tmdb_discover_by_country(payload, page)
    else:
        await edit_target.edit_message_text("Неизвестный запрос.")
        return

    results = data.get("results", []) or []
    total_pages = max(1, int(data.get("total_pages", 1)))

    if not results:
        await edit_target.edit_message_text("Ничего не найдено.")
        return

    kb_rows = [list(r) for r in movies_to_keyboard(results).inline_keyboard]

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("◀ Пред", callback_data=page_nav_cb(kind, page - 1, payload)))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("▶ След", callback_data=page_nav_cb(kind, page + 1, payload)))
    elif len(results) == 20:
        nav_row.append(InlineKeyboardButton("▶ След", callback_data=page_nav_cb(kind, page + 1, payload)))
    if nav_row:
        kb_rows.append(nav_row)

    header = f"{title_line}\n_(страница {page} из {total_pages})_"
    await edit_target.edit_message_text(
        header,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )

async def show_details(qmsg, movie_id: int):
    det = await tmdb_details(movie_id)
    if not det:
        await qmsg.edit_message_text("Не удалось получить детали.")
        return

    title = det.get("title") or det.get("original_title") or "Untitled"
    year = (det.get("release_date") or "—")[:4]
    rating = det.get("vote_average") or "—"
    genres = ", ".join([g["name"] for g in det.get("genres", [])]) or "—"
    overview = det.get("overview") or "—"
    poster_path = det.get("poster_path")
    caption = (
        f"*{title}* ({year})\n"
        f"⭐ TMDb: *{rating}*\n"
        f"🎭 Genres: _{genres}_\n\n"
        f"{overview}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ В избранное", callback_data=f"fav_add:{movie_id}")],
        [InlineKeyboardButton("🟢 Где смотреть?", callback_data=f"watch:{movie_id}")],
        [InlineKeyboardButton("▶ Трейлер", callback_data=f"trailer:{movie_id}")],
        [InlineKeyboardButton("🗂 Мои избранные", callback_data="fav_list")]
    ])

    if poster_path:
        try:
            await qmsg.edit_message_media(
                media=InputMediaPhoto(media=f"{IMG_BASE}{poster_path}", caption=caption, parse_mode=ParseMode.MARKDOWN)
            )
            await qmsg.edit_message_reply_markup(reply_markup=kb)
            return
        except Exception:
            pass

    await qmsg.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ===== Кнопки: Где смотреть / Трейлер =====
async def on_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    movie_id = int(q.data.split(":", 1)[1])
    data = await tmdb_watch_providers(movie_id)

    country_code = (LANG.split("-")[-1] if "-" in LANG else "US").upper()
    entry = data.get(country_code) or data.get("US") or data.get("GB")

    if not entry:
        await q.message.reply_text("Для твоего региона провайдеры не найдены.")
        return

    lines = [f"Где смотреть ({country_code}):"]
    for kind in ["flatrate", "rent", "buy", "ads", "free"]:
        provs = entry.get(kind) or []
        if provs:
            names = ", ".join(p.get("provider_name", "?") for p in provs)
            label = {"flatrate": "Подписка", "rent": "Аренда", "buy": "Покупка", "ads": "С рекламой", "free": "Бесплатно"}[kind]
            lines.append(f"• {label}: {names}")

    link = entry.get("link")
    if link:
        lines.append(f"\nСписок провайдеров: {link}")

    await q.message.reply_text("\n".join(lines))

async def on_trailer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    movie_id = int(q.data.split(":", 1)[1])
    vids = await tmdb_videos(movie_id)
    yt = next((v for v in vids if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser")), None)
    if yt:
        url = f"https://www.youtube.com/watch?v={yt['key']}"
        await q.message.reply_text(f"▶ {yt.get('name', 'Trailer')}\n{url}")
    else:
        await q.message.reply_text("Трейлер не найден.")

# ===== Избранное (SQLite) =====
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS favorites (
  user_id    INTEGER NOT NULL,
  movie_id   INTEGER NOT NULL,
  title      TEXT    NOT NULL,
  year       TEXT    NOT NULL,
  PRIMARY KEY (user_id, movie_id)
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_SQL)
        await db.commit()

async def fav_add(user_id: int, movie_id: int, title: str, year: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO favorites(user_id, movie_id, title, year) VALUES (?, ?, ?, ?)",
                (user_id, movie_id, title, year)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def fav_list(user_id: int) -> List[Tuple[int, str]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT movie_id, title FROM favorites WHERE user_id = ? ORDER BY title",
            (user_id,)
        )
        rows = await cur.fetchall()
    return [(r[0], r[1]) for r in rows]

async def fav_remove(user_id: int, movie_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM favorites WHERE user_id = ? AND movie_id = ?",
            (user_id, movie_id)
        )
        await db.commit()
        return cur.rowcount

# ===== Handlers =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tmdb_genres()  # прогреем кэш
    await update.message.reply_text(
        "Привет! Я бот-поисковик фильмов TMDb. Пиши название или выбери кнопку.",
        reply_markup=MAIN_KEYBOARD
    )

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    if text == "🔎 Поиск":
        await update.message.reply_text("Введи название фильма:", reply_markup=MAIN_KEYBOARD)
        return

    if text == "🎭 Жанр":
        genres = await tmdb_genres()
        rows, row = [], []
        for gid, gname in list(genres.items())[:30]:
            row.append(InlineKeyboardButton(gname, callback_data=f"genre:{gid}:1"))
            if len(row) == 3:
                rows.append(row); row = []
        if row: rows.append(row)
        await update.message.reply_text("Выбери жанр:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if text == "🌍 Страна":
        rows, row = [], []
        for code, label in COUNTRIES:
            row.append(InlineKeyboardButton(label, callback_data=f"country:{code}:1"))
            if len(row) == 2:
                rows.append(row); row = []
        if row: rows.append(row)
        await update.message.reply_text("Выбери страну:", reply_markup=InlineKeyboardMarkup(rows))
        return

    # Иначе — это поисковый запрос
    msg = await update.message.reply_text("🔎 Ищу…")
    data = await tmdb_search(text, page=1)
    results = data.get("results", [])
    if not results:
        await msg.edit_text("Ничего не нашёл. Попробуй другое название.")
        return

    kb_rows = [list(r) for r in movies_to_keyboard(results).inline_keyboard]
    total_pages = max(1, int(data.get("total_pages", 1)))
    if total_pages > 1 or len(results) == 20:
        kb_rows.append([InlineKeyboardButton("▶ След", callback_data=page_nav_cb("s", 2, text))])

    await msg.edit_text(
        f"Результаты по: *{text}*\n_(страница 1 из {total_pages})_",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )

async def on_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    movie_id = int(q.data.split(":", 1)[1])
    await show_details(q, movie_id)

async def on_genre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, gid, page = q.data.split(":", 2)
    gid, page = int(gid), int(page)
    await q.edit_message_text(f"🎭 Жанр: *{(await tmdb_genres())[gid]}*", parse_mode=ParseMode.MARKDOWN)
    await show_list_with_pagination(q, "g", page, str(gid), f"🎭 Жанр: *{(await tmdb_genres())[gid]}*")

async def on_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, code, page = q.data.split(":", 2)
    page = int(page)
    await q.edit_message_text(f"🌍 Страна: *{code}*", parse_mode=ParseMode.MARKDOWN)
    await show_list_with_pagination(q, "c", page, code, f"🌍 Страна: *{code}*")

async def on_page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    kind, page, payload = parse_page_cb(q.data)  # kind: 's' | 'g' | 'c'
    if kind == "s":
        title_line = f"🔎 Поиск: *{payload}*"
    elif kind == "g":
        try:
            gid = int(payload)
        except ValueError:
            gid = None
        if gid is not None:
            gname = (await tmdb_genres()).get(gid, str(gid))
        else:
            gname = payload
        title_line = f"🎭 Жанр: *{gname}*"
    elif kind == "c":
        title_line = f"🌍 Страна: *{payload}*"
    else:
        await q.message.reply_text("Неизвестный тип страницы.")
        return

    await show_list_with_pagination(q, kind, page, payload, title_line)

# Избранное
async def on_fav_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    movie_id = int(q.data.split(":", 1)[1])
    det = await tmdb_details(movie_id)
    if not det:
        await q.message.reply_text("Не удалось получить данные фильма.")
        return
    title = det.get("title") or det.get("original_title") or "Untitled"
    year = (det.get("release_date") or "—")[:4]
    ok = await fav_add(q.from_user.id, movie_id, title, year)
    if ok:
        await q.message.reply_text(f"✔ Добавлено в избранное: {title}")
    else:
        await q.message.reply_text("Уже в избранном.")

async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_favorites(update.effective_user.id, update.effective_chat.id, context)

async def on_fav_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await send_favorites(q.from_user.id, q.message.chat.id, context)

async def on_fav_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    movie_id = int(q.data.split(":", 1)[1])
    n = await fav_remove(q.from_user.id, movie_id)
    if n:
        await q.edit_message_text("Удалено. Открой /favorites снова, чтобы обновить список.")
    else:
        await q.answer("Не найдено в избранном.", show_alert=False)

async def send_favorites(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    rows = await fav_list(user_id)
    if not rows:
        await context.bot.send_message(chat_id, "Пока пусто. Нажимай ⭐ под фильмом, чтобы добавить.")
        return
    buttons = []
    for movie_id, title in rows[:20]:
        buttons.append([
            InlineKeyboardButton(f"ℹ {title}", callback_data=f"det:{movie_id}"),
            InlineKeyboardButton("✖", callback_data=f"fav_del:{movie_id}")
        ])
    await context.bot.send_message(chat_id, "🗂 Твои избранные:", reply_markup=InlineKeyboardMarkup(buttons))

# ===== App =====
async def _post_init(app: Application):
    await init_db()
    await tmdb_genres()

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    # «как браузер»: любой текст — это действие
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # поддержим /start и /favorites
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("favorites", cmd_favorites))

    # карточки/каталоги/пагинация
    app.add_handler(CallbackQueryHandler(on_details, pattern=r"^det:"))
    app.add_handler(CallbackQueryHandler(on_genre, pattern=r"^genre:"))
    app.add_handler(CallbackQueryHandler(on_country, pattern=r"^country:"))
    app.add_handler(CallbackQueryHandler(on_page_nav, pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(on_watch, pattern=r"^watch:"))
    app.add_handler(CallbackQueryHandler(on_trailer, pattern=r"^trailer:"))
    app.add_handler(CallbackQueryHandler(on_fav_add, pattern=r"^fav_add:"))
    app.add_handler(CallbackQueryHandler(on_fav_list, pattern=r"^fav_list$"))
    app.add_handler(CallbackQueryHandler(on_fav_del, pattern=r"^fav_del:"))

    print("Bot is running… Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
