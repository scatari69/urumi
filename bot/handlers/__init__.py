from aiogram import Router

from bot.handlers import chat, common, logger, membership, mood, profile, summary

router = Router(name="root")
router.include_router(membership.router)
router.include_router(logger.router)
router.include_router(summary.router)
router.include_router(profile.router)
router.include_router(mood.router)
router.include_router(common.router)
router.include_router(chat.router)
