import os
import logging
import asyncio
import time
from typing import Optional, List

from telegram import Update, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
)
from telegram.error import BadRequest, Forbidden

from config import (
    BOT_TOKEN, BotState, MESSAGES, check_environment, PROXY_ENABLED, PROXY_URL,
    GENRES, Source, VOTE_WINDOW_SEC, ADMINS
)
from downloader import AudioDownloadManager
from utils import is_admin, get_menu_keyboard, format_status_message, build_search_keyboard, build_vote_keyboard, fmt_duration
from locks import state_lock, radio_lock, vote_lock

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("GrooveAIBot")

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.job_queue: JobQueue = self.app.job_queue
        self.downloader = AudioDownloadManager()
        self.state = BotState()

        logger.info("Initialized bot state: %s", self.state)

        # Register handlers
        self._register_handlers()

        # Error handler
        self.app.add_error_handler(self.on_error)

        # Background jobs
        self.job_queue.run_repeating(self.update_radio, interval=15, first=5)
        self.job_queue.run_repeating(self.update_status_message, interval=30, first=8)
        # Voting scheduler
        first_delay = self._seconds_to_next_hour()
        self.job_queue.run_repeating(self.start_hourly_vote, interval=3600, first=first_delay)
        # Check for vote end
        self.job_queue.run_repeating(self.check_vote_end, interval=10, first=12)

    # ---------- Handlers ----------
    def _register_handlers(self):
        handlers = [
            CommandHandler("start", self.start),
            CommandHandler("menu", self.show_menu),
            CommandHandler("play", self.play),
            CommandHandler(["ron","radio_on"], self.radio_on),
            CommandHandler(["roff","radio_off"], self.radio_off),
            CommandHandler(["next","skip"], self.next_track),
            CommandHandler("source", self.cmd_source),
            CommandHandler("vote", self.manual_vote),
            CallbackQueryHandler(self.on_button),
        ]
        for h in handlers:
            self.app.add_handler(h)

    async def on_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.exception("Update %s caused error %s", update, context.error)
        try:
            if update and update.effective_chat:
                await context.bot.send_message(update.effective_chat.id, "⚠️ Произошла ошибка. Попробуйте ещё раз.")
        except Exception:
            pass

    # ---------- Commands ----------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.downloader.check_ffmpeg()
        await update.message.reply_text(MESSAGES["welcome"])
        await self.show_menu(update, context)

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        async with state_lock:
            if chat_id not in self.state.active_chats:
                self.state.active_chats[chat_id] = type(self.state).ChatData() if hasattr(type(self.state), "ChatData") else None
        await self.update_status_message(context, chat_id)

    async def play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = " ".join(context.args).strip()
        if not query:
            await context.bot.send_message(chat_id, MESSAGES["play_usage"], parse_mode="HTML")
            return
        msg = await context.bot.send_message(chat_id, MESSAGES["searching"])
        # Search
        results = await self.downloader.search_tracks(query, self.state.source, limit=10)
        if not results:
            await msg.edit_text(MESSAGES["not_found"])
            return
        # Build titles with durations
        titles = [f"{r.title} — {fmt_duration(r.duration)}" for r in results]
        kb = build_search_keyboard(titles)
        async with state_lock:
            self.state.search_results[chat_id] = results
        await msg.edit_text(f"Нашёл {len(results)} трек(ов). Выберите:", reply_markup=kb)

    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"])
            return
        async with state_lock:
            self.state.radio_status.is_on = True
        await context.bot.send_message(update.effective_chat.id, MESSAGES["radio_on"])
        await self.update_status_message(context, update.effective_chat.id)

    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"])
            return
        async with state_lock:
            self.state.radio_status.is_on = False
        await context.bot.send_message(update.effective_chat.id, MESSAGES["radio_off"])
        await self.update_status_message(context, update.effective_chat.id)

    async def next_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"])
            return
        async with radio_lock:
            self.state.radio_status.last_played_time = 0
            self.state.radio_status.current_track = None
        await context.bot.send_message(update.effective_chat.id, MESSAGES["next_track"])

    async def cmd_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"])
            return
        # /source youtube|soundcloud|ytmusic|jamendo|archive
        if context.args:
            val = context.args[0].lower().strip()
            try:
                src = Source(val)
                async with state_lock:
                    self.state.source = src
                await context.bot.send_message(update.effective_chat.id, MESSAGES["source_switched"].format(source=src.value), parse_mode="HTML")
                await self.update_status_message(context, update.effective_chat.id)
                return
            except Exception:
                pass
        await context.bot.send_message(update.effective_chat.id, "Доступные источники: youtube, ytmusic, soundcloud, jamendo, archive")

    async def manual_vote(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._start_vote(update.effective_chat.id, context)

    # ---------- Callback ----------
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""

        if data == "radio_on":
            return await self.radio_on(update, context)
        if data == "radio_off":
            return await self.radio_off(update, context)
        if data == "next_track":
            return await self.next_track(update, context)
        if data == "source_switch":
            # cycle through Source enum
            async with state_lock:
                sources = list(Source)
                idx = sources.index(self.state.source)
                self.state.source = sources[(idx + 1) % len(sources)]
                src = self.state.source
            await context.bot.send_message(update.effective_chat.id, MESSAGES["source_switched"].format(source=src.value), parse_mode="HTML")
            await self.update_status_message(context, update.effective_chat.id)
            return

        if data == "vote_now":
            return await self._start_vote(update.effective_chat.id, context)

        if data.startswith("pick:"):
            try:
                idx = int(data.split(":", 1)[1])
            except ValueError:
                return
            chat_id = update.effective_chat.id
            async with state_lock:
                results = self.state.search_results.get(chat_id, [])
            if not results or idx < 0 or idx >= len(results):
                await context.bot.send_message(chat_id, "Выбор недействителен, начните поиск заново: /play <запрос>")
                return
            sel = results[idx]
            await self._send_track_by_url(chat_id, sel.url, context)
            return

        if data.startswith("vote:"):
            genre = data.split(":", 1)[1]
            async with vote_lock:
                if not self.state.voting_active:
                    await context.bot.send_message(update.effective_chat.id, "Сейчас нет активного голосования.")
                    return
                self.state.vote_counts[genre] = self.state.vote_counts.get(genre, 0) + 1
            await context.bot.send_message(update.effective_chat.id, MESSAGES["vote_accepted"].format(genre=genre), parse_mode="HTML")
            return

    # ---------- Radio logic ----------
    async def update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        async with radio_lock:
            rs = self.state.radio_status
            if not rs.is_on:
                return
            now = time.time()
            # if track playing and cooldown not reached; approximate by track duration
            if rs.current_track:
                elapsed = now - rs.last_played_time
                dur = max(60, rs.current_track.duration or 180)
                if elapsed < dur:
                    return
            # Pick genre
            genre = rs.current_genre or self._random_genre()
            self.state.radio_status.current_genre = genre

        # Search and send
        q = f"{genre} {os.getenv('RADIO_SEARCH_QUERY_SUFFIX', 'music')}"
        results = await self.downloader.search_tracks(q, self.state.source, limit=5)
        if not results:
            logger.warning("Radio: no results for genre %s", genre)
            return
        # pick random from top 5
        import random
        sel = random.choice(results[: min(5, len(results))])
        # Download + send
        sent = await self._broadcast_track(sel.url, context, caption=f"📻 Радио: {genre}")
        if sent:
            async with state_lock:
                self.state.radio_status.current_track = sel
                self.state.radio_status.last_played_time = time.time()

    async def _broadcast_track(self, url: str, context: ContextTypes.DEFAULT_TYPE, caption: Optional[str] = None) -> bool:
        path_info = await self.downloader.download_by_url(url, prefer_mp3=True)
        if not path_info:
            return False
        audio_path, ti = path_info
        ok_any = False
        try:
            for chat_id in list(self.state.active_chats.keys()):
                try:
                    with open(audio_path, "rb") as f:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            title=ti.title,
                            performer=ti.artist,
                            duration=ti.duration,
                            caption=caption or f"🎵 {ti.artist} — {ti.title} (источник: {ti.source})"
                        )
                    ok_any = True
                except Exception as e:
                    logger.error("Failed to send audio to %s: %s", chat_id, e)
        finally:
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass
        return ok_any

    async def _send_track_by_url(self, chat_id: int, url: str, context: ContextTypes.DEFAULT_TYPE):
        sent_msg = await context.bot.send_message(chat_id, "⬇️ Скачиваю и отправляю MP3...")
        path_info = await self.downloader.download_by_url(url, prefer_mp3=True)
        if not path_info:
            await sent_msg.edit_text("❌ Не удалось скачать трек. Попробуйте другой результат или источник.")
            return
        audio_path, ti = path_info
        try:
            with open(audio_path, "rb") as f:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    title=ti.title,
                    performer=ti.artist,
                    duration=ti.duration,
                    caption=f"🎵 {ti.artist} — {ti.title} (источник: {ti.source})"
                )
            await sent_msg.delete()
        except Exception as e:
            logger.error("Send audio failed: %s", e)
            await sent_msg.edit_text("❌ Ошибка при отправке трека.")
        finally:
            try:
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception:
                pass

    # ---------- Voting ----------
    async def _start_vote(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        async with vote_lock:
            self.state.voting_active = True
            self.state.vote_end_ts = time.time() + VOTE_WINDOW_SEC
            self.state.vote_counts.clear()
        kb = build_vote_keyboard(GENRES[:12])  # show first 12 for compact UI
        mins = VOTE_WINDOW_SEC // 60
        await context.bot.send_message(chat_id, MESSAGES["vote_started"].format(mins=mins), reply_markup=kb, parse_mode="HTML")

    async def start_hourly_vote(self, context: ContextTypes.DEFAULT_TYPE):
        # Broadcast vote to all active chats
        chats = list(self.state.active_chats.keys())
        if not chats:
            return
        mins = VOTE_WINDOW_SEC // 60
        kb = build_vote_keyboard(GENRES[:12])
        async with vote_lock:
            self.state.voting_active = True
            self.state.vote_end_ts = time.time() + VOTE_WINDOW_SEC
            self.state.vote_counts.clear()
        for cid in chats:
            try:
                await context.bot.send_message(cid, MESSAGES["vote_started"].format(mins=mins), reply_markup=kb, parse_mode="HTML")
            except Exception as e:
                logger.warning("Failed to send vote start to %s: %s", cid, e)

    async def check_vote_end(self, context: ContextTypes.DEFAULT_TYPE):
        async with vote_lock:
            if not self.state.voting_active:
                return
            if time.time() < self.state.vote_end_ts:
                return
            self.state.voting_active = False
            if not self.state.vote_counts:
                winner = None
            else:
                winner = max(self.state.vote_counts.items(), key=lambda kv: kv[1])[0]
        if winner:
            async with state_lock:
                self.state.radio_status.current_genre = winner
                self.state.radio_status.last_played_time = 0  # trigger immediate play
            # Notify
            for cid in list(self.state.active_chats.keys()):
                try:
                    await context.bot.send_message(cid, MESSAGES["vote_ended"].format(genre=winner), parse_mode="HTML")
                except Exception:
                    pass

    # ---------- Status message ----------
    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE, chat_id: Optional[int] = None):
        keyboard = get_menu_keyboard(self.state)
        text = format_status_message(self.state)
        targets = [chat_id] if chat_id else list(self.state.active_chats.keys())
        for cid in targets:
            chat_data = self.state.active_chats.get(cid)
            try:
                if chat_data and chat_data.status_message_id:
                    await context.bot.edit_message_text(
                        chat_id=cid, message_id=chat_data.status_message_id,
                        text=text, reply_markup=keyboard, parse_mode="HTML"
                    )
                else:
                    sent = await context.bot.send_message(cid, text, reply_markup=keyboard, parse_mode="HTML")
                    async with state_lock:
                        if cid in self.state.active_chats and hasattr(chat_data, 'status_message_id'):
                            self.state.active_chats[cid].status_message_id = sent.message_id
            except BadRequest as e:
                if "message not found" in str(e).lower():
                    async with state_lock:
                        if cid in self.state.active_chats and hasattr(chat_data, 'status_message_id'):
                            self.state.active_chats[cid].status_message_id = None
                else:
                    logger.warning("Status update failed for %s: %s", cid, e)
            except Forbidden:
                logger.warning("Bot blocked in chat %s, removing.", cid)
                async with state_lock:
                    if cid in self.state.active_chats:
                        del self.state.active_chats[cid]
            except Exception as e:
                logger.error("Unexpected status update error for %s: %s", cid, e)

    # ---------- Helpers ----------
    def _random_genre(self) -> str:
        import random
        return random.choice(GENRES)

    def _seconds_to_next_hour(self) -> int:
        now = time.time()
        next_hour = (int(now) // 3600 + 1) * 3600
        return max(5, int(next_hour - now))

    async def shutdown(self):
        await self.downloader.close()

def main():
    if not check_environment():
        return
    builder = Application.builder().token(BOT_TOKEN).job_queue()
    app = builder.build()
    bot = MusicBot(app)

    # Graceful shutdown
    import signal
    import functools
    def _sig_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        loop = asyncio.get_event_loop()
        loop.create_task(bot.shutdown())
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
