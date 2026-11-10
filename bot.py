"""NESEREGASHOP — Telegram Stars digital course shop, aiogram 3."""
import asyncio
import html
import logging
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LabeledPrice, Message, PreCheckoutQuery)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
SUPPORT = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
CHANNEL = "https://t.me/NESEREGASHOP"
DB_PATH = Path(os.getenv("DB_PATH", "shop.sqlite3"))
# The original course was advertised for 199 RUB. Stars are a different currency.
# Set COURSE_STARS explicitly; never silently convert RUB to Stars.
DEFAULT_STARS = int(os.getenv("COURSE_STARS", "0"))
router = Router()
write_lock = asyncio.Lock()


def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def initialize():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(db()) as con:
        con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT, joined_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, description TEXT NOT NULL,
            stars INTEGER NOT NULL DEFAULT 0 CHECK(stars>=0),
            file_id TEXT, file_name TEXT, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            stars INTEGER NOT NULL, currency TEXT NOT NULL,
            telegram_charge_id TEXT NOT NULL UNIQUE,
            provider_charge_id TEXT, paid_at TEXT NOT NULL,
            refunded INTEGER NOT NULL DEFAULT 0);
        """)
        if not con.execute("SELECT 1 FROM products").fetchone():
            con.execute("""INSERT INTO products(title,description,stars) VALUES(?,?,?)""", (
                "FunPay — с нуля до первых продаж",
                "Как устроена площадка; выбор товара; анализ конкурентов и цен; "
                "оформление объявлений; общение с покупателями; безопасность и выполнение заказов. "
                "Обучающий курс: доход не гарантируется.", DEFAULT_STARS))
        con.commit()


def fetchone(query, args=()):
    with closing(db()) as con:
        return con.execute(query, args).fetchone()


def fetchall(query, args=()):
    with closing(db()) as con:
        return con.execute(query, args).fetchall()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def esc(s):
    return html.escape(str(s or ""))


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def home_kb(user_id):
    rows = [[btn("🛍 Каталог", "catalog")], [btn("📦 Мои покупки", "purchases")],
            [btn("💬 Поддержка", "support")],
            [InlineKeyboardButton(text="📢 Наш Telegram-канал", url=CHANNEL)]]
    if is_admin(user_id):
        rows.append([btn("⚙️ Админ-панель", "admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(where="menu"):
    return kb([btn("⬅️ Назад", where)])


class AddProduct(StatesGroup):
    title = State()
    description = State()
    price = State()
    file = State()


class SetPrice(StatesGroup):
    value = State()


class SetFile(StatesGroup):
    file = State()


async def remember(user):
    async with write_lock:
        with closing(db()) as con:
            con.execute("INSERT INTO users(id,username,joined_at) VALUES(?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET username=excluded.username",
                        (user.id, user.username, datetime.now(timezone.utc).isoformat()))
            con.commit()


async def edit(callback, text, markup):
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            raise
    await callback.answer()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await remember(message.from_user)
    await message.answer("<b>🛍 NESEREGASHOP</b>\nМагазин обучающих материалов.\n\nВыбери раздел:",
                         reply_markup=home_kb(message.from_user.id), parse_mode="HTML")


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "<b>🛍 NESEREGASHOP</b>\nВыбери раздел:", home_kb(c.from_user.id))


@router.callback_query(F.data == "catalog")
async def catalog(c: CallbackQuery):
    items = fetchall("SELECT * FROM products WHERE active=1 ORDER BY id")
    rows = [[btn(f"📘 {p['title'][:42]}", f"product:{p['id']}")] for p in items]
    rows.append([btn("⬅️ В меню", "menu")])
    await edit(c, "<b>🛍 Каталог курсов</b>\nВыбери материал:" if items else "Каталог пока пуст.",
               InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("product:"))
async def product(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = fetchone("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not p:
        await c.answer("Товар недоступен", show_alert=True)
        return
    purchased = fetchone("SELECT id FROM orders WHERE user_id=? AND product_id=? AND refunded=0",
                         (c.from_user.id, pid))
    price = f"{p['stars']} ⭐" if p['stars'] else "не установлена"
    text = f"<b>📘 {esc(p['title'])}</b>\n\n{esc(p['description'])}\n\n<b>Цена: {price}</b>"
    rows = []
    if purchased:
        rows.append([btn("📥 Получить ещё раз", f"deliver:{pid}")])
    elif p["stars"] > 0 and p["file_id"]:
        rows.append([btn("⭐ Оплатить", f"buy:{pid}")])
    else:
        text += "\n\n<i>Продажи пока не открыты: администратор настраивает цену или материал.</i>"
    rows.append([btn("⬅️ Каталог", "catalog")])
    await edit(c, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery, bot: Bot):
    pid = int(c.data.split(":")[1])
    p = fetchone("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not p or p["stars"] <= 0 or not p["file_id"]:
        await c.answer("Товар пока нельзя купить", show_alert=True)
        return
    if fetchone("SELECT id FROM orders WHERE user_id=? AND product_id=? AND refunded=0",
                (c.from_user.id, pid)):
        await c.answer("Уже куплено — открой «Мои покупки»", show_alert=True)
        return
    await remember(c.from_user)
    # Payload binds invoice to this buyer and product. Payment is checked again in pre-checkout.
    payload = f"shop:{pid}:{c.from_user.id}:{uuid.uuid4().hex[:12]}"
    await bot.send_invoice(chat_id=c.from_user.id, title=p["title"][:32],
                           description=p["description"][:255], payload=payload,
                           currency="XTR", prices=[LabeledPrice(label=p["title"][:32], amount=p["stars"])],
                           provider_token="")
    await c.answer()


def decode_payload(payload):
    try:
        prefix, pid, uid, nonce = payload.split(":")
        if prefix != "shop" or not nonce or len(nonce) > 32:
            return None
        return int(pid), int(uid)
    except (ValueError, AttributeError):
        return None


@router.pre_checkout_query()
async def precheckout(q: PreCheckoutQuery, bot: Bot):
    parsed = decode_payload(q.invoice_payload)
    if not parsed or parsed[1] != q.from_user.id or q.currency != "XTR":
        await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Некорректный заказ")
        return
    p = fetchone("SELECT * FROM products WHERE id=? AND active=1", (parsed[0],))
    if not p or not p["file_id"] or p["stars"] <= 0 or p["stars"] != q.total_amount:
        await bot.answer_pre_checkout_query(q.id, ok=False,
                                            error_message="Товар или цена изменились. Создай новый заказ.")
        return
    if fetchone("SELECT id FROM orders WHERE user_id=? AND product_id=? AND refunded=0",
                (q.from_user.id, parsed[0])):
        await bot.answer_pre_checkout_query(q.id, ok=False, error_message="Товар уже приобретён")
        return
    await bot.answer_pre_checkout_query(q.id, ok=True)


async def send_material(bot: Bot, user_id: int, product_id: int):
    p = fetchone("SELECT title,file_id FROM products WHERE id=?", (product_id,))
    if not p or not p["file_id"]:
        await bot.send_message(user_id, "Материал временно недоступен. Напиши в поддержку.")
        return
    await bot.send_document(user_id, document=p["file_id"], caption=f"📥 Ваш курс: {p['title']}")


@router.message(F.successful_payment)
async def payment_success(message: Message, bot: Bot):
    payment = message.successful_payment
    parsed = decode_payload(payment.invoice_payload)
    if not parsed or parsed[1] != message.from_user.id or payment.currency != "XTR":
        logging.error("Unexpected payment payload: %s", payment.telegram_payment_charge_id)
        await message.answer("Платёж получен. Свяжись с поддержкой для получения материала.")
        return
    pid, uid = parsed
    await remember(message.from_user)
    # The unique charge ID prevents duplicate order insertion on retried updates.
    async with write_lock:
        with closing(db()) as con:
            p = con.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                logging.error("Paid product %s missing", pid)
                await message.answer("Платёж получен. Напиши в поддержку для выдачи материала.")
                return
            existing = con.execute("SELECT id FROM orders WHERE telegram_charge_id=?",
                                   (payment.telegram_payment_charge_id,)).fetchone()
            if existing:
                return
            con.execute("""INSERT INTO orders(user_id,product_id,stars,currency,
                           telegram_charge_id,provider_charge_id,paid_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (uid, pid, payment.total_amount, payment.currency,
                         payment.telegram_payment_charge_id,
                         payment.provider_payment_charge_id,
                         datetime.now(timezone.utc).isoformat()))
            con.commit()
    await message.answer("✅ Оплата подтверждена! Отправляю материал. Он также доступен в «Моих покупках».")
    try:
        await send_material(bot, uid, pid)
    except Exception:
        logging.exception("Delivery failed for product=%s user=%s", pid, uid)
        await message.answer("Не удалось отправить файл. Попробуй повторную выдачу в «Моих покупках» или напиши в поддержку.")


@router.callback_query(F.data == "purchases")
async def purchases(c: CallbackQuery):
    orders = fetchall("""SELECT DISTINCT p.id,p.title FROM orders o JOIN products p ON p.id=o.product_id
                         WHERE o.user_id=? AND o.refunded=0 ORDER BY p.id""", (c.from_user.id,))
    rows = [[btn(f"📥 {o['title'][:42]}", f"deliver:{o['id']}")] for o in orders]
    rows.append([btn("⬅️ В меню", "menu")])
    await edit(c, "<b>📦 Мои покупки</b>\nНажми на курс, чтобы получить файл снова."
               if orders else "<b>📦 Мои покупки</b>\nПокупок пока нет.",
               InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("deliver:"))
async def deliver(c: CallbackQuery, bot: Bot):
    pid = int(c.data.split(":")[1])
    owned = fetchone("SELECT id FROM orders WHERE user_id=? AND product_id=? AND refunded=0",
                     (c.from_user.id, pid))
    if not owned:
        await c.answer("Нет подтверждённой покупки", show_alert=True)
        return
    await c.answer("Отправляю материал")
    try:
        await send_material(bot, c.from_user.id, pid)
    except Exception:
        logging.exception("Repeat delivery failed")
        await bot.send_message(c.from_user.id, "Не удалось отправить файл. Напиши в поддержку.")


@router.callback_query(F.data == "support")
async def support(c: CallbackQuery):
    if SUPPORT:
        markup = kb([InlineKeyboardButton(text="💬 Написать", url=f"https://t.me/{SUPPORT}")],
                    [btn("⬅️ В меню", "menu")])
        await edit(c, "<b>💬 Поддержка</b>\nСвяжись с нами по кнопке ниже.", markup)
    else:
        await edit(c, "<b>💬 Поддержка</b>\nКонтакт ещё не настроен.", back())


@router.message(Command("paysupport", "terms"))
async def terms(m: Message):
    await m.answer("NESEREGASHOP продаёт цифровые учебные материалы за Telegram Stars. "
                   "После подтверждения оплаты файл автоматически приходит в чат. "
                   "По вопросам оплаты, возврата и доступа обращайтесь в поддержку: "
                   + (f"@{SUPPORT}" if SUPPORT else "контакт пока не настроен"))


def admin_kb():
    return kb([btn("➕ Добавить товар", "admin:add")],
              [btn("📚 Управлять товарами", "admin:products")],
              [btn("📊 Статистика", "admin:stats")],
              [btn("⬅️ В меню", "menu")])


@router.callback_query(F.data == "admin")
async def admin(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    await edit(c, "<b>⚙️ Админ-панель</b>", admin_kb())


@router.callback_query(F.data == "admin:stats")
async def stats(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    users = fetchone("SELECT COUNT(*) n FROM users")["n"]
    orders = fetchone("SELECT COUNT(*) n,COALESCE(SUM(stars),0) total FROM orders WHERE refunded=0")
    products = fetchone("SELECT COUNT(*) n FROM products")["n"]
    await edit(c, f"<b>📊 Статистика</b>\nПользователи: {users}\nТовары: {products}\n"
               f"Оплаченные заказы: {orders['n']}\nПолучено: {orders['total']} ⭐ (без учёта комиссий)",
               back("admin"))


@router.callback_query(F.data == "admin:products")
async def admin_products(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    products = fetchall("SELECT * FROM products ORDER BY id")
    rows = [[btn(f"#{p['id']} {'🟢' if p['active'] else '🔴'} {p['title'][:35]}", f"admin:p:{p['id']}")]
            for p in products]
    rows.append([btn("⬅️ Админ-панель", "admin")])
    await edit(c, "<b>📚 Товары</b>\n🟢 в каталоге / 🔴 скрыт", InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("admin:p:"))
async def admin_product(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    p = fetchone("SELECT * FROM products WHERE id=?", (pid,))
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return
    markup = kb([btn("💰 Изменить цену", f"admin:price:{pid}")],
                [btn("📎 Заменить файл", f"admin:file:{pid}")],
                [btn("🔴 Скрыть" if p['active'] else "🟢 Опубликовать", f"admin:toggle:{pid}")],
                [btn("⬅️ Товары", "admin:products")])
    await edit(c, f"<b>{esc(p['title'])}</b>\nЦена: {p['stars']} ⭐\n"
               f"Файл: {'загружен' if p['file_id'] else 'не загружен'}\n"
               f"В каталоге: {'да' if p['active'] else 'нет'}", markup)


@router.callback_query(F.data.startswith("admin:toggle:"))
async def toggle(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    async with write_lock:
        with closing(db()) as con:
            con.execute("UPDATE products SET active=1-active WHERE id=?", (pid,))
            con.commit()
    await admin_product_redirect(c, pid)


async def admin_product_redirect(c, pid):
    p = fetchone("SELECT * FROM products WHERE id=?", (pid,))
    if not p:
        await c.answer("Товар не найден", show_alert=True)
        return
    await edit(c, f"<b>{esc(p['title'])}</b>\nЦена: {p['stars']} ⭐\n"
               f"Файл: {'загружен' if p['file_id'] else 'не загружен'}\n"
               f"В каталоге: {'да' if p['active'] else 'нет'}",
               kb([btn("💰 Изменить цену", f"admin:price:{pid}")],
                  [btn("📎 Заменить файл", f"admin:file:{pid}")],
                  [btn("🔴 Скрыть" if p['active'] else "🟢 Опубликовать", f"admin:toggle:{pid}")],
                  [btn("⬅️ Товары", "admin:products")]))


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Действие отменено.")


@router.callback_query(F.data == "admin:add")
async def add_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AddProduct.title)
    await c.message.answer("Название товара (до 100 символов). /cancel — отмена")
    await c.answer()


@router.message(AddProduct.title)
async def add_title(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    if not m.text or not 1 <= len(m.text.strip()) <= 100:
        await m.answer("Введи название длиной от 1 до 100 символов.")
        return
    await state.update_data(title=m.text.strip())
    await state.set_state(AddProduct.description)
    await m.answer("Введи описание (до 1000 символов).")


@router.message(AddProduct.description)
async def add_desc(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    if not m.text or not 1 <= len(m.text.strip()) <= 1000:
        await m.answer("Введи описание длиной от 1 до 1000 символов.")
        return
    await state.update_data(description=m.text.strip())
    await state.set_state(AddProduct.price)
    await m.answer("Введи цену целым числом в ⭐ Stars (например, 100).")


def valid_price(text):
    try:
        val = int(text)
        return val if 1 <= val <= 100000 else None
    except (ValueError, TypeError):
        return None


@router.message(AddProduct.price)
async def add_price(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    price = valid_price(m.text)
    if price is None:
        await m.answer("Цена должна быть целым числом от 1 до 100000 Stars.")
        return
    await state.update_data(stars=price)
    await state.set_state(AddProduct.file)
    await m.answer("Пришли файл курса как документ (PDF, ZIP и т. д.).")


@router.message(AddProduct.file, F.document)
async def add_file(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    data = await state.get_data()
    async with write_lock:
        with closing(db()) as con:
            con.execute("INSERT INTO products(title,description,stars,file_id,file_name) VALUES(?,?,?,?,?)",
                        (data["title"], data["description"], data["stars"],
                         m.document.file_id, m.document.file_name))
            con.commit()
    await state.clear()
    await m.answer("✅ Товар создан и опубликован.", reply_markup=admin_kb())


@router.callback_query(F.data.startswith("admin:price:"))
async def price_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    if not fetchone("SELECT id FROM products WHERE id=?", (pid,)):
        await c.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(SetPrice.value)
    await state.update_data(pid=pid)
    await c.message.answer("Новая цена в Stars (1–100000). /cancel — отмена")
    await c.answer()


@router.message(SetPrice.value)
async def price_set(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    price = valid_price(m.text)
    if price is None:
        await m.answer("Введи целое число от 1 до 100000.")
        return
    pid = (await state.get_data())["pid"]
    async with write_lock:
        with closing(db()) as con:
            con.execute("UPDATE products SET stars=? WHERE id=?", (price, pid))
            con.commit()
    await state.clear()
    await m.answer(f"✅ Цена товара #{pid}: {price} ⭐")


@router.callback_query(F.data.startswith("admin:file:"))
async def file_start(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Нет доступа", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    if not fetchone("SELECT id FROM products WHERE id=?", (pid,)):
        await c.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(SetFile.file)
    await state.update_data(pid=pid)
    await c.message.answer("Пришли новый файл как документ. /cancel — отмена")
    await c.answer()


@router.message(SetFile.file, F.document)
async def file_set(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    pid = (await state.get_data())["pid"]
    async with write_lock:
        with closing(db()) as con:
            con.execute("UPDATE products SET file_id=?,file_name=? WHERE id=?",
                        (m.document.file_id, m.document.file_name, pid))
            con.commit()
    await state.clear()
    await m.answer("✅ Файл обновлён. Он будет доступен и предыдущим покупателям.")


@router.message(Command("admin"))
async def admin_command(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await m.answer("⚙️ Админ-панель", reply_markup=admin_kb())


@router.message(Command("refund"))
async def refund(m: Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    # /refund <order_id> — Stars refunds require the Telegram charge ID.
    args = (m.text or "").split()
    if len(args) != 2 or not args[1].isdigit():
        await m.answer("Использование: /refund ID_заказа. Список ID: /orders")
        return
    oid = int(args[1])
    order = fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    if not order or order["refunded"]:
        await m.answer("Заказ не найден или уже возвращён.")
        return
    try:
        await bot.refund_star_payment(user_id=order["user_id"],
                                      telegram_payment_charge_id=order["telegram_charge_id"])
    except Exception:
        logging.exception("Refund failed for order %s", oid)
        await m.answer("Не удалось вернуть Stars. Проверь логи и статус платежа.")
        return
    async with write_lock:
        with closing(db()) as con:
            con.execute("UPDATE orders SET refunded=1 WHERE id=?", (oid,))
            con.commit()
    await m.answer(f"✅ Возврат по заказу #{oid} выполнен.")


@router.message(Command("orders"))
async def orders_command(m: Message):
    if not is_admin(m.from_user.id):
        return
    rows = fetchall("""SELECT o.id,o.user_id,o.stars,o.refunded,p.title FROM orders o
                     JOIN products p ON p.id=o.product_id ORDER BY o.id DESC LIMIT 15""")
    text = "<b>Последние 15 заказов</b>\n" + ("\n".join(
        f"#{r['id']} | user {r['user_id']} | {esc(r['title'][:25])} | {r['stars']} ⭐"
        + (" | ВОЗВРАТ" if r['refunded'] else "") for r in rows) or "Заказов нет")
    await m.answer(text, parse_mode="HTML")


@router.message(StateFilter(AddProduct.file, SetFile.file))
async def need_document(m: Message):
    await m.answer("Пришли файл именно как документ (не фото/текст) или /cancel.")


async def main():
    if not TOKEN:
        raise RuntimeError("Укажи BOT_TOKEN в .env или переменных окружения")
    if not ADMIN_IDS:
        raise RuntimeError("Укажи ADMIN_IDS — Telegram ID администратора")
    if DEFAULT_STARS < 0:
        raise RuntimeError("COURSE_STARS не может быть отрицательным")
    logging.basicConfig(level=logging.INFO)
    initialize()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
