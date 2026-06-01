from aiogram import Router

from . import chats, dialog, settings

router = Router()
router.include_router(settings.router)
router.include_router(chats.router)
# dialog ловит любой текст — подключаем последним.
router.include_router(dialog.router)
