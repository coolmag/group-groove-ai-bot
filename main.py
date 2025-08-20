# -*- coding: utf-8 -*-
import logging
import asyncio
import json
import random
import shutil
import re
import yt_dlp
from typing import List, Optional, Deque
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    PollAnswerHandler,
    JobQueue,
)
from telegram.error import BadRequest, TelegramError
from functools import wraps
from asyncio import Lock
from config import *
from utils import *
from radio import *
from handlers import *

# --- Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

