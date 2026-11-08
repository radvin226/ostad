"""
ربات فروش خدمات مجازی (اپل آیدی، شماره مجازی و ...)
aiogram 3 + SQLAlchemy (async) + SQLite
نصب:  pip install -r requirements.txt
اجرا: python main.py
"""
import asyncio
import html
import logging
import os
from datetime import datetime

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ============================ تنظیمات ============================
BOT_TOKEN = os.getenv("9201680404:AAFe2RAWyF3Sn9lnLXbCcgEQYFUSDzAYf8I", "9201680404:AAFe2RAWyF3Sn9lnLXbCcgEQYFUSDzAYf8I")
ADMIN_IDS = {6420547446}                       # آیدی عددی ادمین(ها)
REQUIRED_CHANNEL = "@sofi_vpn"                # کانال اجباری (ربات باید ادمین کانال باشد)
CHANNEL_URL = "https://t.me/sofi_vpn"
DEFAULT_CARD = "6037-0000-0000-0000"          # شماره کارت پیش‌فرض (از پنل ادمین قابل تغییر است)
DB_URL = "sqlite+aiosqlite:///shop.db"
WELCOME = "🏠 <b>منوی اصلی</b>\nیکی از خدمات زیر را انتخاب کنید:"

# ============================ دیتابیس ============================
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # آیدی تلگرام
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    name: Mapped[str] = mapped_column(String(150))
    price: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(Text, default="")


class StockItem(Base):
    """هر آیتم = یک اپل‌آیدی / شماره / ... که بعد از تایید به خریدار ارسال می‌شود"""
    __tablename__ = "stock"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    sold: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    # waiting_receipt -> pending -> approved | rejected
    status: Mapped[str] = mapped_column(String(20), default="waiting_receipt", index=True)
    receipt_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receipt_type: Mapped[str | None] = mapped_column(String(10), nullable=True)  # photo / document
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    product: Mapped[Product] = relationship(lazy="selectin")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


engine = create_async_engine(DB_URL)
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_setting(key: str, default: str) -> str:
    async with Session() as s:
        row = await s.get(Setting, key)
        return row.value if row else default


async def set_setting(key: str, value: str):
    async with Session() as s:
        row = await s.get(Setting, key)
        if row:
            row.value = value
        else:
            s.add(Setting(key=key, value=value))
        await s.commit()


async def all_categories():
    async with Session() as s:
        return (await s.execute(select(Category).order_by(Category.id))).scalars().all()


async def all_products():
    async with Session() as s:
        return (await s.execute(select(Product).order_by(Product.id))).scalars().all()


# ============================ کمکی‌ها ============================
async def is_member(bot: Bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return m.status in {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}
    except Exception:
        logging.exception("membership check failed; is the bot an admin of the channel?")
        return False


def join_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 عضویت در کانال", url=CHANNEL_URL)
    kb.button(text="✅ عضو شدم", callback_data="check_join")
    kb.adjust(1)
    return kb.as_markup()


async def main_menu_kb():
    kb = InlineKeyboardBuilder()
    for c in await all_categories():
        kb.button(text=c.name, callback_data=f"cat:{c.id}")
    kb.adjust(2)
    return kb.as_markup()


async def show(cb: CallbackQuery, text: str, kb=None):
    """ویرایش پیام فعلی؛ اگر نشد، پیام جدید می‌فرستد"""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


async def send_order_card(bot: Bot, chat_id: int, order_id: int):
    """کارت سفارش + رسید + دکمه تایید/رد برای ادمین"""
    async with Session() as s:
        order = await s.get(Order, order_id)
    text = (
        f"🧾 <b>سفارش #{order.id}</b>\n"
        f"👤 کاربر: <code>{order.user_id}</code>\n"
        f"📦 محصول: {html.escape(order.product.name)}\n"
        f"💰 مبلغ: {html.escape(order.product.price)}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تایید و ارسال", callback_data=f"ord:ok:{order.id}")
    kb.button(text="❌ رد", callback_data=f"ord:no:{order.id}")
    kb.adjust(2)
    if order.receipt_type == "document":
        await bot.send_document(chat_id, order.receipt_file_id, caption=text, reply_markup=kb.as_markup())
    else:
        await bot.send_photo(chat_id, order.receipt_file_id, caption=text, reply_markup=kb.as_markup())


async def deliver_order(bot: Bot, order_id: int) -> tuple[str, str]:
    """یک آیتم آزاد از موجودی برمی‌دارد و برای خریدار می‌فرستد"""
    async with Session() as s:
        order = await s.get(Order, order_id)
        if not order or order.status != "pending":
            return "not_pending", ""
        item = (await s.execute(
            select(StockItem)
            .where(StockItem.product_id == order.product_id, StockItem.sold.is_(False))
            .limit(1)
        )).scalar_one_or_none()
        if item is None:
            return "no_stock", ""
        item.sold = True
        order.status = "approved"
        await s.commit()
        user_id, name, content = order.user_id, order.product.name, item.content
    try:
        await bot.send_message(
            user_id,
            f"🎉 سفارش شما تایید شد!\n\n📦 <b>{html.escape(name)}</b>\n\n"
            f"<code>{html.escape(content)}</code>\n\nاز خرید شما متشکریم 🙏",
        )
    except Exception:
        logging.exception("delivery failed for order %s", order_id)
        return "send_failed", content
    return "ok", content


async def reject_order(bot: Bot, order_id: int) -> bool:
    async with Session() as s:
        order = await s.get(Order, order_id)
        if not order or order.status != "pending":
            return False
        order.status = "rejected"
        await s.commit()
        user_id = order.user_id
    try:
        await bot.send_message(user_id, "❌ رسید شما تایید نشد. در صورت اشتباه، با پشتیبانی تماس بگیرید.")
    except Exception:
        logging.exception("could not notify user %s about rejection", user_id)
    return True


# ============================ میدل‌ویر عضویت اجباری ============================
class ForceJoin(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id in ADMIN_IDS:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_join":
            return await handler(event, data)
        if isinstance(event, Message) and event.text and event.text.startswith("/start"):
            return await handler(event, data)
        if await is_member(event.bot, user.id):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("ابتدا در کانال عضو شوید.", show_alert=True)
        else:
            await event.answer("ابتدا در کانال عضو شوید، سپس /start را بزنید.", reply_markup=join_kb())
        return None


# ============================ روترها ============================
user = Router()
user.message.outer_middleware(ForceJoin())
user.callback_query.outer_middleware(ForceJoin())

admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


class Purchase(StatesGroup):
    waiting_receipt = State()


class AdminFlow(StatesGroup):
    cat_name = State()
    prod_name = State()
    prod_price = State()
    prod_desc = State()
    stock = State()
    card = State()


# ============================ هندلرهای کاربر ============================
@user.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    async with Session() as s:
        if not await s.get(User, message.from_user.id):
            s.add(User(id=message.from_user.id, username=message.from_user.username))
            await s.commit()

    if await is_member(message.bot, message.from_user.id) or message.from_user.id in ADMIN_IDS:
        await message.answer(WELCOME, reply_markup=await main_menu_kb())
    else:
        await message.answer(
            "👋 برای استفاده از ربات ابتدا باید در کانال ما عضو شوید.\n\n"
            "پس از عضویت، دکمه «✅ عضو شدم» را بزنید.",
            reply_markup=join_kb(),
        )


@user.callback_query(F.data == "check_join")
async def cb_check_join(cb: CallbackQuery):
    if await is_member(cb.bot, cb.from_user.id):
        await cb.answer("✅ عضویت شما تایید شد")
        await show(cb, "✅ عضویت شما تایید شد.")
        await cb.message.answer(WELCOME, reply_markup=await main_menu_kb())
    else:
        await cb.answer("❌ هنوز عضو کانال نشده‌اید!", show_alert=True)


@user.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, WELCOME, await main_menu_kb())
    await cb.answer()


@user.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery):
    cid = int(cb.data.split(":")[1])
    async with Session() as s:
        cat = await s.get(Category, cid)
        if not cat:
            return await cb.answer("دسته‌بندی پیدا نشد.", show_alert=True)
        products = (await s.execute(
            select(Product).where(Product.category_id == cid).order_by(Product.id)
        )).scalars().all()
        in_stock = set((await s.execute(
            select(StockItem.product_id).where(StockItem.sold.is_(False)).distinct()
        )).scalars().all())

    kb = InlineKeyboardBuilder()
    for p in products:
        mark = "" if p.id in in_stock else " ⛔️"
        kb.button(text=f"{p.name} | {p.price}{mark}", callback_data=f"prod:{p.id}")
    kb.button(text="🔙 بازگشت", callback_data="menu")
    kb.adjust(1)
    body = "یکی از گزینه‌ها را انتخاب کنید:" if products else "فعلاً محصولی در این بخش وجود ندارد."
    await show(cb, f"📂 <b>{html.escape(cat.name)}</b>\n\n{body}", kb.as_markup())
    await cb.answer()


@user.callback_query(F.data.startswith("prod:"))
async def cb_product(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Product, pid)
        if not p:
            return await cb.answer("محصول پیدا نشد.", show_alert=True)
        stock_left = await s.scalar(
            select(func.count()).select_from(StockItem)
            .where(StockItem.product_id == pid, StockItem.sold.is_(False))
        )

    text = (f"📦 <b>{html.escape(p.name)}</b>\n"
            f"💰 قیمت: {html.escape(p.price)}\n\n"
            f"{html.escape(p.description or '')}")
    kb = InlineKeyboardBuilder()
    if stock_left:
        kb.button(text="🛒 خرید", callback_data=f"buy:{pid}")
    else:
        text += "\n\n⛔️ فعلاً ناموجود است."
    kb.button(text="🔙 بازگشت", callback_data=f"cat:{p.category_id}")
    kb.adjust(1)
    await show(cb, text.strip(), kb.as_markup())
    await cb.answer()


@user.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Product, pid)
    if not p:
        return await cb.answer("محصول پیدا نشد.", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ بله، اطمینان دارم", callback_data=f"buyok:{pid}")
    kb.button(text="❌ خیر", callback_data=f"prod:{pid}")
    kb.adjust(1)
    await show(
        cb,
        f"⚠️ آیا از خرید <b>{html.escape(p.name)}</b> به مبلغ {html.escape(p.price)} اطمینان دارید؟",
        kb.as_markup(),
    )
    await cb.answer()


@user.callback_query(F.data.startswith("buyok:"))
async def cb_buy_confirmed(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    async with Session() as s:
        p = await s.get(Product, pid)
        if not p:
            return await cb.answer("محصول پیدا نشد.", show_alert=True)
        order = Order(user_id=cb.from_user.id, product_id=pid)
        s.add(order)
        await s.commit()
        oid = order.id

    card = await get_setting("card", DEFAULT_CARD)
    await state.set_state(Purchase.waiting_receipt)
    await state.update_data(order_id=oid)

    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 انصراف", callback_data="menu")
    kb.adjust(1)
    await show(
        cb,
        f"🧾 سفارش #{oid} ثبت شد.\n\n"
        f"💳 لطفاً مبلغ <b>{html.escape(p.price)}</b> را به شماره کارت زیر واریز کنید:\n\n"
        f"<code>{html.escape(card)}</code>\n\n"
        "پس از واریز، <b>عکس یا فایل رسید</b> را همین‌جا ارسال کنید.",
        kb.as_markup(),
    )
    await cb.answer()


@user.message(Purchase.waiting_receipt, F.photo | F.document)
async def msg_receipt(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.photo:
        file_id, kind = message.photo[-1].file_id, "photo"
    else:
        file_id, kind = message.document.file_id, "document"

    async with Session() as s:
        order = await s.get(Order, data["order_id"])
        if not order or order.status != "waiting_receipt":
            await state.clear()
            return await message.answer("❌ این سفارش دیگر معتبر نیست. دوباره از منوی اصلی اقدام کنید.")
        order.status = "pending"
        order.receipt_file_id = file_id
        order.receipt_type = kind
        await s.commit()

    await state.clear()
    await message.answer("✅ رسید شما ثبت شد. بعد از بررسی و تایید، محصول برای شما ارسال می‌شود.")
    for admin_id in ADMIN_IDS:
        try:
            await send_order_card(message.bot, admin_id, data["order_id"])
        except Exception:
            logging.exception("could not notify admin %s", admin_id)


@user.message(Purchase.waiting_receipt)
async def msg_not_receipt(message: Message):
    await message.answer("لطفاً رسید را به صورت عکس یا فایل ارسال کنید.")


# ============================ هندلرهای ادمین ============================
def admin_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ افزودن دسته‌بندی", callback_data="adm:cat")
    kb.button(text="➕ افزودن محصول", callback_data="adm:prod")
    kb.button(text="📦 افزودن موجودی", callback_data="adm:stock")
    kb.button(text="📋 سفارش‌های در انتظار", callback_data="adm:pending")
    kb.button(text="💳 تنظیم شماره کارت", callback_data="adm:card")
    kb.adjust(1)
    return kb.as_markup()


@admin.message(Command("admin"))
async def adm_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 <b>پنل مدیریت</b>", reply_markup=admin_kb())


# --- دسته‌بندی ---
@admin.callback_query(F.data == "adm:cat")
async def adm_cat(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.cat_name)
    await cb.message.answer("نام دسته‌بندی جدید را بفرستید:")
    await cb.answer()


@admin.message(AdminFlow.cat_name, F.text)
async def adm_cat_save(message: Message, state: FSMContext):
    async with Session() as s:
        s.add(Category(name=message.text.strip()))
        await s.commit()
    await state.clear()
    await message.answer("✅ دسته‌بندی ثبت شد.", reply_markup=admin_kb())


# --- محصول ---
@admin.callback_query(F.data == "adm:prod")
async def adm_prod(cb: CallbackQuery):
    cats = await all_categories()
    if not cats:
        return await cb.answer("اول یک دسته‌بندی بسازید.", show_alert=True)
    kb = InlineKeyboardBuilder()
    for c in cats:
        kb.button(text=c.name, callback_data=f"pcat:{c.id}")
    kb.adjust(1)
    await cb.message.answer("دسته‌بندی محصول را انتخاب کنید:", reply_markup=kb.as_markup())
    await cb.answer()


@admin.callback_query(F.data.startswith("pcat:"))
async def adm_prod_cat(cb: CallbackQuery, state: FSMContext):
    await state.update_data(category_id=int(cb.data.split(":")[1]))
    await state.set_state(AdminFlow.prod_name)
    await cb.message.answer("نام محصول را بفرستید:")
    await cb.answer()


@admin.message(AdminFlow.prod_name, F.text)
async def adm_prod_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminFlow.prod_price)
    await message.answer("قیمت را بفرستید (مثلا: ۱۵۰ هزار تومان):")


@admin.message(AdminFlow.prod_price, F.text)
async def adm_prod_price(message: Message, state: FSMContext):
    await state.update_data(price=message.text.strip())
    await state.set_state(AdminFlow.prod_desc)
    await message.answer("توضیحات محصول را بفرستید، یا «-» برای رد شدن:")


@admin.message(AdminFlow.prod_desc, F.text)
async def adm_prod_desc(message: Message, state: FSMContext):
    data = await state.get_data()
    desc = "" if message.text.strip() == "-" else message.text.strip()
    async with Session() as s:
        s.add(Product(
            category_id=data["category_id"],
            name=data["name"],
            price=data["price"],
            description=desc,
        ))
        await s.commit()
    await state.clear()
    await message.answer(
        "✅ محصول ثبت شد. برای فروش، از «📦 افزودن موجودی» آیتم اضافه کنید.",
        reply_markup=admin_kb(),
    )


# --- موجودی ---
@admin.callback_query(F.data == "adm:stock")
async def adm_stock(cb: CallbackQuery):
    products = await all_products()
    if not products:
        return await cb.answer("هنوز محصولی ثبت نشده.", show_alert=True)
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=p.name, callback_data=f"stk:{p.id}")
    kb.adjust(1)
    await cb.message.answer("محصول را انتخاب کنید:", reply_markup=kb.as_markup())
    await cb.answer()


@admin.callback_query(F.data.startswith("stk:"))
async def adm_stock_pick(cb: CallbackQuery, state: FSMContext):
    await state.update_data(product_id=int(cb.data.split(":")[1]))
    await state.set_state(AdminFlow.stock)
    await cb.message.answer(
        "موجودی را بفرستید؛ هر خط یک آیتم.\n"
        "مثال: <code>email1@x.com|pass1</code>"
    )
    await cb.answer()


@admin.message(AdminFlow.stock, F.text)
async def adm_stock_save(message: Message, state: FSMContext):
    data = await state.get_data()
    lines = [ln.strip() for ln in message.text.splitlines() if ln.strip()]
    async with Session() as s:
        s.add_all([StockItem(product_id=data["product_id"], content=ln) for ln in lines])
        await s.commit()
    await state.clear()
    await message.answer(f"✅ {len(lines)} آیتم به موجودی اضافه شد.", reply_markup=admin_kb())


# --- شماره کارت ---
@admin.callback_query(F.data == "adm:card")
async def adm_card(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFlow.card)
    await cb.message.answer("شماره کارت جدید را بفرستید:")
    await cb.answer()


@admin.message(AdminFlow.card, F.text)
async def adm_card_save(message: Message, state: FSMContext):
    await set_setting("card", message.text.strip())
    await state.clear()
    await message.answer("✅ شماره کارت ذخیره شد.", reply_markup=admin_kb())


# --- سفارش‌های در انتظار ---
@admin.callback_query(F.data == "adm:pending")
async def adm_pending(cb: CallbackQuery):
    async with Session() as s:
        orders = (await s.execute(
            select(Order).where(Order.status == "pending").order_by(Order.id).limit(20)
        )).scalars().all()
    if not orders:
        return await cb.answer("سفارش در انتظاری وجود ندارد.", show_alert=True)
    for o in orders:
        await send_order_card(cb.bot, cb.from_user.id, o.id)
    await cb.answer()


@admin.callback_query(F.data.startswith("ord:"))
async def cb_order_decision(cb: CallbackQuery):
    _, action, oid = cb.data.split(":")
    oid = int(oid)

    if action == "ok":
        result, content = await deliver_order(cb.bot, oid)
        if result == "no_stock":
            return await cb.answer("⛔️ موجودی این محصول تمام شده. ابتدا موجودی اضافه کنید.", show_alert=True)
        if result == "not_pending":
            return await cb.answer("این سفارش دیگر در انتظار نیست.", show_alert=True)
        if result == "send_failed":
            await cb.message.answer(
                f"⚠️ ارسال به کاربر انجام نشد. به صورت دستی بفرستید:\n<code>{html.escape(content)}</code>"
            )
        status = "✅ تایید و ارسال شد" if result == "ok" else "✅ تایید شد (ارسال دستی)"
    else:
        if not await reject_order(cb.bot, oid):
            return await cb.answer("این سفارش دیگر در انتظار نیست.", show_alert=True)
        status = "❌ رد شد"

    await cb.answer()
    try:
        await cb.message.edit_caption(caption=f"{cb.message.caption or ''}\n\n{status}", reply_markup=None)
    except Exception:
        pass


# ============================ اجرا ============================
async def main():
    logging.basicConfig(level=logging.INFO)
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise SystemExit("توکن ربات را در BOT_TOKEN یا متغیر محیطی قرار دهید.")

    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(admin, user)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
