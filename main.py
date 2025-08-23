import os
import time
import logging
import asyncio
from typing import List, Optional

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from config import BOT_TOKEN, CHAT_ID, GENRES, DEFAULT_GENRE, Source, BotState, TrackInfo, is_valid
from utils import is_admin, make_menu_keyboard, make_search_keyboard, make_vote_keyboard, format_status
from locks import state_lock, radio_lock
from downloader import AudioDownloadManager
from lastfm_api import get_top_tracks_by_genre

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("GrooveAIBot")

ADMINS_ENV = os.getenv("ADMINS", "")

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.state = BotState()
        self.dl = AudioDownloadManager()

        # schedule jobs via PTB JobQueue (installed via extra)
        jq = self.app.job_queue
        jq.run_repeating(self.update_radio, interval=60, first=5, name="radio_loop", coalesce=True, max_instances=1)
        jq.run_repeating(self.update_status_message, interval=30, first=10, name="status", coalesce=True, max_instances=1)
        jq.run_repeating(self.autostart_vote, interval=60, first=20, name="vote_watch", coalesce=True, max_instances=1)

        log.info("Bot initialized")

    async def send_status(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        st = self.state.radio
        text = format_status(self.state.source.value, st.current_genre or DEFAULT_GENRE, st.current_track.title if st.current_track else None)
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, disable_web_page_preview=True, reply_markup=make_menu_keyboard())
        except Exception as e:
            log.warning("Failed to send status: %s", e)

    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE):
        # can be extended to edit a pinned message
        pass

    async def autostart_vote(self, context: ContextTypes.DEFAULT_TYPE):
        # every hour at minute 0 start vote; simplified: if not active and minute==0
        minute = int(time.strftime("%M"))
        if minute == 0 and not self.state.voting_active:
            await self.vote_broadcast(context)

    async def vote_broadcast(self, context: ContextTypes.DEFAULT_TYPE):
        self.state.voting_active = True
        self.state.vote_counts = {g: 0 for g in GENRES}
        chats = list(self.state.active_chats.keys()) or ([int(CHAT_ID)] if CHAT_ID else [])
        for chat_id in chats:
            try:
                await context.bot.send_message(chat_id=chat_id, text="🗳 Старт голосования за жанр! Окно: 3 мин.", reply_markup=make_vote_keyboard(GENRES))
            except Exception as e:
                log.warning("vote message failed: %s", e)
        # stop vote after 3 min
        context.job_queue.run_once(self.finish_vote, when=180)

    async def finish_vote(self, context: ContextTypes.DEFAULT_TYPE):
        self.state.voting_active = False
        if not self.state.vote_counts:
            return
        winner = max(self.state.vote_counts.items(), key=lambda x: x[1])[0]
        self.state.radio.current_genre = winner
        chats = list(self.state.active_chats.keys()) or ([int(CHAT_ID)] if CHAT_ID else [])
        for cid in chats:
            try:
                await context.bot.send_message(chat_id=cid, text=f"✅ Победил жанр: {winner}. Продолжаем радио!")
            except Exception:
                pass

    async def update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        async with radio_lock:
            if not self.state.radio.is_on:
                return
            chat_ids = list(self.state.active_chats.keys()) or ([int(CHAT_ID)] if CHAT_ID else [])
            if not chat_ids:
                return
            genre = self.state.radio.current_genre or DEFAULT_GENRE

            # Build candidate queries from Last.fm
            queries = await get_top_tracks_by_genre(genre, limit=15)
            if not queries:
                for cid in chat_ids:
                    await context.bot.send_message(chat_id=cid, text=f"⚠️ Для жанра '{genre}' ничего не найдено, пробую следующий...")
                # fallback to default
                queries = await get_top_tracks_by_genre(DEFAULT_GENRE, limit=15)

            # Skip already played (by title)
            queries = [q for q in queries if q not in self.state.history]
            if not queries:
                queries = await get_top_tracks_by_genre(DEFAULT_GENRE, limit=15)

            for cid in chat_ids:
                await context.bot.send_message(chat_id=cid, text=f"📻 Радио: {genre}")

            # Try to download
            for q in queries:
                res = await self.dl.download_by_query(q)
                if not res:
                    continue
                path, info = res
                # prevent repeats
                self.state.history.append(q)
                self.state.radio.current_track = info
                # Send
                for cid in chat_ids:
                    try:
                        await context.bot.send_audio(chat_id=cid, audio=open(path, "rb"), caption=f"▶️ Отправляю трек: {q}")
                    except Exception as e:
                        log.warning("send_audio failed: %s", e)
                break

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.state.active_chats[update.effective_chat.id] = 1
        await update.message.reply_text("📻 Радио включено! Музыка скоро начнет играть.")
        await self.send_status(update.effective_chat.id, context)

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.send_status(update.effective_chat.id, context)

    async def play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text("Использование: /play <название>")
            return
        query = " ".join(context.args).strip()
        # Pretend search by building 10 variants (yt_dlp will resolve exact link on pick)
        titles = [query] + [f"{query} #{i}" for i in range(2, 11)]
        self.state.search_results[update.effective_chat.id] = [TrackInfo(title=t) for t in titles]
        await update.message.reply_text("Выберите трек:", reply_markup=make_search_keyboard(titles))

    async def on_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if q.data.startswith("pick_"):
            idx = int(q.data.split("_")[1])
            items = self.state.search_results.get(q.message.chat_id) or []
            if idx >= len(items):
                return
            title = items[idx].title
            await q.edit_message_text(f"▶️ Отправляю: {title}")
            res = await self.dl.download_by_query(title)
            if res:
                path, info = res
                await context.bot.send_audio(chat_id=q.message.chat_id, audio=open(path,"rb"), caption=title)
        elif q.data.startswith("vote_"):
            genre = q.data.split("_",1)[1]
            self.state.vote_counts[genre] = self.state.vote_counts.get(genre, 0) + 1
            await q.answer(f"Голос за {genre} засчитан!", show_alert=False)
        elif q.data == "ron":
            self.state.radio.is_on = True
            await q.edit_message_text("📻 Радио включено.", reply_markup=make_menu_keyboard())
        elif q.data == "roff":
            self.state.radio.is_on = False
            await q.edit_message_text("⏸ Радио выключено.", reply_markup=make_menu_keyboard())
        elif q.data == "src_youtube":
            self.state.source = Source.YOUTUBE
            await q.answer("Источник: YouTube")
        elif q.data == "src_soundcloud":
            self.state.source = Source.SOUNDCLOUD
            await q.answer("Источник: SoundCloud")

    async def vote_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.vote_broadcast(context)

    async def ron(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id, os.getenv("ADMINS")):
            return
        self.state.radio.is_on = True
        await update.message.reply_text("📻 Радио включено.")

    async def roff(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id, os.getenv("ADMINS")):
            return
        self.state.radio.is_on = False
        await update.message.reply_text("⏸ Радио выключено.")

    async def skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id, os.getenv("ADMINS")):
            return
        # Trigger immediate update
        await self.update_radio(context)

def build_app() -> Application:
    if not is_valid():
        raise SystemExit("Environment invalid. Set TELEGRAM_TOKEN and LASTFM_API_KEY")
    app = Application.builder().token(BOT_TOKEN).build()
    bot = MusicBot(app)

    app.add_handler(CommandHandler(["start"], bot.start))
    app.add_handler(CommandHandler(["menu"], bot.menu))
    app.add_handler(CommandHandler(["play","p"], bot.play))
    app.add_handler(CommandHandler(["vote"], bot.vote_cmd))
    app.add_handler(CommandHandler(["ron"], bot.ron))
    app.add_handler(CommandHandler(["roff"], bot.roff))
    app.add_handler(CommandHandler(["skip"], bot.skip))
    app.add_handler(CallbackQueryHandler(bot.on_pick))

    return app

if __name__ == "__main__":
    application = build_app()
    application.run_polling(close_loop=False)
