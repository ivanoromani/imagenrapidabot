import os
import time
from io import BytesIO
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageOps
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_USERNAME = "@imagenrapidabot"

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
# user_id -> (file_id, original_name)
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
    "📌 Tip: puedes *responder* a una imagen con el comando, o enviar el comando después (usa la última imagen).\n"
)


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
    # Corrige orientación EXIF si existe
    im = ImageOps.exif_transpose(im)
    return im


def _strip_exif(im: Image.Image) -> Image.Image:
    # Crear copia "limpia" sin info extra
    data = list(im.getdata())
    clean = Image.new(im.mode, im.size)
    clean.putdata(data)
    return clean


def _save_as(im: Image.Image, fmt: str, quality: Optional[int] = None) -> bytes:
    out = BytesIO()
    save_kwargs = {}

    fmt_up = fmt.upper()

    # Para JPG hay que garantizar RGB (si viene con alpha)
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
        # WebP soporta alpha
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
    """
    Devuelve (file_id, original_name) si en el mensaje hay imagen (photo o document imagen).
    """
    msg = update.message
    if not msg:
        return None

    # Photo (Telegram envía varias resoluciones; elegimos la mayor)
    if msg.photo:
        file_id = msg.photo[-1].file_id
        return (file_id, "foto")

    # Document (si envían como archivo)
    if msg.document and msg.document.mime_type and msg.document.mime_type.startswith("image/"):
        file_id = msg.document.file_id
        name = (msg.document.file_name or "imagen").rsplit(".", 1)[0]
        return (file_id, name)

    return None


def _get_from_reply_or_last(update: Update) -> Optional[Tuple[str, str]]:
    """
    1) Si el comando responde a una imagen, usa esa.
    2) Si no, usa la última imagen guardada para ese usuario.
    """
    msg = update.message
    if not msg:
        return None

    # 1) Reply-to
    if msg.reply_to_message:
        temp = Update(update.update_id, message=msg.reply_to_message)
        reply_img = _get_target_image_file_id(temp)
        if reply_img:
            return reply_img

    # 2) Last saved
    uid = update.effective_user.id if update.effective_user else 0
    return _last_image.get(uid)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_markdown(HELP_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_markdown(HELP_TEXT)


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

    await update.message.reply_text(
        "✅ Imagen recibida.\n\n"
        "Ahora usa:\n"
        "• /webp  • /jpg  • /png\n"
        "• /compress 70\n"
        "• /resize 1024\n"
        "• /strip\n\n"
        f"Tip: también puedes responder a una imagen con el comando.\nHecho con {BOT_USERNAME}"
    )


async def _convert_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    out_ext: str,
    out_format: str,
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
        await update.message.reply_text("Primero envíame una imagen (foto o archivo), o responde a una imagen con el comando.")
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
    except Exception as e:
        await update.message.reply_text(f"❌ Error procesando la imagen: {e}")


async def webp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _convert_and_send(update, context, out_ext="webp", out_format="WEBP", quality=85)


async def jpg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _convert_and_send(update, context, out_ext="jpg", out_format="JPEG", quality=85)


async def png_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _convert_and_send(update, context, out_ext="png", out_format="PNG")


async def compress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /compress 70 (JPG)
    q = 70
    if context.args:
        try:
            q = int(context.args[0])
        except ValueError:
            q = 70
    q = max(1, min(95, q))
    await _convert_and_send(update, context, out_ext="jpg", out_format="JPEG", quality=q)


async def resize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # /resize 1024
    if not context.args:
        await update.message.reply_text("Uso: /resize 1024  (ancho máximo en px)")
        return
    try:
        w = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Valor inválido. Ej: /resize 1024")
        return
    w = max(64, min(8000, w))
    # mantenemos formato original como JPG por defecto (práctico), pero puedes cambiarlo a WEBP si prefieres
    await _convert_and_send(update, context, out_ext="jpg", out_format="JPEG", quality=85, resize_w=w)


async def strip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Quita EXIF y devuelve JPG por defecto
    await _convert_and_send(update, context, out_ext="jpg", out_format="JPEG", quality=85, strip=True)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Falta la variable de entorno BOT_TOKEN (ponla en Railway).")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("webp", webp_cmd))
    app.add_handler(CommandHandler("jpg", jpg_cmd))
    app.add_handler(CommandHandler("png", png_cmd))
    app.add_handler(CommandHandler("compress", compress_cmd))
    app.add_handler(CommandHandler("resize", resize_cmd))
    app.add_handler(CommandHandler("strip", strip_cmd))

    # Recibir imágenes (foto o documento imagen)
    app.add_handler(MessageHandler(filters.PHOTO, on_image))
    app.add_handler(MessageHandler(filters.Document.IMAGE, on_image))

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()