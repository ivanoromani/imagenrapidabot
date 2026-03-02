import os
import re
import time
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import asyncpg
from PIL import Image, ImageOps
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_USERNAME = "@imagenrapidabot"

# ENV
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID")   # -100...
ADMIN_ID = os.getenv("ADMIN_ID")         # numeric string

if not BOT_TOKEN:
    raise RuntimeError("Falta la variable de entorno BOT_TOKEN (ponla en Railway).")
if not DATABASE_URL:
    raise RuntimeError("Falta la variable DATABASE_URL (añade Postgres en Railway).")

ADMIN_ID_INT = int(ADMIN_ID) if ADMIN_ID and ADMIN_ID.isdigit() else None
LOG_CHAT_ID_INT = int(LOG_CHAT_ID) if LOG_CHAT_ID and re.match(r"^-?\d+$", LOG_CHAT_ID) else None

# -----------------------------
# Anti-spam simple (rate limit)
# -----------------------------
MAX_REQ = 8
WINDOW_SEC = 60
_user_hits: Dict[int, List[float]] = {}

def rate_limited(user_id: int) -> bool:
    now = time.time()
    hits = _user_hits.get(user_id, [])
    hits = [t for t in hits if now - t < WINDOW_SEC]
    if len(hits) >= MAX_REQ:
        _user_hits[user_id] = hits
        return True
    hits.append(now)
    _user_hits[user_id] = hits
    return False


# -----------------------------
# Store last image per user
# -----------------------------
_last_image: Dict[int, Tuple[str, str]] = {}

HELP_TEXT = (
    "🖼️ *Imagen Rápida – Convertidor*\n\n"
    "✅ Envíame una foto (o imagen como archivo) y luego usa:\n"
    "• /webp  → convierte a WebP\n"
    "• /jpg   → convierte a JPG\n"
    "• /png   → convierte a PNG\n"
    "• /compress 70  → JPG con calidad 70 (1–95)\n"
    "• /resize 1024  → redimensiona (ancho máx.)\n"
    "• /strip → quita metadatos (EXIF)\n\n"
    "📌 Tip: puedes responder a una imagen con el comando, o enviar el comando después (usa la última imagen).\n"
)


# -----------------------------
# DB helpers
# -----------------------------
POOL: Optional[asyncpg.Pool] = None

CREATE_SQL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  user_id BIGINT NOT NULL,
  username TEXT,
  command TEXT NOT NULL
);
"""

CREATE_SQL_USERS = """
CREATE TABLE IF NOT EXISTS users (
  user_id BIGINT PRIMARY KEY,
  username TEXT,
  first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

async def db_init() -> None:
    global POOL
    POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with POOL.acquire() as conn:
        await conn.execute(CREATE_SQL_USERS)
        await conn.execute(CREATE_SQL_EVENTS)

async def db_record_event(user_id: int, username: str, command: str) -> Tuple[bool, int]:
    """
    Salva evento e crea utente se nuovo.
    Ritorna (is_new_user, total_events).
    """
    assert POOL is not None
    is_new = False

    async with POOL.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id FROM users WHERE user_id=$1", user_id)
        if row is None:
            is_new = True
            await conn.execute(
                "INSERT INTO users(user_id, username) VALUES($1,$2) ON CONFLICT (user_id) DO NOTHING",
                user_id, username
            )
        else:
            await conn.execute("UPDATE users SET username=$2 WHERE user_id=$1", user_id, username)

        await conn.execute(
            "INSERT INTO events(user_id, username, command) VALUES($1,$2,$3)",
            user_id, username, command
        )

        total = await conn.fetchval("SELECT COUNT(*) FROM events")
        return is_new, int(total)

async def db_stats() -> dict:
    assert POOL is not None
    async with POOL.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        total_events = await conn.fetchval("SELECT COUNT(*) FROM events")

        users_24h = await conn.fetchval(
            "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts > NOW() - INTERVAL '24 hours'"
        )
        users_7d = await conn.fetchval(
            "SELECT COUNT(DISTINCT user_id) FROM events WHERE ts > NOW() - INTERVAL '7 days'"
        )

        top_cmds = await conn.fetch(
            "SELECT command, COUNT(*) AS c "
            "FROM events WHERE ts > NOW() - INTERVAL '7 days' "
            "GROUP BY command ORDER BY c DESC LIMIT 8"
        )

    return {
        "total_users": int(total_users or 0),
        "total_events": int(total_events or 0),
        "users_24h": int(users_24h or 0),
        "users_7d": int(users_7d or 0),
        "top_cmds": [(r["command"], int(r["c"])) for r in top_cmds],
    }

async def notify_log(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if LOG_CHAT_ID_INT:
        try:
            await context.bot.send_message(chat_id=LOG_CHAT_ID_INT, text=text)
        except Exception:
            pass


# -----------------------------
# Image helpers
# -----------------------------
def _safe_filename(base: str, ext: str) -> str:
    base = (base or "imagen").strip()
    base = "".join(c for c in base if c.isalnum() or c in ("-", "_"))[:40] or "imagen"
    return f"{base}.{ext}"

async def _download_image_bytes(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    data = await tg_file.download_as_bytearray()
    return bytes(data)

def _load_image(img_bytes: bytes) -> Image.Image:
    im = Image.open(BytesIO(img_bytes))
    im = ImageOps.exif_transpose(im)
    return im

def _strip_exif(im: Image.Image) -> Image.Image:
    data = list(im.getdata())
    clean = Image.new(im.mode, im.size)
    clean.putdata(data)
    return clean

def _save_as(im: Image.Image, fmt: str, quality: Optional[int] = None) -> bytes:
    out = BytesIO()
    save_kwargs = {}
    fmt_up = fmt.upper()

    # JPG needs RGB
    if fmt_up in ("JPG", "JPEG"):
        if im.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        if quality is not None:
            save_kwargs["quality"] = max(1, min(95, int(quality)))
        save_kwargs["optimize"] = True
        save_kwargs["progressive"] = True

    if fmt_up == "WEBP":
        save_kwargs["quality"] = 85 if quality is None else max(1, min(95, int(quality)))
        save_kwargs["method"] = 6

    im.save(out, format=fmt_up, **save_kwargs)
    out.seek(0)
    return out.getvalue()

def _resize_max_width(im: Image.Image, max_w: int) -> Image.Image:
    max_w = int(max_w)
    if max_w <= 0:
        return im
    w, h = im.size
    if w <= max_w:
        return im
    new_h = int((max_w / w) * h)
    return im.resize((max_w, new_h), Image.LANCZOS)

def _get_target_image_file_id(update: Update) -> Optional[Tuple[str, str]]:
    msg = update.message
    if not msg:
        return None

    if msg.photo:
        file_id = msg.photo[-1].file_id
        return (file_id, "foto")

    if msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        file_id = msg.document.file_id
        name = (msg.document.file_name or "imagen").rsplit(".", 1)[0]
        return (file_id, name)

    return None

def _get_from_reply_or_last(update: Update) -> Optional[Tuple[str, str]]:
    msg = update.message
    if not msg:
        return None

    if msg.reply_to_message:
        temp = Update(update.update_id, message=msg.reply_to_message)
        reply_img = _get_target_image_file_id(temp)
        if reply_img:
            return reply_img

    uid = update.effective_user.id if update.effective_user else 0
    return _last_image.get(uid)


# -----------------------------
# Tracking
# -----------------------------
async def track(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    uname = update.effective_user.username if update.effective_user and update.effective_user.username else ""
    if uid == 0:
        return

    is_new, total_events = await db_record_event(uid, uname, command)

    if is_new:
        await notify_log(context, f"🆕 Nuevo usuario: {uid} @{uname or '—'}")

    # every 50 conversions/events total (you can tune this)
    if command.startswith("convert_") and total_events % 50 == 0:
        await notify_log(context, f"🎉 {total_events} acciones totales. Último: {command}")


# -----------------------------
# Commands
# -----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_markdown(HELP_TEXT)
        await track(update, context, "start")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_markdown(HELP_TEXT)
        await track(update, context, "help")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    uid = update.effective_user.id if update.effective_user else 0
    if not ADMIN_ID_INT or uid != ADMIN_ID_INT:
        await update.message.reply_text("❌ No autorizado.")
        return

    s = await db_stats()
    top = "\n".join([f"• {cmd}: {c}" for cmd, c in s["top_cmds"]]) or "—"
    msg = (
        "📊 *Stats (7 días)*\n\n"
        f"👥 Usuarios totales: {s['total_users']}\n"
        f"⚙️ Acciones totales: {s['total_events']}\n"
        f"🕒 Usuarios únicos 24h: {s['users_24h']}\n"
        f"📅 Usuarios únicos 7d: {s['users_7d']}\n\n"
        f"🏷️ Top comandos (7d):\n{top}"
    )
    await update.message.reply_markdown(msg)

async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    uid = update.effective_user.id if update.effective_user else 0
    if uid and rate_limited(uid):
        await update.message.reply_text("⏳ Demasiadas peticiones. Espera un minuto y prueba otra vez.")
        return

    found = _get_target_image_file_id(update)
    if not found:
        return

    file_id, base_name = found
    _last_image[uid] = (file_id, base_name)

    await track(update, context, "image_received")

    await update.message.reply_text(
        "✅ Imagen recibida.\n\n"
        "Ahora usa:\n"
        "• /webp  • /jpg  • /png\n"
        "• /compress 70\n"
        "• /resize 1024\n"
        "• /strip\n\n"
        f"Hecho con {BOT_USERNAME}"
    )

async def _convert_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    out_ext: str,
    out_format: str,
    track_name: str,
    quality: Optional[int] = None,
    resize_w: Optional[int] = None,
    strip: bool = False,
) -> None:
    if not update.message:
        return

    uid = update.effective_user.id if update.effective_user else 0
    if uid and rate_limited(uid):
        await update.message.reply_text("⏳ Demasiadas peticiones. Espera un minuto y prueba otra vez.")
        return

    src = _get_from_reply_or_last(update)
    if not src:
        await update.message.reply_text(
            "Primero envíame una imagen (foto o archivo), o responde a una imagen con el comando."
        )
        return

    file_id, base_name = src
    await update.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

    try:
        img_bytes = await _download_image_bytes(context, file_id)
        im = _load_image(img_bytes)

        if strip:
            im = _strip_exif(im)
        if resize_w is not None:
            im = _resize_max_width(im, resize_w)

        out_bytes = _save_as(im, out_format, quality=quality)
        filename = _safe_filename(base_name, out_ext)

        bio = BytesIO(out_bytes)
        bio.name = filename
        bio.seek(0)

        caption_parts = [f"✅ Listo: {filename}"]
        if resize_w is not None:
            caption_parts.append(f"📐 Resize: {resize_w}px ancho máx.")
        if quality is not None and out_format.upper() in ("JPG", "JPEG", "WEBP"):
            caption_parts.append(f"🗜️ Calidad: {quality}")
        if strip:
            caption_parts.append("🧼 Metadatos: eliminados")
        caption_parts.append(f"Hecho con {BOT_USERNAME}")

        await update.message.reply_document(document=bio, caption="\n".join(caption_parts))
        await track(update, context, track_name)

    except Exception as e:
        await update.message.reply_text(f"❌ Error procesando la imagen: {e}")
        await notify_log(context, f"⚠️ Error: {e}")

async def webp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _convert_and_send(update, context, "webp", "WEBP", "convert_webp", quality=85)

async def jpg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _convert_and_send(update, context, "jpg", "JPEG", "convert_jpg", quality=85)

async def png_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _convert_and_send(update, context, "png", "PNG", "convert_png")

async def compress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = 70
    if context.args:
        try:
            q = int(context.args[0])
        except ValueError:
            q = 70
    q = max(1, min(95, q))
    await _convert_and_send(update, context, "jpg", "JPEG", "convert_compress", quality=q)

async def resize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: /resize 1024  (ancho máximo en px)")
        return
    try:
        w = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Valor inválido. Ej: /resize 1024")
        return
    w = max(64, min(8000, w))
    await _convert_and_send(update, context, "jpg", "JPEG", "convert_resize", quality=85, resize_w=w)

async def strip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _convert_and_send(update, context, "jpg", "JPEG", "convert_strip", quality=85, strip=True)


# -----------------------------
# App init
# -----------------------------
async def post_init(app: Application) -> None:
    await db_init()

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    app.add_handler(CommandHandler("webp", webp_cmd))
    app.add_handler(CommandHandler("jpg", jpg_cmd))
    app.add_handler(CommandHandler("png", png_cmd))
    app.add_handler(CommandHandler("compress", compress_cmd))
    app.add_handler(CommandHandler("resize", resize_cmd))
    app.add_handler(CommandHandler("strip", strip_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, on_image))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_image))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()