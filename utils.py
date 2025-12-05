from telegram import Update
from config import settings


async def is_admin(update: Update, context) -> bool:
    """Проверка админа"""
    user_id = update.effective_user.id
    return user_id in settings.ADMIN_IDS


def validate_query(query: str):
    """Проверка запроса"""
    if len(query) > settings.MAX_QUERY_LENGTH:
        return False, f"❌ Слишком длинный запрос (макс {settings.MAX_QUERY_LENGTH} символов)"
    if len(query.strip()) < 2:
        return False, "❌ Слишком короткий запрос"
    return True, ""