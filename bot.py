import os
import logging
import sqlite3
import subprocess
import asyncio
from datetime import date, timedelta
from telegram import (
    Update,
    LabeledPrice,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from groq import Groq

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
groq_client = Groq(api_key=GROQ_API_KEY)

FREE_DAILY_LIMIT = 1       # 1 video por día gratis
FREE_CLIPS_PER_VIDEO = 3   # 3 clips por video gratis
STARS_PRICE = 499
REFERRAL_COMMISSION = 99   # 20% de 499 ~ 99 Stars
DOWNLOAD_DIR = "/tmp/clipviral"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
DB_PATH = "clipviral.db"

SUPPORTED_DOMAINS = (
    "youtube.com", "youtu.be",
    "kick.com",
    "twitch.tv",
    "tiktok.com",
)

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            videos_today   INTEGER DEFAULT 0,
            last_date      TEXT    DEFAULT '',
            premium_until  TEXT    DEFAULT '',
            referred_by    INTEGER DEFAULT NULL,
            stars_earned   INTEGER DEFAULT 0,
            referral_count INTEGER DEFAULT 0
        )
    """)
    con.commit()
    con.close()

def get_user(user_id: int) -> dict:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT videos_today, last_date, premium_until, referred_by, stars_earned, referral_count
        FROM users WHERE user_id=?
    """, (user_id,))
    row = cur.fetchone()
    con.close()
    if row is None:
        return {"videos_today": 0, "last_date": "", "premium_until": "", "referred_by": None, "stars_earned": 0, "referral_count": 0}
    return {"videos_today": row[0], "last_date": row[1], "premium_until": row[2], "referred_by": row[3], "stars_earned": row[4], "referral_count": row[5]}

def register_user(user_id: int, referred_by: int = None):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, referred_by) VALUES (?, ?)", (user_id, referred_by))
    con.commit()
    con.close()

def record_video(user_id: int):
    user = get_user(user_id)
    today = str(date.today())
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if user["last_date"] != today:
        cur.execute("""
            INSERT INTO users (user_id, videos_today, last_date)
            VALUES (?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET videos_today=1, last_date=excluded.last_date
        """, (user_id, today))
    else:
        cur.execute("""
            UPDATE users SET videos_today = videos_today + 1 WHERE user_id = ?
        """, (user_id,))
    con.commit()
    con.close()

def set_premium(user_id: int, until: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO users (user_id, premium_until)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET premium_until=excluded.premium_until
    """, (user_id, until))
    con.commit()
    con.close()

def add_commission(referrer_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        UPDATE users
        SET stars_earned = stars_earned + ?,
            referral_count = referral_count + 1
        WHERE user_id = ?
    """, (REFERRAL_COMMISSION, referrer_id))
    con.commit()
    con.close()

def is_premium(user: dict) -> bool:
    pu = user.get("premium_until", "")
    if not pu:
        return False
    try:
        return date.fromisoformat(pu) >= date.today()
    except Exception:
        return False

def can_process(user_id: int) -> tuple[bool, int]:
    user = get_user(user_id)
    today = str(date.today())
    if is_premium(user):
        return True, 9999
    if user["last_date"] != today:
        return True, FREE_DAILY_LIMIT
    remaining = FREE_DAILY_LIMIT - user["videos_today"]
    return remaining > 0, max(remaining, 0)

def is_supported_url(text: str) -> bool:
    return any(d in text.lower() for d in SUPPORTED_DOMAINS)

# ── IA: Detectar momentos virales ─────────────────────────────────────────────
async def get_viral_moments(transcript: str, duration: int, clip_duration: int) -> list[dict]:
    prompt = f"""
Eres un experto en contenido viral para redes sociales (TikTok, YouTube Shorts, Instagram Reels).

Analiza esta transcripción de un video de {duration} segundos de duración y encuentra los 3 mejores momentos virales.

TRANSCRIPCIÓN:
{transcript[:4000]}

Para cada momento viral devuelve EXACTAMENTE este formato JSON (sin texto extra, solo el JSON):
[
  {{
    "start": 120,
    "end": 150,
    "title": "Título gancho del clip",
    "description": "Descripción viral con potencial de engagement para redes sociales (máximo 150 caracteres)",
    "hashtags": "#hashtag1 #hashtag2 #hashtag3 #hashtag4 #hashtag5"
  }},
  ...
]

REGLAS:
- start y end deben ser segundos exactos dentro del video
- Cada clip debe durar aproximadamente {clip_duration} segundos
- Busca momentos con: revelaciones, emociones fuertes, humor, consejos, controversia
- Los hashtags deben ser relevantes y trending
- La descripción debe generar curiosidad y clicks
- Responde SOLO el JSON, sin explicaciones
"""
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
        )
        import json
        text = response.choices[0].message.content.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        logger.error("Groq error: %s", e)
        return []

# ── Descarga y transcripción ──────────────────────────────────────────────────
def download_audio(url: str, user_id: int) -> tuple[str | None, int]:
    """Descarga solo el audio del video y retorna (path, duracion_segundos)"""
    out_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_audio.%(ext)s")
    try:
        import yt_dlp
        ydl_opts = {
            "outtmpl": out_path,
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = info.get("duration", 0)
            audio_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_audio.mp3")
            return audio_path, duration
    except Exception as e:
        logger.error("Download error: %s", e)
        return None, 0

def download_video_segment(url: str, user_id: int, start: int, end: int, clip_num: int) -> str | None:
    """Descarga un segmento específico del video"""
    out_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_clip{clip_num}.mp4")
    try:
        import yt_dlp
        ydl_opts = {
            "outtmpl": os.path.join(DOWNLOAD_DIR, f"{user_id}_full.%(ext)s"),
            "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "download_ranges": lambda info, *args: [{"start_time": start, "end_time": end}],
            "force_keyframes_at_cuts": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        full_path = os.path.join(DOWNLOAD_DIR, f"{user_id}_full.mp4")
        if os.path.exists(full_path):
            subprocess.run([
                "ffmpeg", "-y",
                "-i", full_path,
                "-ss", str(start),
                "-to", str(end),
                "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264", "-c:a", "aac",
                "-preset", "fast",
                out_path
            ], capture_output=True)
            try:
                os.remove(full_path)
            except Exception:
                pass
            return out_path if os.path.exists(out_path) else None
    except Exception as e:
        logger.error("Segment download error: %s", e)
        return None

def transcribe_audio(audio_path: str) -> str:
    """Transcribe el audio usando Whisper via Groq"""
    try:
        with open(audio_path, "rb") as f:
            transcription = groq_client.audio.transcriptions.create(
                file=(os.path.basename(audio_path), f),
                model="whisper-large-v3",
                language="es",
                response_format="verbose_json",
            )
        return transcription.text
    except Exception as e:
        logger.error("Transcription error: %s", e)
        return ""

def cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def get_ref_link(bot_username: str, uid: int) -> str:
    return f"https://t.me/{bot_username}?start=REF_{uid}"

def get_premium_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⭐ Comprar Premium — {STARS_PRICE} Stars/mes", callback_data="buy_premium")
    ]])

# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id

    referred_by = None
    if ctx.args:
        try:
            ref_id = int(ctx.args[0].replace("REF_", ""))
            if ref_id != uid:
                referred_by = ref_id
        except Exception:
            pass

    register_user(uid, referred_by)
    bot_username = (await ctx.bot.get_me()).username
    ref_link = get_ref_link(bot_username, uid)

    text = (
        f"✂️ *¡Bienvenido a ClipViral Bot!*\n\n"
        f"Hola *{user.first_name}* 👋\n\n"
        "Soy tu asistente de contenido viral. Analizo tus videos largos de "
        "*YouTube, Twitch, Kick y TikTok* y genero automáticamente los mejores "
        "clips con mayor potencial viral. 🔥\n\n"
        "📋 *¿Cómo funciono?*\n"
        "1️⃣ Envíame el link de un video largo\n"
        "2️⃣ Elige la duración del clip (20, 30 o 40 segundos)\n"
        "3️⃣ Recibe tus clips con descripción y hashtags listos\n\n"
        f"🎁 *Plan Gratuito:*\n"
        f"• {FREE_DAILY_LIMIT} video por día\n"
        f"• {FREE_CLIPS_PER_VIDEO} clips por video\n\n"
        f"⭐ *Premium — {STARS_PRICE} Stars/mes:*\n"
        "• Videos ilimitados todos los días\n"
        "• Clips ilimitados por video\n"
        "• Prioridad en procesamiento\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💰 *¡GANA STARS GRATIS!*\n\n"
        f"Por cada amigo que compre Premium ganas *{REFERRAL_COMMISSION} Stars* automáticamente.\n\n"
        f"🔗 Tu link de referido:\n`{ref_link}`\n\n"
        f"💡 *Ejemplo:* 5 amigos = *{5 * REFERRAL_COMMISSION} Stars* (~Premium gratis)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📌 *Comandos:*\n"
        "/status — tus estadísticas\n"
        "/premium — obtener acceso ilimitado\n"
        "/referido — tu link para ganar Stars\n"
        "/help — ayuda"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start(update, ctx)

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid)
    user = get_user(uid)
    today = str(date.today())

    if is_premium(user):
        msg = (
            f"⭐ *Eres Premium* hasta `{user['premium_until']}`\n"
            f"Videos: *ilimitados* 🎉\n"
            f"Clips por video: *ilimitados* 🎉\n\n"
            f"💰 Stars ganadas por referidos: *{user['stars_earned']} ⭐*\n"
            f"👥 Amigos referidos: *{user['referral_count']}*"
        )
    else:
        used = user["videos_today"] if user["last_date"] == today else 0
        remaining = FREE_DAILY_LIMIT - used
        msg = (
            f"📊 *Tu estado hoy:*\n"
            f"Videos procesados: {used}/{FREE_DAILY_LIMIT}\n"
            f"Videos restantes: *{remaining}*\n\n"
            f"💰 Stars ganadas: *{user['stars_earned']} ⭐*\n"
            f"👥 Amigos referidos: *{user['referral_count']}*\n\n"
            f"¿Quieres videos ilimitados? /premium"
        )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def referido_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    register_user(uid)
    user = get_user(uid)
    bot_username = (await ctx.bot.get_me()).username
    ref_link = get_ref_link(bot_username, uid)

    msg = (
        "💰 *Tu link de referido:*\n\n"
        f"`{ref_link}`\n\n"
        "Comparte este link. Cuando alguien compre Premium:\n"
        f"✅ Tú ganas *{REFERRAL_COMMISSION} Stars* automáticamente\n"
        f"✅ Ellos obtienen clips ilimitados\n\n"
        f"📊 *Tu historial:*\n"
        f"👥 Amigos referidos: *{user['referral_count']}*\n"
        f"⭐ Stars ganadas: *{user['stars_earned']}*\n\n"
        f"💡 *Ejemplo:* 5 amigos = *{5 * REFERRAL_COMMISSION} Stars*"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def premium_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⭐ *Premium — Videos y clips ilimitados*\n\n"
        f"Precio: *{STARS_PRICE} Telegram Stars/mes* (~$5 USD)\n\n"
        "✅ Videos ilimitados por día\n"
        "✅ Clips ilimitados por video\n"
        "✅ Procesamiento prioritario\n"
        "✅ Hashtags y descripciones virales\n\n"
        f"💰 ¿Sin Stars? Usa /referido para ganarlas gratis.\n\n"
        "👇 Presiona para pagar:",
        parse_mode="Markdown",
        reply_markup=get_premium_keyboard(),
    )

async def buy_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await ctx.bot.send_invoice(
        chat_id=query.from_user.id,
        title="ClipViral Bot Premium",
        description="Videos y clips ilimitados por 30 días",
        payload="premium_30d",
        currency="XTR",
        prices=[LabeledPrice("Premium 30 días", STARS_PRICE)],
        provider_token="",
    )

async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload == "premium_30d":
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Pago no reconocido.")

async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    until = str(date.today() + timedelta(days=30))
    set_premium(uid, until)

    user = get_user(uid)
    referrer_msg = ""
    if user.get("referred_by"):
        referrer_id = user["referred_by"]
        add_commission(referrer_id)
        referrer_msg = f"\n\n💰 ¡Tu amigo que te invitó acaba de ganar *{REFERRAL_COMMISSION} Stars*!"
        try:
            await ctx.bot.send_message(
                chat_id=referrer_id,
                text=f"🎉 *¡Ganaste {REFERRAL_COMMISSION} Stars!*\n\nUno de tus referidos compró Premium. ¡Sigue compartiendo con /referido! 💰",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await update.message.reply_text(
        f"🎉 *¡Pago exitoso! Ya eres Premium.*\n\n"
        f"Tu acceso vence el *{until}*.\n"
        f"¡Disfruta clips ilimitados! 🚀{referrer_msg}",
        parse_mode="Markdown",
    )

async def handle_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    uid = update.effective_user.id
    register_user(uid)

    if not is_supported_url(url):
        await update.message.reply_text(
            "❌ URL no soportada.\n\n"
            "Plataformas compatibles:\n"
            "▶️ YouTube\n🟣 Twitch\n🟢 Kick\n🎵 TikTok (VODs)"
        )
        return

    allowed, remaining = can_process(uid)
    if not allowed:
        bot_username = (await ctx.bot.get_me()).username
        ref_link = get_ref_link(bot_username, uid)
        await update.message.reply_text(
            f"⛔ *Límite diario alcanzado*\n\n"
            f"Ya procesaste tu video gratuito de hoy.\n\n"
            "¿Cómo conseguir más?\n\n"
            f"⭐ *Opción 1:* Compra Premium por *{STARS_PRICE} Stars/mes*\n\n"
            f"💰 *Opción 2:* Gana Stars compartiendo:\n"
            f"`{ref_link}`\n"
            f"Cada amigo que pague = *{REFERRAL_COMMISSION} Stars para ti*\n\n"
            "🕐 *Opción 3:* Vuelve mañana",
            parse_mode="Markdown",
            reply_markup=get_premium_keyboard(),
        )
        return

    # Pedir duración del clip
    ctx.user_data["pending_url"] = url
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ 20 segundos", callback_data="clip_20"),
            InlineKeyboardButton("⏱ 30 segundos", callback_data="clip_30"),
            InlineKeyboardButton("⏱ 40 segundos", callback_data="clip_40"),
        ]
    ])
    await update.message.reply_text(
        "✂️ *¿Qué duración quieres para cada clip?*",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

async def clip_duration_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    clip_duration = int(query.data.replace("clip_", ""))
    url = ctx.user_data.get("pending_url")

    if not url:
        await query.edit_message_text("❌ Envía el link de nuevo por favor.")
        return

    await query.edit_message_text(
        f"⏳ *Procesando tu video...*\n\n"
        f"🔍 Descargando audio...\n"
        f"Esto puede tardar 1-3 minutos según la duración del video.",
        parse_mode="Markdown"
    )

    # Descargar audio
    audio_path, duration = await asyncio.get_event_loop().run_in_executor(
        None, download_audio, url, uid
    )

    if not audio_path or not os.path.exists(audio_path):
        await query.edit_message_text("❌ No pude descargar el video. Verifica el link e intenta de nuevo.")
        return

    await ctx.bot.send_message(
        chat_id=uid,
        text="🎙️ *Transcribiendo audio con IA...*",
        parse_mode="Markdown"
    )

    # Transcribir
    transcript = await asyncio.get_event_loop().run_in_executor(
        None, transcribe_audio, audio_path
    )
    cleanup(audio_path)

    if not transcript:
        await ctx.bot.send_message(chat_id=uid, text="❌ No pude transcribir el audio. Intenta con otro video.")
        return

    await ctx.bot.send_message(
        chat_id=uid,
        text="🔥 *Detectando momentos virales con IA...*",
        parse_mode="Markdown"
    )

    # Detectar momentos virales
    moments = await get_viral_moments(transcript, duration, clip_duration)

    if not moments:
        await ctx.bot.send_message(chat_id=uid, text="❌ No pude detectar momentos virales. Intenta con otro video.")
        return

    user_data = get_user(uid)
    max_clips = FREE_CLIPS_PER_VIDEO if not is_premium(user_data) else len(moments)
    moments = moments[:max_clips]

    record_video(uid)

    await ctx.bot.send_message(
        chat_id=uid,
        text=f"✂️ *¡Encontré {len(moments)} momentos virales! Descargando clips...*",
        parse_mode="Markdown"
    )

    # Descargar y enviar cada clip
    for i, moment in enumerate(moments, 1):
        start_sec = moment.get("start", 0)
        end_sec = moment.get("end", start_sec + clip_duration)
        title = moment.get("title", f"Clip {i}")
        description = moment.get("description", "")
        hashtags = moment.get("hashtags", "")

        await ctx.bot.send_message(
            chat_id=uid,
            text=f"⏬ *Descargando clip {i}/{len(moments)}...*",
            parse_mode="Markdown"
        )

        clip_path = await asyncio.get_event_loop().run_in_executor(
            None, download_video_segment, url, uid, start_sec, end_sec, i
        )

        if clip_path and os.path.exists(clip_path):
            caption = (
                f"🎬 *Clip {i}:* {title}\n\n"
                f"📝 {description}\n\n"
                f"🏷️ {hashtags}"
            )
            try:
                with open(clip_path, "rb") as f:
                    await ctx.bot.send_video(
                        chat_id=uid,
                        video=f,
                        caption=caption,
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.error("Send clip error: %s", e)
                await ctx.bot.send_message(
                    chat_id=uid,
                    text=f"✅ *Clip {i}: {title}*\n\n📝 {description}\n\n🏷️ {hashtags}\n\n⚠️ El video fue muy grande para enviar.",
                    parse_mode="Markdown"
                )
            finally:
                cleanup(clip_path)
        else:
            await ctx.bot.send_message(
                chat_id=uid,
                text=f"⚠️ No pude descargar el clip {i}. Intenta de nuevo.",
            )

    # Mensaje final
    user_data = get_user(uid)
    if not is_premium(user_data):
        today = str(date.today())
        used = user_data["videos_today"] if user_data["last_date"] == today else 1
        remaining_videos = FREE_DAILY_LIMIT - used
        bot_username = (await ctx.bot.get_me()).username
        ref_link = get_ref_link(bot_username, uid)

        final_msg = (
            f"✅ *¡Tus {len(moments)} clips están listos!*\n\n"
            f"📊 Videos restantes hoy: *{remaining_videos}/{FREE_DAILY_LIMIT}*\n\n"
        )
        if remaining_videos == 0:
            final_msg += (
                "⛔ *Has agotado tus videos gratuitos de hoy.*\n\n"
                f"⭐ *¿Quieres clips ilimitados?*\n"
                f"Premium por solo *{STARS_PRICE} Stars/mes*\n\n"
                f"💰 *¿Prefieres gratis?*\n"
                f"Comparte tu link y gana Stars:\n"
                f"`{ref_link}`\n"
                f"Cada amigo que pague = *{REFERRAL_COMMISSION} Stars*"
            )
            await ctx.bot.send_message(
                chat_id=uid,
                text=final_msg,
                parse_mode="Markdown",
                reply_markup=get_premium_keyboard()
            )
        else:
            await ctx.bot.send_message(chat_id=uid, text=final_msg, parse_mode="Markdown")
    else:
        await ctx.bot.send_message(
            chat_id=uid,
            text=f"✅ *¡Tus {len(moments)} clips están listos!* 🚀",
            parse_mode="Markdown"
        )

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("premium", premium_cmd))
    app.add_handler(CommandHandler("referido", referido_cmd))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_premium$"))
    app.add_handler(CallbackQueryHandler(clip_duration_callback, pattern="^clip_"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    logger.info("ClipViral Bot iniciado ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
