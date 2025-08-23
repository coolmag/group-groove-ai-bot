import os, time, logging, asyncio
from typing import Optional, List
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, JobQueue
from telegram.error import BadRequest, Forbidden
from config import BOT_TOKEN, BotState, ChatData, MESSAGES, check_environment, GENRES, Source, VOTE_WINDOW_SEC, ADMINS
from downloader import AudioDownloadManager
from utils import is_admin, get_menu_keyboard, format_status_message, build_search_keyboard, build_vote_keyboard, fmt_duration
from locks import state_lock, radio_lock, vote_lock

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("GrooveAIBot")

class MusicBot:
    def __init__(self, app: Application):
        self.app = app
        self.job_queue: JobQueue = self.app.job_queue
        self.downloader = AudioDownloadManager()
        self.state = BotState()
        logger.info("Initialized bot state: %s", self.state)

        self._register_handlers()
        self.app.add_error_handler(self.on_error)

        # Background jobs: radio every 60s, status every 5s
        self.job_queue.run_repeating(self.update_radio, interval=60, first=10, name="update_radio")
        self.job_queue.run_repeating(self.update_status_message, interval=5, first=3, name="update_status_message")
        self.job_queue.run_repeating(self.start_hourly_vote, interval=3600, first=self._seconds_to_next_hour(), name="start_hourly_vote")
        self.job_queue.run_repeating(self.check_vote_end, interval=10, first=12, name="check_vote_end")

    def _register_handlers(self):
        handlers = [
            CommandHandler(["start"], self.start),
            CommandHandler(["menu","status"], self.show_menu),
            CommandHandler(["play","p"], self.play),
            CommandHandler(["ron","radio_on"], self.radio_on),
            CommandHandler(["roff","radio_off"], self.radio_off),
            CommandHandler(["next","skip"], self.next_track),
            CommandHandler("source", self.cmd_source),
            CommandHandler("vote", self.manual_vote),
            CommandHandler("keyboard", self.keyboard),
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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.downloader.check_ffmpeg()
        await update.message.reply_text(MESSAGES["welcome"])
        await self.show_menu(update, context)

    async def keyboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from telegram import ReplyKeyboardMarkup
        kb = [["/play", "/vote"], ["/menu", "/skip"], ["/source"]]
        await update.message.reply_text("Выберите действие:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

    async def show_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        async with state_lock:
            if chat_id not in self.state.active_chats:
                self.state.active_chats[chat_id] = ChatData()
        await self._update_status_for_chat(context, chat_id)

    async def play(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = " ".join(context.args).strip()
        if not query:
            await context.bot.send_message(chat_id, MESSAGES["play_usage"], parse_mode="HTML")
            return
        searching_msg = await context.bot.send_message(chat_id, MESSAGES["searching"])
        results = await self.downloader.search_tracks(query, self.state.source, limit=10)
        if not results:
            await searching_msg.edit_text(MESSAGES["not_found"])
            return
        titles = [f"{r.title} — {fmt_duration(r.duration)}" for r in results]
        kb = build_search_keyboard(titles)
        async with state_lock:
            self.state.search_results[chat_id] = results
        await searching_msg.edit_text(f"Нашёл {len(results)} трек(ов). Выберите:", reply_markup=kb)

    async def radio_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"]); return
        async with state_lock:
            self.state.radio_status.is_on = True
        await context.bot.send_message(update.effective_chat.id, MESSAGES["radio_on"])
        await self._update_status_for_chat(context, update.effective_chat.id)

    async def radio_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"]); return
        async with state_lock:
            self.state.radio_status.is_on = False
        await context.bot.send_message(update.effective_chat.id, MESSAGES["radio_off"])
        await self._update_status_for_chat(context, update.effective_chat.id)

    async def next_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"]); return
        async with radio_lock:
            self.state.radio_status.last_played_time = 0
            self.state.radio_status.current_track = None
        await context.bot.send_message(update.effective_chat.id, MESSAGES["next_track"])

    async def cmd_source(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin(uid, ADMINS):
            await context.bot.send_message(update.effective_chat.id, MESSAGES["admin_only"]); return
        if context.args:
            val = context.args[0].lower().strip()
            try:
                src = Source(val)
                async with state_lock:
                    self.state.source = src
                await context.bot.send_message(update.effective_chat.id, MESSAGES["source_switched"].format(source=src.value), parse_mode="HTML")
                await self._update_status_for_chat(context, update.effective_chat.id)
                return
            except Exception:
                pass
        await context.bot.send_message(update.effective_chat.id, "Доступные источники: youtube, ytmusic, soundcloud, jamendo, archive")

    async def manual_vote(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._start_vote(update.effective_chat.id, context)

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if data == "radio_on": return await self.radio_on(update, context)
        if data == "radio_off": return await self.radio_off(update, context)
        if data == "next_track": return await self.next_track(update, context)
        if data == "source_switch":
            async with state_lock:
                sources = list(Source)
                idx = sources.index(self.state.source)
                self.state.source = sources[(idx + 1) % len(sources)]
                src = self.state.source
            await context.bot.send_message(update.effective_chat.id, MESSAGES["source_switched"].format(source=src.value), parse_mode="HTML")
            await self._update_status_for_chat(context, update.effective_chat.id)
            return
        if data == "vote_now": return await self._start_vote(update.effective_chat.id, context)
        if data.startswith("pick:"):
            try: idx = int(data.split(":",1)[1])
            except ValueError: return
            chat_id = update.effective_chat.id
            async with state_lock:
                results = self.state.search_results.get(chat_id, [])
            if not results or idx<0 or idx>=len(results):
                await context.bot.send_message(chat_id, "Выбор недействителен, начните поиск заново: /play <запрос>"); return
            sel = results[idx]
            async with state_lock:
                self.state.search_results.pop(chat_id, None)
            await self._send_track_by_url(chat_id, sel.url, context)
            return
        if data.startswith("vote:"):
            genre = data.split(":",1)[1]
            async with vote_lock:
                if not self.state.voting_active:
                    await context.bot.send_message(update.effective_chat.id, "Сейчас нет активного голосования."); return
                self.state.vote_counts[genre] = self.state.vote_counts.get(genre,0) + 1
            await context.bot.send_message(update.effective_chat.id, MESSAGES["vote_accepted"].format(genre=genre), parse_mode="HTML")
            return

    # Radio logic with source fallback and robust error handling
    async def update_radio(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            async with radio_lock:
                rs = self.state.radio_status
                if not rs.is_on:
                    return
                # Logic to wait for the previous track to finish has been removed.
                # A new track will be fetched every minute.
                genre = rs.current_genre or self._random_genre()
                self.state.radio_status.current_genre = genre

            # Try multiple sources in order to find a track
            preferred_sources = [self.state.source, Source.YOUTUBE_MUSIC, Source.SOUNDCLOUD, Source.JAMENDO, Source.YOUTUBE]
            results = []
            for src in preferred_sources:
                results = await self.downloader.search_tracks(f"{genre} {os.getenv('RADIO_SEARCH_QUERY_SUFFIX','music')}", src, limit=10) # Increased limit to get more results
                if results:
                    async with state_lock:
                        self.state.source = src
                    break

            if not results:
                logger.warning("Radio: no results for genre %s on any source", genre)
                for cid in list(self.state.active_chats.keys()):
                    try:
                        await context.bot.send_message(cid, f"⚠️ Для жанра '{genre}' ничего не найдено, пробую следующий...")
                    except Exception:
                        pass
                return

            # Filter tracks to be 10 minutes or less (600 seconds)
            short_tracks = [t for t in results if t.duration and t.duration <= 600]

            if not short_tracks:
                logger.warning("Radio: Found tracks for genre %s, but all were longer than 10 minutes.", genre)
                for cid in list(self.state.active_chats.keys()):
                    try:
                        await context.bot.send_message(cid, f"⚠️ Треки для жанра '{genre}' слишком длинные, ищу другой жанр...")
                    except Exception:
                        pass
                return

            import random
            sel = random.choice(short_tracks)
            
            # announce and broadcast
            for cid in list(self.state.active_chats.keys()):
                try:
                    await context.bot.send_message(cid, f"▶️ Отправляю трек: {sel.title} — {sel.artist}")
                except Exception:
                    pass
            
            ok = await self._broadcast_track(sel.url, context, caption=f"📻 Радио: {genre}")
            if ok:
                async with state_lock:
                    self.state.radio_status.current_track = sel
                    self.state.radio_status.last_played_time = time.time()
        except Exception as e:
            logger.error("update_radio failed: %s", e, exc_info=True)
            # ensure we don't leave radio blocked; notify chats
            for cid in list(self.state.active_chats.keys()):
                try:
                    await context.bot.send_message(cid, "⚠️ Ошибка при попытке поставить трек. Попробуем снова через минуту.")
                except Exception:
                    pass

    async def _broadcast_track(self, url: str, context: ContextTypes.DEFAULT_TYPE, caption: Optional[str] = None) -> bool:
        path_info = await self.downloader.download_by_url(url, prefer_mp3=True)
        if not path_info:
            logger.error("Failed to download for broadcast")
            return False
        audio_path, ti = path_info
        ok_any = False
        try:
            for chat_id in list(self.state.active_chats.keys()):
                try:
                    logger.info("Sending audio to chat %s: %s — %s", chat_id, ti.artist, ti.title)
                    with open(audio_path, "rb") as f:
                        await context.bot.send_audio(chat_id=chat_id, audio=f, title=ti.title, performer=ti.artist, duration=ti.duration, caption=caption or f"🎵 {ti.artist} — {ti.title} (источник: {ti.source})")
                    ok_any = True
                except Exception as e:
                    logger.error("Failed to send audio to %s: %s", chat_id, e)
        finally:
            try:
                if os.path.exists(audio_path): os.remove(audio_path)
            except Exception:
                pass
        return ok_any

    async def _send_track_by_url(self, chat_id: int, url: str, context: ContextTypes.DEFAULT_TYPE):
        sent_msg = await context.bot.send_message(chat_id, "⬇️ Скачиваю и отправляю MP3...")
        path_info = await self.downloader.download_by_url(url, prefer_mp3=True)
        if not path_info:
            logger.error("Failed to download/send track for chat %s", chat_id)
            await sent_msg.edit_text("❌ Не удалось скачать трек. Попробуйте другой результат или источник.")
            return
        audio_path, ti = path_info
        try:
            logger.info("Sending MP3 audio to chat %s: %s — %s", chat_id, ti.artist, ti.title)
            with open(audio_path, "rb") as f:
                await context.bot.send_audio(chat_id=chat_id, audio=f, title=ti.title, performer=ti.artist, duration=ti.duration, caption=f"🎵 {ti.artist} — {ti.title} (источник: {ti.source})")
            await sent_msg.delete()
        except Exception as e:
            logger.error("Send audio failed: %s", e)
            await sent_msg.edit_text("❌ Ошибка при отправке трека.")
        finally:
            try: 
                if os.path.exists(audio_path): os.remove(audio_path)
            except Exception: pass

    # Voting (unchanged)
    async def _start_vote(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
        async with vote_lock:
            self.state.voting_active = True
            self.state.vote_end_ts = time.time() + VOTE_WINDOW_SEC
            self.state.vote_counts.clear()
        kb = build_vote_keyboard(GENRES[:12])
        mins = VOTE_WINDOW_SEC // 60
        await context.bot.send_message(chat_id, MESSAGES["vote_started"].format(mins=mins), reply_markup=kb, parse_mode="HTML")

    async def start_hourly_vote(self, context: ContextTypes.DEFAULT_TYPE):
        chats = list(self.state.active_chats.keys())
        if not chats: return
        mins = VOTE_WINDOW_SEC // 60
        kb = build_vote_keyboard(GENRES[:12])
        async with vote_lock:
            self.state.voting_active = True
            self.state.vote_end_ts = time.time() + VOTE_WINDOW_SEC
            self.state.vote_counts.clear()
        for cid in chats:
            try: await context.bot.send_message(cid, MESSAGES["vote_started"].format(mins=mins), reply_markup=kb, parse_mode="HTML")
            except Exception as e: logger.warning("Failed to send vote start to %s: %s", cid, e)

    async def check_vote_end(self, context: ContextTypes.DEFAULT_TYPE):
        async with vote_lock:
            if not self.state.voting_active: return
            if time.time() < self.state.vote_end_ts: return
            self.state.voting_active = False
            winner = max(self.state.vote_counts.items(), key=lambda kv: kv[1])[0] if self.state.vote_counts else None
        if winner:
            async with state_lock:
                self.state.radio_status.current_genre = winner
                self.state.radio_status.last_played_time = 0
            for cid in list(self.state.active_chats.keys()):
                try: await context.bot.send_message(cid, MESSAGES["vote_ended"].format(genre=winner), parse_mode="HTML")
                except Exception: pass

    # Status updates
    async def update_status_message(self, context: ContextTypes.DEFAULT_TYPE):
        keyboard = get_menu_keyboard(self.state)
        text = format_status_message(self.state)
        for cid, chat_data in list(self.state.active_chats.items()):
            try:
                if chat_data.status_message_id:
                    await context.bot.edit_message_text(chat_id=cid, message_id=chat_data.status_message_id, text=text, reply_markup=keyboard, parse_mode="HTML")
                else:
                    sent = await context.bot.send_message(cid, text, reply_markup=keyboard, parse_mode="HTML")
                    async with state_lock:
                        self.state.active_chats[cid].status_message_id = sent.message_id
            except BadRequest as e:
                if "message not found" in str(e).lower():
                    async with state_lock: self.state.active_chats[cid].status_message_id = None
                elif "message is not modified" in str(e).lower():
                    pass
                else:
                    logger.warning("Status update failed for %s: %s", cid, e)
            except Forbidden:
                logger.warning("Bot blocked in chat %s, removing.", cid)
                async with state_lock: self.state.active_chats.pop(cid, None)
            except Exception as e:
                logger.error("Unexpected status update error for %s: %s", cid, e)

    async def _update_status_for_chat(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        keyboard = get_menu_keyboard(self.state)
        text = format_status_message(self.state)
        chat_data = self.state.active_chats.get(chat_id)
        try:
            if chat_data and chat_data.status_message_id:
                await context.bot.edit_message_text(chat_id=chat_id, message_id=chat_data.status_message_id, text=text, reply_markup=keyboard, parse_mode="HTML")
            else:
                sent = await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="HTML")
                async with state_lock: self.state.active_chats[chat_id].status_message_id = sent.message_id
        except Exception as e:
            logger.warning("Failed to update status for chat %s: %s", chat_id, e)

    def _random_genre(self) -> str:
        import random
        return random.choice(GENRES)

    def _seconds_to_next_hour(self) -> int:
        now = time.time()
        next_hour = (int(now) // 3600 + 1) * 3600
        return max(5, int(next_hour - now))

    async def shutdown(self): await self.downloader.check_ffmpeg()

def main():
    if not check_environment(): return
    builder = Application.builder().token(BOT_TOKEN)
    app = builder.build()
    bot = MusicBot(app)
    import signal
    def _sig_handler(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        loop = asyncio.get_event_loop()
        loop.create_task(bot.shutdown())
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    logger.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__": main()
