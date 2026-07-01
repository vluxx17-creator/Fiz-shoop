import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ================== ВАШИ ДАННЫЕ (подставлены) ==================
BOT_TOKEN = "8918867676:AAHixz0SseKQ9eqV99oDPI-CTwdQsXrO9mI"
ADMIN_IDS = [7727618205, 8297446667]

# Для платежей – замените на реальный PROVIDER_TOKEN от @BotFather
PROVIDER_TOKEN = "TEST"  # или ваш настоящий токен

DATABASE_NAME = "shop_bot.db"
MIN_DEPOSIT = 40

# ================== НАСТРОЙКА ЛОГИРОВАНИЯ ==================
logging.basicConfig(level=logging.INFO)

# ================== ИНИЦИАЛИЗАЦИЯ БОТА ==================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ================== РАБОТА С БАЗОЙ ДАННЫХ ==================
class Database:
    def __init__(self, db_name: str):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0,
                registered_at TEXT
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                country TEXT,
                number TEXT,
                code TEXT,
                date TEXT,
                price REAL,
                description TEXT,
                file_id TEXT,
                is_sold BOOLEAN DEFAULT 0,
                buyer_id INTEGER REFERENCES users(id)
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                account_id INTEGER REFERENCES accounts(id),
                purchase_date TEXT,
                price REAL
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                review_text TEXT,
                created_at TEXT
            )
        """)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                message TEXT,
                created_at TEXT,
                is_answered BOOLEAN DEFAULT 0,
                answer TEXT,
                answered_at TEXT
            )
        """)
        self.conn.commit()

    # ------------------ ПОЛЬЗОВАТЕЛИ ------------------
    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = self.cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "username": row[1],
                "balance": row[2],
                "registered_at": row[3],
            }
        return None

    def create_user(self, user_id: int, username: str = None):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT INTO users (id, username, balance, registered_at) VALUES (?, ?, ?, ?)",
            (user_id, username, 0.0, now),
        )
        self.conn.commit()

    def update_balance(self, user_id: int, amount: float):
        self.cursor.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?",
            (amount, user_id),
        )
        self.conn.commit()

    def get_balance(self, user_id: int) -> float:
        self.cursor.execute("SELECT balance FROM users WHERE id = ?", (user_id,))
        row = self.cursor.fetchone()
        return row[0] if row else 0.0

    def get_bought_count(self, user_id: int) -> int:
        self.cursor.execute(
            "SELECT COUNT(*) FROM purchases WHERE user_id = ?",
            (user_id,)
        )
        row = self.cursor.fetchone()
        return row[0] if row else 0

    # ------------------ АККАУНТЫ ------------------
    def get_available_accounts(self, country: str = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM accounts WHERE is_sold = 0"
        params = []
        if country:
            query += " AND country = ?"
            params.append(country)
        self.cursor.execute(query, params)
        rows = self.cursor.fetchall()
        accounts = []
        for row in rows:
            accounts.append({
                "id": row[0],
                "country": row[1],
                "number": row[2],
                "code": row[3],
                "date": row[4],
                "price": row[5],
                "description": row[6],
                "file_id": row[7],
                "is_sold": row[8],
                "buyer_id": row[9],
            })
        return accounts

    def get_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        self.cursor.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = self.cursor.fetchone()
        if row:
            return {
                "id": row[0],
                "country": row[1],
                "number": row[2],
                "code": row[3],
                "date": row[4],
                "price": row[5],
                "description": row[6],
                "file_id": row[7],
                "is_sold": row[8],
                "buyer_id": row[9],
            }
        return None

    def buy_account(self, user_id: int, account_id: int) -> bool:
        account = self.get_account(account_id)
        if not account or account["is_sold"]:
            return False
        price = account["price"]
        balance = self.get_balance(user_id)
        if balance < price:
            return False

        self.update_balance(user_id, -price)
        self.cursor.execute(
            "UPDATE accounts SET is_sold = 1, buyer_id = ? WHERE id = ?",
            (user_id, account_id),
        )
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT INTO purchases (user_id, account_id, purchase_date, price) VALUES (?, ?, ?, ?)",
            (user_id, account_id, now, price),
        )
        self.conn.commit()
        return True

    def add_account(self, country: str, number: str, code: str, date: str, price: float, description: str = "", file_id: str = None):
        self.cursor.execute(
            "INSERT INTO accounts (country, number, code, date, price, description, file_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (country, number, code, date, price, description, file_id),
        )
        self.conn.commit()

    def get_user_purchases(self, user_id: int) -> List[Dict[str, Any]]:
        self.cursor.execute("""
            SELECT a.*, p.purchase_date, p.price as paid_price
            FROM purchases p
            JOIN accounts a ON p.account_id = a.id
            WHERE p.user_id = ?
            ORDER BY p.purchase_date DESC
        """, (user_id,))
        rows = self.cursor.fetchall()
        purchases = []
        for row in rows:
            purchases.append({
                "id": row[0],
                "country": row[1],
                "number": row[2],
                "code": row[3],
                "date": row[4],
                "price": row[5],
                "description": row[6],
                "file_id": row[7],
                "is_sold": row[8],
                "buyer_id": row[9],
                "purchase_date": row[10],
                "paid_price": row[11],
            })
        return purchases

    # ------------------ ОТЗЫВЫ ------------------
    def add_review(self, user_id: int, review_text: str):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT INTO reviews (user_id, review_text, created_at) VALUES (?, ?, ?)",
            (user_id, review_text, now),
        )
        self.conn.commit()

    # ------------------ ПОДДЕРЖКА ------------------
    def add_support_message(self, user_id: int, message: str):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT INTO support_messages (user_id, message, created_at) VALUES (?, ?, ?)",
            (user_id, message, now),
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def get_unanswered_messages(self) -> List[Dict[str, Any]]:
        self.cursor.execute(
            "SELECT * FROM support_messages WHERE is_answered = 0 ORDER BY created_at"
        )
        rows = self.cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row[0],
                "user_id": row[1],
                "message": row[2],
                "created_at": row[3],
                "is_answered": row[4],
                "answer": row[5],
                "answered_at": row[6],
            })
        return result

    def mark_answer(self, msg_id: int, answer: str):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "UPDATE support_messages SET is_answered = 1, answer = ?, answered_at = ? WHERE id = ?",
            (answer, now, msg_id),
        )
        self.conn.commit()

    def get_all_users(self) -> List[Dict[str, Any]]:
        self.cursor.execute("SELECT id, username, balance, registered_at FROM users")
        rows = self.cursor.fetchall()
        users = []
        for row in rows:
            users.append({
                "id": row[0],
                "username": row[1],
                "balance": row[2],
                "registered_at": row[3],
            })
        return users

    def close(self):
        self.conn.close()

# ================== ИНИЦИАЛИЗАЦИЯ БД ==================
db = Database(DATABASE_NAME)

# Закомментируйте или удалите эту функцию, если не нужны тестовые аккаунты
def seed_accounts():
    existing = db.cursor.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    if existing == 0:
        sample_accounts = [
            ("РФ", "+7 999 123-45-67", "ABC123", "2026-07-01", 50.0, "Инструкция: логин 123, пароль qwe", None),
            ("РФ", "+7 999 234-56-78", "DEF456", "2026-07-02", 70.0, "Использовать VPN", None),
            ("КЗ", "+7 777 111-22-33", "GHI789", "2026-07-03", 60.0, "", None),
            ("УКР", "+380 50 111-22-33", "JKL012", "2026-07-04", 55.0, "", None),
            ("Беларусь", "+375 29 111-22-33", "MNO345", "2026-07-05", 65.0, "", None),
            ("Узбекистан", "+998 90 111-22-33", "PQR678", "2026-07-06", 45.0, "", None),
            ("Азербайджан", "+994 50 111-22-33", "STU901", "2026-07-07", 50.0, "", None),
        ]
        for country, number, code, date, price, desc, file_id in sample_accounts:
            db.add_account(country, number, code, date, price, desc, file_id)
        db.conn.commit()

seed_accounts()  # раскомментируйте, если нужны тестовые данные

# ================== КЛАВИАТУРЫ ==================
def main_menu_keyboard(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Профиль", callback_data="profile")
    builder.button(text="🛒 Купить аккаунт", callback_data="buy")
    builder.button(text="💰 Пополнить баланс", callback_data="deposit")
    builder.button(text="📱 Мои аккаунты", callback_data="my_accounts")
    builder.button(text="📞 Техподдержка", callback_data="support")
    if user_id in ADMIN_IDS:
        builder.button(text="🛠 Админ-панель", callback_data="admin_panel")
    builder.adjust(2)
    return builder.as_markup()

def back_to_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="main_menu")
    return builder.as_markup()

def country_keyboard():
    countries = ["РФ", "КЗ", "УКР", "Беларусь", "Узбекистан", "Азербайджан"]
    builder = InlineKeyboardBuilder()
    for country in countries:
        builder.button(text=country, callback_data=f"country_{country}")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

def account_keyboard(accounts: List[Dict[str, Any]]):
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        text = f"{acc['country']} - {acc['number']} ({acc['price']}₽)"
        builder.button(text=text, callback_data=f"buy_account_{acc['id']}")
    builder.button(text="🔙 Назад", callback_data="buy")
    builder.adjust(1)
    return builder.as_markup()

def payment_keyboard(account_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить", callback_data=f"pay_{account_id}")
    builder.button(text="🔙 Назад", callback_data="buy")
    return builder.as_markup()

def review_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Оставить отзыв", callback_data="leave_review")
    builder.button(text="🏠 В главное меню", callback_data="main_menu")
    return builder.as_markup()

def admin_panel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Пополнить физ номера", callback_data="admin_add_account")
    builder.button(text="📋 Неотвеченные обращения", callback_data="admin_support_list")
    builder.button(text="👥 Список пользователей", callback_data="admin_users_list")
    builder.button(text="💸 Выдать баланс (команда)", callback_data="admin_balance_help")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def my_accounts_keyboard(accounts: List[Dict[str, Any]]):
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        purchase_date = acc.get("purchase_date", "")
        try:
            dt = datetime.fromisoformat(purchase_date)
            date_str = dt.strftime("%d.%m.%Y")
        except:
            date_str = purchase_date[:10]
        text = f"{acc['number']} (купил {date_str})"
        builder.button(text=text, callback_data=f"my_acc_{acc['id']}")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

# ================== FSM СОСТОЯНИЯ ==================
class DepositStates(StatesGroup):
    waiting_for_amount = State()

class ReviewStates(StatesGroup):
    waiting_for_review = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

class AdminAddAccountStates(StatesGroup):
    waiting_country = State()
    waiting_number = State()
    waiting_code = State()
    waiting_date = State()
    waiting_price = State()
    waiting_description = State()
    waiting_file = State()

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
async def send_account_data(chat_id: int, account: Dict[str, Any], caption_extra: str = ""):
    text = (
        f"📱 Данные аккаунта:\n\n"
        f"Страна: {account['country']}\n"
        f"Номер: {account['number']}\n"
        f"Код: {account['code']}\n"
        f"Дата: {account['date']}\n"
        f"Описание: {account['description'] or 'Нет'}\n"
        f"Цена: {account['price']}₽\n"
        f"{caption_extra}"
    )
    file_id = account.get("file_id")
    if file_id:
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=file_id,
                caption=text,
                reply_markup=back_to_menu_keyboard()
            )
        except Exception as e:
            logging.error(f"Ошибка отправки файла: {e}")
            await bot.send_message(chat_id, text, reply_markup=back_to_menu_keyboard())
    else:
        await bot.send_message(chat_id, text, reply_markup=back_to_menu_keyboard())

# ================== ОБРАБОТЧИКИ КОМАНД ==================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Без ника"
    if not db.get_user(user_id):
        db.create_user(user_id, username)
    balance = db.get_balance(user_id)
    await message.answer(
        f"Вы попали в физ.шоп\n\nВаш баланс: {balance:.2f}₽",
        reply_markup=main_menu_keyboard(user_id)
    )

@dp.message(Command("addbalance"))
async def cmd_add_balance(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для этой команды.")
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("❌ Использование: /addbalance <user_id> <сумма>")
        return
    try:
        user_id = int(args[1])
        amount = float(args[2].replace(",", "."))
    except ValueError:
        await message.answer("❌ ID пользователя должен быть числом, сумма – числом.")
        return
    if amount <= 0:
        await message.answer("❌ Сумма должна быть положительной.")
        return
    db.update_balance(user_id, amount)
    await message.answer(f"✅ Баланс пользователя {user_id} пополнен на {amount:.2f}₽.")
    try:
        await bot.send_message(user_id, f"💰 Ваш баланс пополнен администратором на {amount:.2f}₽.")
    except:
        pass

@dp.message(Command("reply"))
async def cmd_reply(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для этой команды.")
        return
    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("❌ Использование: /reply <user_id> <текст ответа>")
        return
    try:
        user_id = int(args[1])
    except ValueError:
        await message.answer("❌ ID пользователя должен быть числом.")
        return
    answer_text = args[2]
    try:
        await bot.send_message(user_id, f"📩 Ответ от поддержки:\n{answer_text}")
        await message.answer(f"✅ Ответ отправлен пользователю {user_id}.")
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки: {e}")

# ================== ОБРАБОТЧИКИ CALLBACK ==================
@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    balance = db.get_balance(user_id)
    await callback.message.edit_text(
        f"Вы попали в физ.шоп\n\nВаш баланс: {balance:.2f}₽",
        reply_markup=main_menu_keyboard(user_id)
    )
    await callback.answer()

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = db.get_user(user_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    bought_count = db.get_bought_count(user_id)
    username = user["username"] or "Без ника"
    balance = user["balance"]
    registered_at = user["registered_at"]
    try:
        dt = datetime.fromisoformat(registered_at)
        reg_date = dt.strftime("%d.%m.%Y %H:%M")
    except:
        reg_date = registered_at
    text = (
        f"👤 Профиль\n\n"
        f"Ник: @{username}\n"
        f"Баланс: {balance:.2f}₽\n"
        f"Куплено аккаунтов: {bought_count}\n"
        f"Дата регистрации: {reg_date}"
    )
    await callback.message.edit_text(text, reply_markup=back_to_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "buy")
async def buy_country(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌍 Выберите страну аккаунта:",
        reply_markup=country_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("country_"))
async def select_country(callback: CallbackQuery):
    country = callback.data.split("_", 1)[1]
    accounts = db.get_available_accounts(country)
    if not accounts:
        await callback.message.edit_text(
            f"В данный момент нет доступных аккаунтов для {country}.",
            reply_markup=back_to_menu_keyboard()
        )
        await callback.answer()
        return
    accounts.sort(key=lambda x: x["price"])
    text = f"📱 Доступные аккаунты ({country}):\n\n"
    for acc in accounts:
        text += f"• {acc['number']} — {acc['price']}₽\n"
    await callback.message.edit_text(
        text,
        reply_markup=account_keyboard(accounts)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_account_"))
async def buy_account(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    account = db.get_account(account_id)
    if not account or account["is_sold"]:
        await callback.answer("Этого аккаунта уже нет в наличии.", show_alert=True)
        await callback.message.edit_text("Выберите страну:", reply_markup=country_keyboard())
        return
    user_id = callback.from_user.id
    balance = db.get_balance(user_id)
    if balance < account["price"]:
        await callback.answer(
            f"Недостаточно средств. Пополните баланс (нужно {account['price']}₽).",
            show_alert=True
        )
        await callback.message.edit_text(
            f"Недостаточно средств для покупки аккаунта {account['number']}.\n"
            f"Цена: {account['price']}₽\n"
            f"Ваш баланс: {balance:.2f}₽\n\n"
            "Пополните баланс через кнопку '💰 Пополнить баланс' в главном меню.",
            reply_markup=back_to_menu_keyboard()
        )
        return
    text = (
        f"Вы выбрали аккаунт:\n"
        f"Страна: {account['country']}\n"
        f"Номер: {account['number']}\n"
        f"Цена: {account['price']}₽\n"
        f"Баланс: {balance:.2f}₽\n\n"
        "Подтвердите покупку:"
    )
    await callback.message.edit_text(
        text,
        reply_markup=payment_keyboard(account_id)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("pay_"))
async def confirm_payment(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[1])
    account = db.get_account(account_id)
    if not account or account["is_sold"]:
        await callback.answer("Аккаунт уже продан.", show_alert=True)
        await callback.message.edit_text("Выберите страну:", reply_markup=country_keyboard())
        return
    user_id = callback.from_user.id
    balance = db.get_balance(user_id)
    if balance < account["price"]:
        await callback.answer("Недостаточно средств.", show_alert=True)
        return

    success = db.buy_account(user_id, account_id)
    if not success:
        await callback.answer("Ошибка при покупке.", show_alert=True)
        return

    await callback.message.delete()
    await send_account_data(
        chat_id=user_id,
        account=account,
        caption_extra="✅ Аккаунт успешно куплен!"
    )
    await bot.send_message(
        user_id,
        "✍️ Оставьте отзыв о покупке, нажав на кнопку ниже.",
        reply_markup=review_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "deposit")
async def deposit(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💰 Введите сумму пополнения (минимум 40₽):\n\n"
        "Введите число, например: 100",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(DepositStates.waiting_for_amount)
    await callback.answer()

@dp.message(DepositStates.waiting_for_amount)
async def process_deposit_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число (например, 100).")
        return
    if amount < MIN_DEPOSIT:
        await message.answer(
            f"❌ Минимальная сумма пополнения {MIN_DEPOSIT}₽. Попробуйте снова."
        )
        return
    try:
        await bot.send_invoice(
            chat_id=message.chat.id,
            title="Пополнение баланса",
            description=f"Пополнение баланса в физ.шоп на {amount}₽",
            payload=f"deposit_{message.from_user.id}_{amount}",
            provider_token=PROVIDER_TOKEN,
            currency="RUB",
            prices=[LabeledPrice(label="Пополнение", amount=int(amount * 100))],
            start_parameter="deposit",
            need_email=False,
            need_phone_number=False,
            is_flexible=False,
        )
    except Exception as e:
        logging.error(f"Ошибка отправки инвойса: {e}")
        await message.answer(
            "❌ Ошибка при создании платежа. Попробуйте позже.",
            reply_markup=back_to_menu_keyboard()
        )
    await state.clear()

# ================== ПЛАТЕЖИ ==================
@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment = message.successful_payment
    payload = payment.invoice_payload
    try:
        parts = payload.split("_")
        if parts[0] == "deposit":
            user_id = int(parts[1])
            amount = float(parts[2])
            db.update_balance(user_id, amount)
            await message.answer(
                f"✅ Ваш баланс пополнен на {amount:.2f}₽.\n"
                f"Текущий баланс: {db.get_balance(user_id):.2f}₽",
                reply_markup=back_to_menu_keyboard()
            )
        else:
            await message.answer("Получен платёж с неизвестным payload.", reply_markup=back_to_menu_keyboard())
    except Exception as e:
        logging.error(f"Ошибка обработки платежа: {e}")
        await message.answer("Произошла ошибка при обработке платежа.", reply_markup=back_to_menu_keyboard())

# ================== МОИ АККАУНТЫ ==================
@dp.callback_query(F.data == "my_accounts")
async def my_accounts(callback: CallbackQuery):
    user_id = callback.from_user.id
    purchases = db.get_user_purchases(user_id)
    if not purchases:
        await callback.message.edit_text(
            "📭 У вас пока нет купленных аккаунтов.",
            reply_markup=back_to_menu_keyboard()
        )
        await callback.answer()
        return
    text = "📱 Ваши купленные аккаунты:\n\n"
    for acc in purchases:
        purchase_date = acc.get("purchase_date", "")
        try:
            dt = datetime.fromisoformat(purchase_date)
            date_str = dt.strftime("%d.%m.%Y %H:%M")
        except:
            date_str = purchase_date
        text += f"• {acc['number']} ({acc['country']}) – куплен {date_str}, цена {acc['paid_price']}₽\n"
    await callback.message.edit_text(
        text,
        reply_markup=my_accounts_keyboard(purchases)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("my_acc_"))
async def my_account_details(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    account = db.get_account(account_id)
    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return
    user_id = callback.from_user.id
    purchases = db.get_user_purchases(user_id)
    if not any(a["id"] == account_id for a in purchases):
        await callback.answer("Это не ваш аккаунт.", show_alert=True)
        return
    await callback.message.delete()
    await send_account_data(
        chat_id=user_id,
        account=account,
        caption_extra=""
    )
    await callback.answer()

# ================== ОТЗЫВЫ ==================
@dp.callback_query(F.data == "leave_review")
async def leave_review(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✍️ Напишите ваш отзыв о покупке. Мы будем рады услышать ваше мнение!",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(ReviewStates.waiting_for_review)
    await callback.answer()

@dp.message(ReviewStates.waiting_for_review)
async def process_review(message: Message, state: FSMContext):
    user_id = message.from_user.id
    review_text = message.text
    db.add_review(user_id, review_text)
    await message.answer(
        "✅ Спасибо за ваш отзыв! Он помогает нам становиться лучше.",
        reply_markup=back_to_menu_keyboard()
    )
    await state.clear()

# ================== ТЕХПОДДЕРЖКА ==================
@dp.callback_query(F.data == "support")
async def support_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📞 Напишите ваше сообщение в техподдержку.\n"
        "Мы ответим вам в ближайшее время.",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(SupportStates.waiting_for_message)
    await callback.answer()

@dp.message(SupportStates.waiting_for_message)
async def support_receive_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text
    db.add_support_message(user_id, text)
    username = message.from_user.username or "Без ника"
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📩 Новое обращение в поддержку!\n"
                f"От: @{username} (ID: {user_id})\n"
                f"Сообщение: {text}\n\n"
                f"Для ответа используйте команду:\n/reply {user_id} <текст>"
            )
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    await message.answer(
        "✅ Ваше сообщение отправлено. Мы свяжемся с вами в ближайшее время.",
        reply_markup=back_to_menu_keyboard()
    )
    await state.clear()

# ================== АДМИН-ПАНЕЛЬ ==================
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.message.edit_text(
        "🛠 Админ-панель\nВыберите действие:",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_balance_help")
async def admin_balance_help(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.message.edit_text(
        "💸 Выдача баланса:\n\n"
        "Используйте команду:\n"
        "/addbalance <user_id> <сумма>\n\n"
        "Например: /addbalance 123456789 100",
        reply_markup=back_to_menu_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_add_account")
async def admin_add_account(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.message.edit_text(
        "➕ Пополнение физических номеров (добавление аккаунта).\n"
        "Введите страну (например: РФ):",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(AdminAddAccountStates.waiting_country)
    await callback.answer()

@dp.message(AdminAddAccountStates.waiting_country)
async def admin_add_country(message: Message, state: FSMContext):
    await state.update_data(country=message.text.strip())
    await message.answer("Введите номер телефона (в любом формате):")
    await state.set_state(AdminAddAccountStates.waiting_number)

@dp.message(AdminAddAccountStates.waiting_number)
async def admin_add_number(message: Message, state: FSMContext):
    await state.update_data(number=message.text.strip())
    await message.answer("Введите код (пароль, пин-код и т.п.):")
    await state.set_state(AdminAddAccountStates.waiting_code)

@dp.message(AdminAddAccountStates.waiting_code)
async def admin_add_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.strip())
    await message.answer("Введите дату (например, 2026-07-01):")
    await state.set_state(AdminAddAccountStates.waiting_date)

@dp.message(AdminAddAccountStates.waiting_date)
async def admin_add_date(message: Message, state: FSMContext):
    await state.update_data(date=message.text.strip())
    await message.answer("Введите цену (число, например 50):")
    await state.set_state(AdminAddAccountStates.waiting_price)

@dp.message(AdminAddAccountStates.waiting_price)
async def admin_add_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Цена должна быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(price=price)
    await message.answer(
        "Введите описание (инструкция по входу, дополнительная информация).\n"
        "Если хотите прикрепить файл, отправьте его следующим сообщением (можно фото, документ).\n"
        "Или нажмите «Пропустить», чтобы не загружать файл.",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(AdminAddAccountStates.waiting_description)

@dp.message(AdminAddAccountStates.waiting_description)
async def admin_add_description(message: Message, state: FSMContext):
    if message.text and message.text.lower() == "пропустить":
        description = ""
    else:
        description = message.text.strip()
    await state.update_data(description=description)
    await message.answer(
        "Теперь отправьте файл (если нужен) или нажмите «Пропустить».",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(AdminAddAccountStates.waiting_file)

@dp.message(AdminAddAccountStates.waiting_file)
async def admin_add_file(message: Message, state: FSMContext):
    file_id = None
    if message.document:
        file_id = message.document.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.audio:
        file_id = message.audio.file_id
    elif message.text and message.text.lower() == "пропустить":
        pass
    else:
        await message.answer("Пожалуйста, отправьте файл (документ, фото, видео) или нажмите «Пропустить».")
        return

    data = await state.get_data()
    country = data["country"]
    number = data["number"]
    code = data["code"]
    date = data["date"]
    price = data["price"]
    description = data.get("description", "")

    db.add_account(country, number, code, date, price, description, file_id)
    await message.answer(
        f"✅ Аккаунт добавлен!\n"
        f"Страна: {country}\n"
        f"Номер: {number}\n"
        f"Код: {code}\n"
        f"Дата: {date}\n"
        f"Цена: {price}₽\n"
        f"Описание: {description or 'Нет'}\n"
        f"Файл: {'Прикреплён ✅' if file_id else 'Нет'}",
        reply_markup=back_to_menu_keyboard()
    )
    await state.clear()

# ================== ПРОСМОТР НЕОТВЕЧЕННЫХ ОБРАЩЕНИЙ ==================
@dp.callback_query(F.data == "admin_support_list")
async def admin_support_list(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    messages = db.get_unanswered_messages()
    if not messages:
        await callback.message.edit_text(
            "📭 Нет новых обращений.",
            reply_markup=back_to_menu_keyboard()
        )
        await callback.answer()
        return
    text = "📋 Список неотвеченных обращений:\n\n"
    for msg in messages[:10]:
        user = db.get_user(msg["user_id"])
        username = user["username"] if user else "Неизвестный"
        text += f"ID {msg['id']} | @{username} (ID: {msg['user_id']})\n"
        text += f"Сообщение: {msg['message'][:50]}...\n"
        text += f"Время: {msg['created_at']}\n\n"
    text += "Для ответа используйте команду /reply <user_id> <текст>"
    await callback.message.edit_text(
        text,
        reply_markup=back_to_menu_keyboard()
    )
    await callback.answer()

# ================== СПИСОК ПОЛЬЗОВАТЕЛЕЙ (админ) ==================
@dp.callback_query(F.data == "admin_users_list")
async def admin_users_list(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    users = db.get_all_users()
    if not users:
        await callback.message.edit_text("👥 Пользователей пока нет.", reply_markup=back_to_menu_keyboard())
        await callback.answer()
        return
    text = "👥 Список пользователей:\n\n"
    for user in users[:20]:
        username = user["username"] or "Без ника"
        text += f"ID: {user['id']} | @{username} | Баланс: {user['balance']:.2f}₽\n"
    if len(users) > 20:
        text += f"\n... и ещё {len(users)-20} пользователей."
    await callback.message.edit_text(text, reply_markup=back_to_menu_keyboard())
    await callback.answer()

# ================== ЗАПУСК БОТА ==================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
