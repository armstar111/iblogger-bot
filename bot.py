"""Бот платной подписки на закрытый канал iBloger.

Как это работает:
  1. Человек жмёт «Вступить в канал» → выбирает тариф → Telegram показывает окно оплаты (ЮKassa).
  2. После оплаты бот записывает подписку в базу и присылает ссылку в канал. Ссылка работает через
     заявки на вступление: бот сам одобряет заявку, только если у человека есть активная подписка.
  3. Раз в CHECK_EVERY_SEC секунд бот проверяет базу:
       - за REMIND_DAYS дней до конца присылает напоминание с кнопкой «Оплатить»;
       - когда срок вышел, убирает человека из канала.
  4. Вопросы подписчиков приходят тебе в личку, ответ реплаем уходит обратно.

Автосписаний нет: встроенные платежи Telegram их не поддерживают.
Продление = новая оплата, дни добавляются к концу текущей подписки.
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatJoinRequest,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv

import db

load_dotenv()

# ───────────────────────── Настройки (файл .env) ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
PROVIDER_TOKEN = os.environ["PROVIDER_TOKEN"]  # токен ЮKassa из @BotFather (сначала TEST)
CHANNEL_ID = int(os.environ["CHANNEL_ID"])      # вида -100xxxxxxxxxx
ADMIN_ID = int(os.environ["ADMIN_ID"])          # твой Telegram ID
REMIND_DAYS = int(os.getenv("REMIND_DAYS", "3"))
CHECK_EVERY_SEC = int(os.getenv("CHECK_EVERY_SEC", "3600"))
SEND_RECEIPT = os.getenv("SEND_RECEIPT", "0") == "1"  # 1 — отправлять данные чека в ЮKassa
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
DB_FILE = os.getenv("DB_FILE", "bot.db")
# Языки сообщений бота. Только русский: LANGS=ru. Русский и узбекский: LANGS=ru,uz
LANGS = [l for l in (x.strip() for x in os.getenv("LANGS", "ru").split(",")) if l in ("ru", "uz")] or ["ru"]


# ───────────────────────────── Тарифы ─────────────────────────────
def parse_tariffs() -> list[tuple[int, int]]:
    """TARIFFS=30:550,90:1390,180:2490  →  [(30, 550), (90, 1390), (180, 2490)]  (дней:рублей).
    Если TARIFFS не задан, работает один тариф из PRICE_RUB и SUB_DAYS (как в первой версии)."""
    raw = os.getenv("TARIFFS", "").strip()
    if not raw:
        return [(int(os.getenv("SUB_DAYS", "30")), int(os.getenv("PRICE_RUB", "550")))]
    try:
        result = []
        for part in raw.split(","):
            days_s, price_s = part.strip().split(":")
            days, price = int(days_s), int(price_s)
            if days <= 0 or price <= 0:
                raise ValueError
            result.append((days, price))
    except ValueError:
        raise SystemExit("Неверный формат TARIFFS в .env. Нужно так: TARIFFS=30:550,90:1390,180:2490")
    return sorted(result)


TARIFFS = parse_tariffs()        # [(дней, цена в ₽), ...] от короткого к длинному
TARIFF_PRICE = dict(TARIFFS)     # дней → цена

# Telegram не создаёт счета на сумму меньше эквивалента ~1 доллара. Для рубля сейчас это около 88 ₽
# (по currencies.json от Telegram), порог зависит от курса и может меняться.
MIN_INVOICE_RUB = 90


def below_minimum() -> list[tuple[int, int]]:
    return [(d, p) for d, p in TARIFFS if p < MIN_INVOICE_RUB]


def period_label(days: int, lang: str) -> str:
    if days % 30 == 0:
        n = days // 30
        return f"{n} мес." if lang == "ru" else f"{n} oy"
    return f"{days} дн." if lang == "ru" else f"{days} kun"


def discount_pct(days: int, price: int) -> int:
    """Скидка относительно цены за день самого короткого тарифа."""
    base_days, base_price = TARIFFS[0]
    full = base_price / base_days * days
    return round((1 - price / full) * 100) if full > 0 else 0


def tariff_tail(days: int, price: int) -> str:
    d = discount_pct(days, price)
    return f" (−{d}%)" if d >= 1 else ""


def tariffs_text(lang: str) -> str:
    return "\n".join(f"• {period_label(d, lang)} — {p} ₽{tariff_tail(d, p)}" for d, p in TARIFFS)


def days_from_payload(payload: str) -> int:
    """payload счёта имеет вид sub:90. Если не разобрать, даём самый короткий срок."""
    try:
        kind, value = payload.split(":")
        if kind == "sub" and int(value) > 0:
            return int(value)
    except ValueError:
        pass
    return TARIFFS[0][0]


# ───────────────────────────── Тексты ─────────────────────────────
# Каждый текст хранится на двух языках. Какие из них показывать, решает LANGS в .env.
TEXTS = {
    "start": {
        "ru": """Привет! 👋 Это бот канала <b>iBloger</b>.

Здесь ты научишься вести блог с нуля: от страха камеры до первых денег с контента.

<b>Что внутри:</b>
1. Пошаговые уроки голосом и текстом
2. Разборы реальных роликов
3. Задания с личной обратной связью

💳 <b>Подписка:</b>
{tariffs}

Автосписания нет: перед окончанием я напомню о продлении.""",
        "uz": """Salom! 👋 Bu <b>iBloger</b> kanalining boti.

Bu yerda blogni noldan yuritishni o'rganasiz: kamera qo'rquvidan kontentdan birinchi daromadgacha.

<b>Kanalda nimalar bor:</b>
1. Ovozli va matnli bosqichma-bosqich darslar
2. Haqiqiy videolar tahlili
3. Shaxsiy fikr-mulohaza bilan topshiriqlar

💳 <b>Obuna:</b>
{tariffs}

Avtomatik yechib olinmaydi: tugashidan oldin uzaytirishni eslataman.""",
    },
    "paid_ok": {
        "ru": """Оплата прошла ✅
Доступ открыт до <b>{date}</b>.

Ссылка в канал:
{link}

Нажмите на неё и отправьте заявку на вступление. Я одобрю её автоматически.""",
        "uz": """To'lov qabul qilindi ✅
Kirish <b>{date}</b> gacha ochiq.

Kanalga havola:
{link}

Uni bosing va qo'shilish uchun ariza yuboring. Men uni avtomatik tasdiqlayman.""",
    },
    "paid_no_link": {
        "ru": """Оплата прошла ✅ Доступ открыт до <b>{date}</b>, но ссылку создать не получилось. Автор уже уведомлён.
Через несколько минут нажмите «Моя подписка» → «Получить ссылку». Если не получится, напишите через «Задать вопрос».""",
        "uz": """To'lov qabul qilindi ✅ Kirish <b>{date}</b> gacha ochiq, lekin havolani yaratib bo'lmadi. Muallifga xabar berildi.
Bir necha daqiqadan keyin «Mening obunam» → «Havola» tugmasini bosing.""",
    },
    "me_active": {
        "ru": "Подписка активна до <b>{date}</b> ✅",
        "uz": "Obuna <b>{date}</b> gacha faol ✅",
    },
    "me_none": {
        "ru": "Активной подписки нет.",
        "uz": "Faol obuna yo'q.",
    },
    "link_text": {
        "ru": "Ссылка в канал:\n{link}\n\nНажмите на неё и отправьте заявку на вступление, я одобрю её автоматически.",
        "uz": "Kanalga havola:\n{link}\n\nUni bosing va qo'shilish uchun ariza yuboring, men uni avtomatik tasdiqlayman.",
    },
    "link_fail": {
        "ru": "Не получилось создать ссылку. Напишите автору через «Задать вопрос».",
        "uz": "Havolani yaratib bo'lmadi. Muallifga «Savol» orqali yozing.",
    },
    "remind": {
        "ru": "⏰ Подписка на iBloger заканчивается <b>{date}</b>. Продлите её, чтобы не потерять доступ.",
        "uz": "⏰ iBloger obunasi <b>{date}</b> da tugaydi. Kirishni yo'qotmaslik uchun uzaytiring.",
    },
    "expired": {
        "ru": "Срок подписки закончился, доступ к каналу закрыт. Вернуться можно в любой момент: оплатите заново 👇",
        "uz": "Obuna muddati tugadi, kanalga kirish yopildi. Istalgan vaqtda qaytishingiz mumkin: qayta to'lang 👇",
    },
    "ask": {
        "ru": "Напишите свой вопрос одним сообщением, я передам его автору.",
        "uz": "Savolingizni bitta xabarda yozing, men uni muallifga yetkazaman.",
    },
    "ask_sent": {
        "ru": "Спасибо! Ответ придёт сюда, в этот чат.",
        "uz": "Rahmat! Javob shu chatga keladi.",
    },
    "join_declined": {
        "ru": "Для вступления в канал нужна активная подписка. Оформить её можно в боте @{bot}.",
        "uz": "Kanalga qo'shilish uchun faol obuna kerak. Uni @{bot} botida rasmiylashtirish mumkin.",
    },
    "invoice_fail": {
        "ru": "Не получилось создать счёт. Автор уже уведомлён. Попробуйте позже или напишите через «Задать вопрос».",
        "uz": "Hisob yaratib bo'lmadi. Muallifga xabar berildi. Keyinroq urinib ko'ring yoki «Savol» orqali yozing.",
    },
    "choose": {
        "ru": "Выберите срок подписки 👇",
        "uz": "Obuna muddatini tanlang 👇",
    },
}

# Подписи кнопок
BUTTONS = {
    "subscribe": {"ru": "🚀 Вступить в канал", "uz": "🚀 Kanalga qo'shilish"},
    "mine": {"ru": "📋 Моя подписка", "uz": "📋 Mening obunam"},
    "ask": {"ru": "❓ Задать вопрос", "uz": "❓ Savol"},
    "pay": {"ru": "💳 Оплатить", "uz": "💳 To'lash"},
    "link": {"ru": "🔗 Получить ссылку", "uz": "🔗 Havola"},
    "renew": {"ru": "💳 Продлить", "uz": "💳 Uzaytirish"},
    "menu": {"ru": "🏠 Главное меню", "uz": "🏠 Bosh menyu"},
}

LONG_TEXTS = {"start", "paid_ok", "paid_no_link"}  # между языками в них ставится линия


def T(key: str, **kw) -> str:
    """Текст на всех включённых языках. Язык-специфичное (список тарифов) подставляется само."""
    parts = [TEXTS[key][lang].format(tariffs=tariffs_text(lang), **kw) for lang in LANGS]
    return ("\n\n———\n\n" if key in LONG_TEXTS else "\n\n").join(parts)


def B(key: str) -> str:
    return " · ".join(BUTTONS[key][lang] for lang in LANGS)


# ───────────────────────────── Клавиатуры ─────────────────────────────
def menu_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=B("menu"), callback_data="menu")]


def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[menu_row()])


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=B("subscribe"), callback_data="join")],
        [InlineKeyboardButton(text=B("mine"), callback_data="me")],
        [InlineKeyboardButton(text=B("ask"), callback_data="ask")],
    ])


def kb_pay() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=B("pay"), callback_data="buy")],
        menu_row(),
    ])


def kb_tariffs() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{' · '.join(period_label(d, lang) for lang in LANGS)} — {p} ₽{tariff_tail(d, p)}",
            callback_data=f"buy:{d}",
        )]
        for d, p in TARIFFS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows + [menu_row()])


def kb_link() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=B("link"), callback_data="link")],
        [InlineKeyboardButton(text=B("renew"), callback_data="buy")],
        menu_row(),
    ])


# ───────────────────────────── Вспомогательное ─────────────────────────────
router = Router()


class Ask(StatesGroup):
    waiting = State()


async def dbx(fn, *args):
    """Запускает синхронную функцию базы в отдельном потоке."""
    return await asyncio.to_thread(fn, *args)


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y")


async def notify_admin(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception:
        logging.exception("Не удалось написать админу")


async def new_invite_link(bot: Bot) -> str:
    """Создаёт общую ссылку с заявками на вступление и запоминает её.
    Без срока и лимита: войти по ней может только тот, кому бот одобрит заявку."""
    link = await bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        name="iBloger: доступ по подписке",
        creates_join_request=True,
    )
    await dbx(db.set_setting, "invite_link", link.invite_link)
    return link.invite_link


async def get_invite_link(bot: Bot) -> str:
    return await dbx(db.get_setting, "invite_link") or await new_invite_link(bot)


async def kick(bot: Bot, user_id: int) -> bool:
    """Убирает человека из канала так, чтобы он мог вернуться после новой оплаты."""
    if user_id == ADMIN_ID:
        return True
    try:
        await bot.ban_chat_member(CHANNEL_ID, user_id)
        await bot.unban_chat_member(CHANNEL_ID, user_id, only_if_is_banned=True)
        return True
    except TelegramBadRequest as e:
        logging.warning("Не удалось убрать %s из канала: %s", user_id, e)
        return False


async def send_access(bot: Bot, user_id: int, until: int) -> None:
    try:
        url = await get_invite_link(bot)
    except Exception:
        logging.exception("Не удалось создать ссылку для %s", user_id)
        await bot.send_message(user_id, T("paid_no_link", date=fmt_date(until)), reply_markup=kb_link())
        await notify_admin(
            bot,
            f"⚠️ Пользователь {user_id} оплатил, но ссылку в канал создать не удалось. "
            "Проверь, что бот админ канала с правом приглашать участников. "
            "Пользователь сможет сам получить ссылку кнопкой «Моя подписка».",
        )
        return
    await bot.send_message(user_id, T("paid_ok", date=fmt_date(until), link=url), reply_markup=kb_link())


# ───────────────────────────── Пользователь ─────────────────────────────
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(T("start"), reply_markup=kb_main())


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    """Кнопка «Главное меню»: сбрасывает ожидание вопроса и показывает стартовое сообщение."""
    await cb.answer()
    await state.clear()
    await cb.message.answer(T("start"), reply_markup=kb_main())


async def send_invoice(bot: Bot, chat_id: int, days: int, price: int) -> None:
    extra = {}
    if SEND_RECEIPT:
        # Данные чека для ЮKassa (54-ФЗ). Включай, только если ЮKassa просит чеки.
        extra = {
            "need_email": True,
            "send_email_to_provider": True,
            "provider_data": json.dumps({
                "receipt": {"items": [{
                    "description": f"Доступ к каналу iBloger на {days} дн.",
                    "quantity": "1.00",
                    "amount": {"value": f"{price:.2f}", "currency": "RUB"},
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }]}
            }, ensure_ascii=False),
        }
    try:
        await bot.send_invoice(
            chat_id=chat_id,
            title="Подписка iBloger",
            description=f"Доступ к закрытому каналу iBloger на {days} дн.",
            payload=f"sub:{days}",
            provider_token=PROVIDER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label=f"Подписка на {days} дн.", amount=price * 100)],
            **extra,
        )
    except TelegramBadRequest as e:
        logging.exception("Не удалось создать счёт на %s ₽ (%s дн.)", price, days)
        await bot.send_message(chat_id, T("invoice_fail"), reply_markup=kb_menu())
        hint = (f" Сумма меньше минимума Telegram (около {MIN_INVOICE_RUB} ₽)." if price < MIN_INVOICE_RUB else "")
        await notify_admin(bot, f"⚠️ Не удалось создать счёт на {price} ₽ ({days} дн.): {e}.{hint}")


@router.callback_query(F.data == "join")
async def cb_join(cb: CallbackQuery, bot: Bot):
    """Кнопка «Вступить в канал»: если подписка уже оплачена, даёт ссылку, иначе предлагает выбрать тариф."""
    sub = await dbx(db.get_sub, cb.from_user.id)
    if sub and sub["active"] and sub["paid_until"] > int(time.time()):
        await cb_link(cb, bot)
    else:
        await cb_buy(cb, bot)


@router.callback_query(F.data == "buy")
async def cb_buy(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    if len(TARIFFS) == 1:  # один тариф: сразу счёт
        await send_invoice(bot, cb.from_user.id, *TARIFFS[0])
        return
    await cb.message.answer(T("choose"), reply_markup=kb_tariffs())


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy_tariff(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    try:
        days = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        return
    price = TARIFF_PRICE.get(days)
    if price is None:  # кнопка из старого сообщения: тарифа с таким сроком уже нет
        await cb.message.answer(T("choose"), reply_markup=kb_tariffs())
        return
    await send_invoice(bot, cb.from_user.id, days, price)


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(m: Message, bot: Bot):
    p = m.successful_payment
    uid = m.from_user.id
    is_new = await dbx(db.add_payment, uid, p.total_amount, p.currency, p.telegram_payment_charge_id)
    if not is_new:
        return  # этот платёж уже обработан
    days = days_from_payload(p.invoice_payload)
    until = await dbx(db.extend, uid, m.from_user.username, days)
    await send_access(bot, uid, until)
    await notify_admin(
        bot,
        f"💰 Оплата {p.total_amount / 100:.0f} {p.currency} от {escape(m.from_user.full_name)} "
        f"(id {uid}). Тариф {days} дн., подписка до {fmt_date(until)}.",
    )


@router.callback_query(F.data == "me")
async def cb_me(cb: CallbackQuery):
    await cb.answer()
    sub = await dbx(db.get_sub, cb.from_user.id)
    if sub and sub["active"] and sub["paid_until"] > int(time.time()):
        await cb.message.answer(T("me_active", date=fmt_date(sub["paid_until"])), reply_markup=kb_link())
    else:
        await cb.message.answer(T("me_none"), reply_markup=kb_pay())


@router.callback_query(F.data == "link")
async def cb_link(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    sub = await dbx(db.get_sub, cb.from_user.id)
    if not (sub and sub["active"] and sub["paid_until"] > int(time.time())):
        await cb.message.answer(T("me_none"), reply_markup=kb_pay())
        return
    try:
        url = await get_invite_link(bot)
    except Exception:
        logging.exception("Не удалось создать ссылку для %s", cb.from_user.id)
        await cb.message.answer(T("link_fail"), reply_markup=kb_menu())
        return
    await cb.message.answer(T("link_text", link=url), reply_markup=kb_menu())


@router.callback_query(F.data == "ask")
async def cb_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Ask.waiting)
    await cb.message.answer(T("ask"), reply_markup=kb_menu())


@router.message(Ask.waiting)
async def on_question(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    u = m.from_user
    who = escape(u.full_name) + (f" (@{escape(u.username)})" if u.username else "")
    head = await bot.send_message(
        ADMIN_ID,
        f"❓ Вопрос от {who}, id <code>{u.id}</code>.\nОтветь реплаем на это сообщение или на сообщение ниже.",
    )
    copy = await m.copy_to(ADMIN_ID)
    for msg_id in (head.message_id, copy.message_id):
        await dbx(db.save_question, msg_id, u.id)
    await m.answer(T("ask_sent"), reply_markup=kb_menu())


# ───────────────────────────── Админ ─────────────────────────────
@router.message(Command("grant"), F.from_user.id == ADMIN_ID)
async def cmd_grant(m: Message, command: CommandObject, bot: Bot):
    """/grant <id> <дней> — выдать доступ вручную (оплата картой, тест). Дни могут быть дробными: 0.002."""
    try:
        uid_s, days_s = (command.args or "").split()
        uid, days = int(uid_s), float(days_s)
    except ValueError:
        await m.answer("Формат: /grant 123456789 30")
        return
    until = await dbx(db.extend, uid, None, days)
    try:
        await send_access(bot, uid, until)
    except (TelegramForbiddenError, TelegramBadRequest):
        await m.answer("Подписка записана, но пользователь не начинал диалог с ботом, написать ему не могу.")
        return
    await m.answer(f"Готово: {uid} до {fmt_date(until)}.")


@router.message(Command("revoke"), F.from_user.id == ADMIN_ID)
async def cmd_revoke(m: Message, command: CommandObject, bot: Bot):
    """/revoke <id> — закрыть доступ сразу (например, после возврата денег)."""
    try:
        uid = int((command.args or "").strip())
    except ValueError:
        await m.answer("Формат: /revoke 123456789")
        return
    ok = await kick(bot, uid)
    await dbx(db.deactivate, uid, True)
    await m.answer("Доступ закрыт." if ok else "Подписка снята, но из канала убрать не удалось: сделай это вручную.")


@router.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def cmd_stats(m: Message):
    s = await dbx(db.stats, int(time.time()))
    await m.answer(
        f"Активных подписок: {s['active']}\n"
        f"Закончатся за 7 дней: {s['soon']}\n"
        f"Платежей через бота: {s['payments']} на {s['total_kopecks'] / 100:.0f} ₽"
    )


@router.message(F.from_user.id == ADMIN_ID, F.reply_to_message)
async def admin_reply(m: Message):
    """Ответ реплаем на вопрос подписчика уходит ему (текст, голосовое, фото — что угодно)."""
    uid = await dbx(db.get_question_user, m.reply_to_message.message_id)
    if not uid:
        return
    try:
        await m.copy_to(uid)
        await m.reply("Отправлено ✅")
    except TelegramForbiddenError:
        await m.reply("Пользователь заблокировал бота.")


@router.chat_join_request(F.chat.id == CHANNEL_ID)
async def on_join_request(req: ChatJoinRequest, bot: Bot):
    """Одобряем заявку только тем, у кого есть активная подписка (и тебе)."""
    uid = req.from_user.id
    sub = await dbx(db.get_sub, uid)
    has_access = uid == ADMIN_ID or bool(sub and sub["active"] and sub["paid_until"] > int(time.time()))
    if has_access:
        await bot.approve_chat_join_request(req.chat.id, uid)
        return
    await bot.decline_chat_join_request(req.chat.id, uid)
    try:
        me = await bot.me()
        await bot.send_message(req.user_chat_id, T("join_declined", bot=me.username))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@router.message(Command("newlink"), F.from_user.id == ADMIN_ID)
async def cmd_newlink(m: Message, bot: Bot):
    """/newlink — создать новую общую ссылку в канал (старая отзывается). Нужно, если ссылка утекла."""
    old = await dbx(db.get_setting, "invite_link")
    url = await new_invite_link(bot)
    if old:
        try:
            await bot.revoke_chat_invite_link(CHANNEL_ID, old)
        except TelegramBadRequest:
            pass
    await m.answer(f"Новая ссылка в канал:\n{url}\nСтарая отозвана.")


STATUS_RU = {
    "creator": "владелец канала",
    "administrator": "администратор",
    "member": "в канале",
    "restricted": "в канале с ограничениями",
    "left": "не в канале (вышел или не вступал)",
    "kicked": "ЗАБЛОКИРОВАН в канале (поэтому ссылки не работают, помогает /unban)",
}


def member_status(member) -> str:
    st = member.status
    return str(getattr(st, "value", st))


def parse_uid(args: str | None) -> int | None:
    try:
        return int((args or "").strip())
    except ValueError:
        return None


@router.message(Command("check"), F.from_user.id == ADMIN_ID)
async def cmd_check(m: Message, command: CommandObject, bot: Bot):
    """/check [id] — диагностика: какой канал подключён, есть ли права у бота, жива ли ссылка,
    в каком статусе человек (в канале / не в канале / заблокирован)."""
    try:
        chat = await bot.get_chat(CHANNEL_ID)
    except TelegramBadRequest as e:
        await m.answer(f"❌ Не вижу чат {CHANNEL_ID}: {escape(str(e))}\n"
                       "Проверь CHANNEL_ID в .env и что бот добавлен в этот канал.")
        return
    lines = [f"Чат: <b>{escape(chat.title or '—')}</b>, тип: {chat.type}, id <code>{CHANNEL_ID}</code>"]

    me = await bot.me()
    try:
        bm = await bot.get_chat_member(CHANNEL_ID, me.id)
        status = member_status(bm)
        extra = ""
        if status == "administrator":
            yes = lambda v: "да" if v else "НЕТ"
            extra = (f" (приглашать: {yes(getattr(bm, 'can_invite_users', False))}, "
                     f"блокировать: {yes(getattr(bm, 'can_restrict_members', False))})")
        lines.append(f"Бот @{me.username} в чате: {STATUS_RU.get(status, status)}{extra}")
    except TelegramBadRequest as e:
        lines.append(f"❌ Статус бота получить не удалось: {escape(str(e))}")

    link = await dbx(db.get_setting, "invite_link")
    if link:
        try:  # те же параметры, что при создании, поэтому ссылка не меняется
            info = await bot.edit_chat_invite_link(CHANNEL_ID, link, name="iBloger: доступ по подписке",
                                                   creates_join_request=True)
            state = "ОТОЗВАНА" if info.is_revoked else "действует"
            lines.append(f"Ссылка в базе {link}: {state}; заявки на вступление: "
                         f"{'да' if info.creates_join_request else 'НЕТ'}; срок: "
                         f"{fmt_date(int(info.expire_date.timestamp())) if info.expire_date else 'без срока'}")
        except TelegramBadRequest as e:
            lines.append(f"❌ Ссылка в базе недействительна: {escape(str(e))}. Выполни /newlink")
    else:
        lines.append("Ссылки в базе пока нет: она создастся при первой оплате или командой /newlink")

    if command.args:
        uid = parse_uid(command.args)
        if uid is None:
            await m.answer("Формат: /check 123456789")
            return
        try:
            um = await bot.get_chat_member(CHANNEL_ID, uid)
            st = member_status(um)
            lines.append(f"Человек {uid}: {STATUS_RU.get(st, st)}")
        except TelegramBadRequest as e:
            lines.append(f"Человек {uid}: статус получить не удалось ({escape(str(e))})")
        sub = await dbx(db.get_sub, uid)
        if sub and sub["active"] and sub["paid_until"] > int(time.time()):
            lines.append(f"В базе: подписка активна до {fmt_date(sub['paid_until'])}")
        else:
            lines.append("В базе: активной подписки нет")
    await m.answer("\n".join(lines))


@router.message(Command("unban"), F.from_user.id == ADMIN_ID)
async def cmd_unban(m: Message, command: CommandObject, bot: Bot):
    """/unban <id> — снять блокировку в канале (после неё человек снова может вступить)."""
    uid = parse_uid(command.args)
    if uid is None:
        await m.answer("Формат: /unban 123456789")
        return
    try:
        await bot.unban_chat_member(CHANNEL_ID, uid, only_if_is_banned=True)
    except TelegramBadRequest as e:
        await m.answer(f"Не получилось: {escape(str(e))}")
        return
    await m.answer(f"Блокировка снята с {uid}. Теперь он может вступить по ссылке, если подписка активна.")


GEO_SERVICES = ("https://ipinfo.io/json", "https://ipapi.co/json/")

COUNTRIES_RU = {
    "RU": "Россия", "NL": "Нидерланды", "DE": "Германия", "FI": "Финляндия", "US": "США", "KZ": "Казахстан",
    "UZ": "Узбекистан", "PL": "Польша", "FR": "Франция", "GB": "Великобритания", "SE": "Швеция",
    "LT": "Литва", "LV": "Латвия", "EE": "Эстония", "AM": "Армения", "TR": "Турция", "CH": "Швейцария",
    "UA": "Украина", "BY": "Беларусь", "GE": "Грузия", "KG": "Киргизия", "TJ": "Таджикистан",
    "AE": "ОАЭ", "CZ": "Чехия", "AT": "Австрия", "IT": "Италия", "ES": "Испания", "CA": "Канада",
}


async def fetch_json(url: str) -> dict | None:
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def lookup_server_location() -> dict | None:
    """Определяет внешний IP бота и его страну по открытым сервисам (второй — запасной)."""
    for url in GEO_SERVICES:
        data = await fetch_json(url)
        if isinstance(data, dict) and data.get("ip"):
            return data
    return None


def describe_server(g: dict) -> str:
    code = str(g.get("country_code") or g.get("country") or "").upper()
    country = COUNTRIES_RU.get(code) or g.get("country_name") or code or "не определена"
    place = ", ".join(x for x in (g.get("city"), g.get("region")) if x)
    lines = [
        f"🌍 Внешний IP бота: <code>{escape(str(g.get('ip')))}</code>",
        f"Страна по IP: <b>{escape(str(country))}</b>" + (f" ({escape(code)})" if code else ""),
    ]
    if place:
        lines.append(f"Город: {escape(place)}")
    if g.get("org"):
        lines.append(f"Провайдер: {escape(str(g['org']))}")
    lines.append("Страна определяется по базе IP-адресов: обычно это страна дата-центра, но возможны неточности.")
    return "\n".join(lines)


@router.message(Command("where"), F.from_user.id == ADMIN_ID)
async def cmd_where(m: Message):
    """/where — где физически работает бот: внешний IP, страна, город, провайдер."""
    geo = await lookup_server_location()
    if not geo:
        await m.answer("Не удалось определить: сервисы геолокации IP не ответили. Попробуй через минуту.")
        return
    await m.answer(describe_server(geo))


# ───────────────────────────── Фоновая проверка подписок ─────────────────────────────
async def watcher(bot: Bot) -> None:
    while True:
        try:
            now = int(time.time())

            for s in await dbx(db.due_reminders, now, REMIND_DAYS * 86400):
                try:
                    await bot.send_message(s["user_id"], T("remind", date=fmt_date(s["paid_until"])),
                                           reply_markup=kb_pay())
                except (TelegramForbiddenError, TelegramBadRequest):
                    pass
                await dbx(db.mark_reminded, s["user_id"], s["paid_until"])

            for s in await dbx(db.expired, now):
                uid = s["user_id"]
                removed = await kick(bot, uid)
                await dbx(db.deactivate, uid)
                try:
                    await bot.send_message(uid, T("expired"), reply_markup=kb_pay())
                except (TelegramForbiddenError, TelegramBadRequest):
                    pass
                if not removed:
                    await notify_admin(
                        bot,
                        f"⚠️ Подписка пользователя {uid} закончилась, но убрать его из канала не удалось. "
                        "Проверь права бота (блокировка участников) или удали его вручную.",
                    )
        except Exception:
            logging.exception("Ошибка в проверке подписок")
        await asyncio.sleep(CHECK_EVERY_SEC)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    db.init(DB_FILE)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    watch_task = asyncio.create_task(watcher(bot))  # noqa: F841 (держим ссылку, чтобы задачу не удалил сборщик мусора)
    await bot.delete_webhook()
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    for days, price in below_minimum():
        logging.warning("Тариф %s дн. за %s ₽ меньше минимума Telegram (около %s ₽): счёт создаться не сможет. "
                        "Для проверки с настоящими деньгами ставь от %s ₽.", days, price, MIN_INVOICE_RUB, MIN_INVOICE_RUB)
    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
