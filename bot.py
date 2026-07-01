import os
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Any
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    SuccessfulPayment, Document, PhotoSize, InlineKeyboardMarkup,
    InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncio
from aiohttp import web

# ================== КОНФИГУРАЦИЯ ==================
BOT_TOKEN = "8918867676:AAHixz0SseKQ9eqV99oDPI-CTwdQsXrO9mI"
ADMIN_IDS = [7727618205, 8297446667]  # список админов

# Реквизиты карты для пополнения (замените на свои)
CARD_DETAILS = """
💳 Реквизиты для пополнения баланса:

Номер карты: 1234 5678 9012 3456
Получатель: Иванов Иван Иванович
Банк: Тинькофф

❗ После перевода отправьте скриншот чека в этот чат.
"""

# Минимальная сумма пополнения
MIN_DEPOSIT = 40

# Статусы пользователей в зависимости от количества покупок
STATUSES = [
    (0, "Новый клиент"),
    (3, "Постоянный клиент"),
    (10, "VIP клиент"),
    (25, "Легенда"),
]

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        # Пользователи
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0,
                registered_at TEXT,
                referrer_id INTEGER DEFAULT NULL,
                promo_used INTEGER DEFAULT 0
            )
        """)
        # Аккаунты (физические номера)
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
                buyer_id INTEGER REFERENCES users(id),
                admin_id INTEGER,  -- кто добавил аккаунт
                is_departure BOOLEAN DEFAULT 0  -- флаг "с отлетой"
            )
        """)
        # Покупки
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                account_id INTEGER REFERENCES accounts(id),
                purchase_date TEXT,
                price REAL,
                admin_earned REAL DEFAULT 0  -- сколько заработал админ
            )
        """)
        # Отзывы (необязательно)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                review_text TEXT,
                created_at TEXT
            )
        """)
        # Сообщения техподдержки
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
        # Заявки на пополнение (при ручном переводе)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS deposit_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                amount REAL,
                screenshot_file_id TEXT,
                status TEXT DEFAULT 'pending',  -- pending, approved, rejected
                created_at TEXT,
                processed_at TEXT,
                admin_id INTEGER
            )
        """)
        # Баланс админов (заработанные средства)
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_balances (
                admin_id INTEGER PRIMARY KEY,
                balance REAL DEFAULT 0,
                total_earned REAL DEFAULT 0
            )
        """)
        # Промокоды
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                code TEXT PRIMARY KEY,
                bonus REAL,
                uses_limit INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                expires_at TEXT
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
                "referrer_id": row[4],
                "promo_used": row[5],
            }
        return None

    def create_user(self, user_id: int, username: str = None, referrer_id: int = None):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT INTO users (id, username, balance, registered_at, referrer_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, 0.0, now, referrer_id),
        )
        self.conn.commit()
        # Если есть реферер, начисляем ему бонус (например, 10₽)
        if referrer_id:
            self.update_balance(referrer_id, 10.0)

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

    def get_purchase_count(self, user_id: int) -> int:
        self.cursor.execute(
            "SELECT COUNT(*) FROM purchases WHERE user_id = ?",
            (user_id,)
        )
        row = self.cursor.fetchone()
        return row[0] if row else 0

    def get_user_status(self, user_id: int) -> str:
        count = self.get_purchase_count(user_id)
        for threshold, status in STATUSES:
            if count >= threshold:
                return status
        return "Новый клиент"

    # ------------------ АККАУНТЫ ------------------
    def get_available_accounts(self, country: str = None, departure: bool = False) -> List[Dict[str, Any]]:
        query = "SELECT * FROM accounts WHERE is_sold = 0 AND is_departure = ?"
        params = [1 if departure else 0]
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
                "admin_id": row[10],
                "is_departure": row[11],
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
                "admin_id": row[10],
                "is_departure": row[11],
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
        # Начисляем админу (кто добавил аккаунт) полную сумму (можно изменить процент)
        admin_id = account["admin_id"]
        if admin_id:
            self.update_admin_balance(admin_id, price)

        self.cursor.execute(
            "INSERT INTO purchases (user_id, account_id, purchase_date, price, admin_earned) VALUES (?, ?, ?, ?, ?)",
            (user_id, account_id, now, price, price if admin_id else 0),
        )
        self.conn.commit()
        return True

    def add_account(self, country: str, number: str, code: str, date: str, price: float,
                    description: str = "", file_id: str = None, admin_id: int = None, is_departure: bool = False):
        self.cursor.execute(
            "INSERT INTO accounts (country, number, code, date, price, description, file_id, admin_id, is_departure) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (country, number, code, date, price, description, file_id, admin_id, 1 if is_departure else 0),
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
                "admin_id": row[10],
                "is_departure": row[11],
                "purchase_date": row[12],
                "paid_price": row[13],
            })
        return purchases

    # ------------------ ЗАЯВКИ НА ПОПОЛНЕНИЕ ------------------
    def add_deposit_request(self, user_id: int, amount: float, screenshot_file_id: str):
        now = datetime.now().isoformat()
        self.cursor.execute(
            "INSERT INTO deposit_requests (user_id, amount, screenshot_file_id, created_at) VALUES (?, ?, ?, ?)",
            (user_id, amount, screenshot_file_id, now),
        )
        self.conn.commit()
        return self.cursor.lastrowid

    def get_pending_deposits(self) -> List[Dict[str, Any]]:
        self.cursor.execute(
            "SELECT * FROM deposit_requests WHERE status = 'pending' ORDER BY created_at"
        )
        rows = self.cursor.fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row[0],
                "user_id": row[1],
                "amount": row[2],
                "screenshot_file_id": row[3],
                "status": row[4],
                "created_at": row[5],
                "processed_at": row[6],
                "admin_id": row[7],
            })
        return result

    def approve_deposit(self, request_id: int, admin_id: int):
        self.cursor.execute(
            "UPDATE deposit_requests SET status = 'approved', processed_at = ?, admin_id = ? WHERE id = ?",
            (datetime.now().isoformat(), admin_id, request_id),
        )
        # Начисляем баланс пользователю
        self.cursor.execute("SELECT user_id, amount FROM deposit_requests WHERE id = ?", (request_id,))
        row = self.cursor.fetchone()
        if row:
            self.update_balance(row[0], row[1])
        self.conn.commit()

    def reject_deposit(self, request_id: int, admin_id: int):
        self.cursor.execute(
            "UPDATE deposit_requests SET status = 'rejected', processed_at = ?, admin_id = ? WHERE id = ?",
            (datetime.now().isoformat(), admin_id, request_id),
        )
        self.conn.commit()

    # ------------------ БАЛАНС АДМИНОВ ------------------
    def update_admin_balance(self, admin_id: int, amount: float):
        # Увеличиваем баланс админа
        self.cursor.execute(
            "INSERT INTO admin_balances (admin_id, balance, total_earned) VALUES (?, ?, ?) "
            "ON CONFLICT(admin_id) DO UPDATE SET balance = balance + ?, total_earned = total_earned + ?",
            (admin_id, amount, amount, amount, amount),
        )
        self.conn.commit()

    def get_admin_balance(self, admin_id: int) -> float:
        self.cursor.execute("SELECT balance FROM admin_balances WHERE admin_id = ?", (admin_id,))
        row = self.cursor.fetchone()
        return row[0] if row else 0.0

    def get_admin_total_earned(self, admin_id: int) -> float:
        self.cursor.execute("SELECT total_earned FROM admin_balances WHERE admin_id = ?", (admin_id,))
        row = self.cursor.fetchone()
        return row[0] if row else 0.0

    # ------------------ ПРОМОКОДЫ ------------------
    def use_promocode(self, code: str, user_id: int) -> Optional[float]:
        # Проверяем, существует ли промокод, не истёк ли, не превышен ли лимит
        now = datetime.now().isoformat()
        self.cursor.execute(
            "SELECT bonus, uses_limit, used_count, expires_at FROM promocodes WHERE code = ? AND (expires_at IS NULL OR expires_at > ?)",
            (code, now)
        )
        row = self.cursor.fetchone()
        if not row:
            return None
        bonus, limit, used, expires = row
        if used >= limit:
            return None
        # Проверяем, не использовал ли этот пользователь уже этот промокод (можно хранить в отдельной таблице, для простоты проверим поле promo_used)
        user = self.get_user(user_id)
        if user and user.get("promo_used", 0) >= 1:
            return None  # уже использовал промокод
        # Начисляем бонус
        self.update_balance(user_id, bonus)
        # Обновляем использованные
        self.cursor.execute(
            "UPDATE promocodes SET used_count = used_count + 1 WHERE code = ?",
            (code,)
        )
        # Отмечаем, что пользователь использовал промокод (можно хранить список использованных, но для простоты отметим)
        self.cursor.execute("UPDATE users SET promo_used = 1 WHERE id = ?", (user_id,))
        self.conn.commit()
        return bonus

    def add_promocode(self, code: str, bonus: float, limit: int = 1, expires_at: str = None):
        self.cursor.execute(
            "INSERT INTO promocodes (code, bonus, uses_limit, expires_at) VALUES (?, ?, ?, ?)",
            (code, bonus, limit, expires_at),
        )
        self.conn.commit()

    # ------------------ СТАТИСТИКА ------------------
    def get_total_revenue(self) -> float:
        self.cursor.execute("SELECT SUM(price) FROM purchases")
        row = self.cursor.fetchone()
        return row[0] if row[0] else 0.0

    def get_total_purchases(self) -> int:
        self.cursor.execute("SELECT COUNT(*) FROM purchases")
        row = self.cursor.fetchone()
        return row[0] if row else 0

    def get_top_buyers(self, limit: int = 10) -> List[Dict[str, Any]]:
        self.cursor.execute("""
            SELECT user_id, COUNT(*) as purchases_count, SUM(price) as total_spent
            FROM purchases
            GROUP BY user_id
            ORDER BY purchases_count DESC
            LIMIT ?
        """, (limit,))
        rows = self.cursor.fetchall()
        result = []
        for row in rows:
            user = self.get_user(row[0])
            username = user["username"] if user else "Неизвестный"
            result.append({
                "user_id": row[0],
                "username": username,
                "purchases": row[1],
                "total_spent": row[2],
            })
        return result

    def get_all_users_count(self) -> int:
        self.cursor.execute("SELECT COUNT(*) FROM users")
        row = self.cursor.fetchone()
        return row[0] if row else 0

    def close(self):
        self.conn.close()

# ================== ИНИЦИАЛИЗАЦИЯ БД ==================
db = Database("fizer_shop.db")

# ================== КЛАВИАТУРЫ ==================
def main_menu_keyboard(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="📱 Аккаунты", callback_data="accounts")
    builder.button(text="✈️ Аккаунты с отлетой", callback_data="accounts_departure")
    builder.button(text="👤 Профиль", callback_data="profile")
    builder.button(text="🎫 Промокод", callback_data="promocode")
    builder.button(text="🏆 Топ покупателей", callback_data="top_buyers")
    builder.button(text="📞 ПОДДЕРЖКА", callback_data="support")
    builder.adjust(2)
    return builder.as_markup()

def back_to_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="main_menu")
    return builder.as_markup()

def country_keyboard(departure: bool = False):
    countries = ["РФ", "КЗ", "УКР", "Беларусь", "Узбекистан", "Азербайджан"]
    builder = InlineKeyboardBuilder()
    for country in countries:
        callback = f"country_{country}_{1 if departure else 0}"
        builder.button(text=country, callback_data=callback)
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

def account_keyboard(accounts: List[Dict[str, Any]], departure: bool = False):
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        text = f"{acc['country']} - {acc['number']} ({acc['price']}₽)"
        builder.button(text=text, callback_data=f"buy_account_{acc['id']}")
    builder.button(text="🔙 Назад", callback_data="accounts" if not departure else "accounts_departure")
    builder.adjust(1)
    return builder.as_markup()

def payment_keyboard(account_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить", callback_data=f"pay_{account_id}")
    builder.button(text="🔙 Назад", callback_data="accounts")
    return builder.as_markup()

def review_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="✍️ Оставить отзыв", callback_data="leave_review")
    builder.button(text="🏠 В главное меню", callback_data="main_menu")
    return builder.as_markup()

def admin_panel_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Заявки на пополнение", callback_data="admin_deposits")
    builder.button(text="➕ Добавить аккаунт", callback_data="admin_add_account")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="💵 Мой баланс", callback_data="admin_balance")
    builder.button(text="💸 Запросить вывод", callback_data="admin_withdraw")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def admin_deposit_keyboard(requests: List[Dict[str, Any]]):
    builder = InlineKeyboardBuilder()
    for req in requests:
        text = f"Заявка #{req['id']} - {req['amount']}₽ от {req['user_id']}"
        builder.button(text=text, callback_data=f"admin_deposit_{req['id']}")
    builder.button(text="🔙 Назад", callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()

def admin_deposit_action_keyboard(request_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"approve_deposit_{request_id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_deposit_{request_id}")
    builder.button(text="🔙 Назад", callback_data="admin_deposits")
    builder.adjust(2)
    return builder.as_markup()

def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Пополнить баланс", callback_data="deposit")
    builder.button(text="📱 Мои аккаунты", callback_data="my_accounts")
    builder.button(text="🔙 Назад", callback_data="main_menu")
    builder.adjust(2)
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
    builder.button(text="🔙 Назад", callback_data="profile")
    builder.adjust(1)
    return builder.as_markup()

# ================== FSM СОСТОЯНИЯ ==================
class DepositStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_screenshot = State()

class ReviewStates(StatesGroup):
    waiting_for_review = State()

class SupportStates(StatesGroup):
    waiting_for_message = State()

class PromocodeStates(StatesGroup):
    waiting_for_code = State()

class AdminAddAccountStates(StatesGroup):
    waiting_country = State()
    waiting_number = State()
    waiting_code = State()
    waiting_date = State()
    waiting_price = State()
    waiting_description = State()
    waiting_file = State()
    waiting_departure = State()

class AdminWithdrawStates(StatesGroup):
    waiting_amount = State()

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
            logger.error(f"Ошибка отправки файла: {e}")
            await bot.send_message(chat_id, text, reply_markup=back_to_menu_keyboard())
    else:
        await bot.send_message(chat_id, text, reply_markup=back_to_menu_keyboard())

# ================== ОБРАБОТЧИКИ КОМАНД ==================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Без ника"
    # Проверяем реферальную ссылку
    referrer_id = None
    if message.text and "ref_" in message.text:
        try:
            ref = int(message.text.split("ref_")[1])
            if ref != user_id and db.get_user(ref):
                referrer_id = ref
        except:
            pass

    if not db.get_user(user_id):
        db.create_user(user_id, username, referrer_id)

    # Отправка приветствия
    await message.answer(
        "Добро пожаловать в FIZER SHOP!\n\n"
        "— Почему именно мы:\n"
        "  • Более 700+ живых отзывов\n"
        "  • Молниеносная выдача\n"
        "  • Постоянные раздачи и бонусы в канале\n\n"
        "Выбери раздел ниже, чтобы продолжить:",
        reply_markup=main_menu_keyboard(user_id)
    )

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав администратора.")
        return
    await message.answer(
        "🛠 Админ-панель FIZER SHOP\nВыберите действие:",
        reply_markup=admin_panel_keyboard()
    )

# ================== ОБРАБОТЧИКИ CALLBACK ==================
@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: CallbackQuery):
    user_id = callback.from_user.id
    await callback.message.edit_text(
        "Добро пожаловать в FIZER SHOP!\n\n"
        "— Почему именно мы:\n"
        "  • Более 700+ живых отзывов\n"
        "  • Молниеносная выдача\n"
        "  • Постоянные раздачи и бонусы в канале\n\n"
        "Выбери раздел ниже, чтобы продолжить:",
        reply_markup=main_menu_keyboard(user_id)
    )
    await callback.answer()

@dp.callback_query(F.data == "accounts")
async def accounts_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌍 Выберите страну аккаунта:",
        reply_markup=country_keyboard(departure=False)
    )
    await callback.answer()

@dp.callback_query(F.data == "accounts_departure")
async def accounts_departure_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "✈️ Аккаунты с отлетой\nВыберите страну:",
        reply_markup=country_keyboard(departure=True)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("country_"))
async def select_country(callback: CallbackQuery):
    parts = callback.data.split("_")
    country = parts[1]
    is_departure = bool(int(parts[2]))
    accounts = db.get_available_accounts(country, departure=is_departure)
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
        reply_markup=account_keyboard(accounts, departure=is_departure)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_account_"))
async def buy_account(callback: CallbackQuery):
    account_id = int(callback.data.split("_")[2])
    account = db.get_account(account_id)
    if not account or account["is_sold"]:
        await callback.answer("Этого аккаунта уже нет в наличии.", show_alert=True)
        await callback.message.edit_text("Выберите страну:", reply_markup=country_keyboard(departure=False))
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
            "Пополните баланс через раздел «Профиль».",
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
        await callback.message.edit_text("Выберите страну:", reply_markup=country_keyboard(departure=False))
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

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = db.get_user(user_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    purchase_count = db.get_purchase_count(user_id)
    username = user["username"] or "Без ника"
    balance = user["balance"]
    status = db.get_user_status(user_id)
    registered_at = user["registered_at"]
    try:
        dt = datetime.fromisoformat(registered_at)
        reg_date = dt.strftime("%d.%m.%Y")
    except:
        reg_date = registered_at

    # Ссылка для приглашения
    invite_link = f"https://t.me/{bot.username}?start=ref_{user_id}"

    text = (
        f"👤 Твой профиль\n\n"
        f"ID: {user_id}\n"
        f"Имя: {username}\n"
        f"Юзернейм: @{username if username != 'Без ника' else 'не указан'}\n\n"
        f"Покупок всего: {purchase_count}\n"
        f"Статус: {status}\n"
        f"Доступ в приват: {'активен' if purchase_count >= 5 else 'не активен'}\n\n"
        f"Приглашено друзей: 0\n\n"  # можно реализовать подсчёт рефералов
        f"Зарабатывай с нами\n"
        f"Поделись ссылкой с другом, он зайдёт в бота, а ты попадёшь в реферальную статистику:\n"
        f"{invite_link}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=profile_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "deposit")
async def deposit_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"💰 Пополнение баланса\n\n"
        f"{CARD_DETAILS}\n\n"
        f"После перевода введите сумму пополнения (минимум {MIN_DEPOSIT}₽):",
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
    await state.update_data(amount=amount)
    await message.answer(
        "📸 Теперь отправьте скриншот чека (фото или документ).",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(DepositStates.waiting_for_screenshot)

@dp.message(DepositStates.waiting_for_screenshot, F.photo | F.document)
async def process_deposit_screenshot(message: Message, state: FSMContext):
    # Получаем file_id
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    else:
        await message.answer("❌ Пожалуйста, отправьте фото или документ.")
        return

    data = await state.get_data()
    amount = data.get("amount")
    user_id = message.from_user.id

    # Сохраняем заявку
    request_id = db.add_deposit_request(user_id, amount, file_id)

    # Уведомляем админов
    for admin_id in ADMIN_IDS:
        try:
            # Отправляем админу сообщение с заявкой
            await bot.send_message(
                admin_id,
                f"📩 Новая заявка на пополнение!\n"
                f"ID заявки: {request_id}\n"
                f"Пользователь: @{message.from_user.username or 'Без ника'} (ID: {user_id})\n"
                f"Сумма: {amount}₽",
                reply_markup=admin_deposit_action_keyboard(request_id)
            )
            # Отправляем скриншот
            await bot.send_document(admin_id, file_id)
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

    await message.answer(
        "✅ Ваша заявка на пополнение отправлена. Ожидайте подтверждения администратором.",
        reply_markup=back_to_menu_keyboard()
    )
    await state.clear()

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

# ================== ПРОМОКОД ==================
@dp.callback_query(F.data == "promocode")
async def promocode_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🎫 Введите промокод:\n\n"
        "Для отмены отправьте /cancel",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(PromocodeStates.waiting_for_code)
    await callback.answer()

@dp.message(PromocodeStates.waiting_for_code)
async def process_promocode(message: Message, state: FSMContext):
    if message.text.lower() == "/cancel":
        await message.answer("❌ Операция отменена.", reply_markup=back_to_menu_keyboard())
        await state.clear()
        return
    code = message.text.strip().upper()
    bonus = db.use_promocode(code, message.from_user.id)
    if bonus is None:
        await message.answer("❌ Недействительный или уже использованный промокод.", reply_markup=back_to_menu_keyboard())
    else:
        await message.answer(f"✅ Промокод активирован! Вы получили {bonus}₽ на баланс.", reply_markup=back_to_menu_keyboard())
    await state.clear()

# ================== ТОП ПОКУПАТЕЛЕЙ ==================
@dp.callback_query(F.data == "top_buyers")
async def top_buyers(callback: CallbackQuery):
    top = db.get_top_buyers(10)
    if not top:
        await callback.message.edit_text("Пока нет покупателей.", reply_markup=back_to_menu_keyboard())
        await callback.answer()
        return
    text = "🏆 Топ покупателей:\n\n"
    for i, buyer in enumerate(top, 1):
        text += f"{i}. @{buyer['username']} — {buyer['purchases']} покупок, потрачено {buyer['total_spent']:.2f}₽\n"
    await callback.message.edit_text(text, reply_markup=back_to_menu_keyboard())
    await callback.answer()

# ================== ПОДДЕРЖКА ==================
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
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
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

@dp.callback_query(F.data == "admin_deposits")
async def admin_deposits_list(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    requests = db.get_pending_deposits()
    if not requests:
        await callback.message.edit_text("📭 Нет новых заявок на пополнение.", reply_markup=back_to_menu_keyboard())
        await callback.answer()
        return
    text = "💰 Заявки на пополнение:\n\n"
    for req in requests:
        user = db.get_user(req["user_id"])
        username = user["username"] if user else "Неизвестный"
        text += f"ID {req['id']} | @{username} (ID: {req['user_id']}) | {req['amount']}₽\n"
    text += "\nВыберите заявку для обработки:"
    await callback.message.edit_text(
        text,
        reply_markup=admin_deposit_keyboard(requests)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_deposit_"))
async def admin_deposit_detail(callback: CallbackQuery):
    request_id = int(callback.data.split("_")[2])
    # Находим заявку
    self = db
    self.cursor.execute("SELECT * FROM deposit_requests WHERE id = ?", (request_id,))
    row = self.cursor.fetchone()
    if not row:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    req = {
        "id": row[0],
        "user_id": row[1],
        "amount": row[2],
        "screenshot_file_id": row[3],
        "status": row[4],
        "created_at": row[5],
    }
    user = db.get_user(req["user_id"])
    username = user["username"] if user else "Неизвестный"
    text = (
        f"Заявка #{req['id']}\n"
        f"Пользователь: @{username} (ID: {req['user_id']})\n"
        f"Сумма: {req['amount']}₽\n"
        f"Статус: {req['status']}\n"
        f"Создана: {req['created_at']}"
    )
    # Отправляем скриншот
    if req["screenshot_file_id"]:
        await callback.message.delete()
        await bot.send_document(
            chat_id=callback.from_user.id,
            document=req["screenshot_file_id"],
            caption=text,
            reply_markup=admin_deposit_action_keyboard(req["id"])
        )
    else:
        await callback.message.edit_text(
            text,
            reply_markup=admin_deposit_action_keyboard(req["id"])
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("approve_deposit_"))
async def admin_approve_deposit(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    request_id = int(callback.data.split("_")[2])
    db.approve_deposit(request_id, callback.from_user.id)
    # Уведомляем пользователя
    self = db
    self.cursor.execute("SELECT user_id, amount FROM deposit_requests WHERE id = ?", (request_id,))
    row = self.cursor.fetchone()
    if row:
        user_id, amount = row
        try:
            await bot.send_message(user_id, f"✅ Ваш баланс пополнен на {amount}₽. Спасибо за доверие!")
        except:
            pass
    await callback.message.edit_text("✅ Заявка подтверждена, баланс пополнен.", reply_markup=back_to_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_deposit_"))
async def admin_reject_deposit(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    request_id = int(callback.data.split("_")[2])
    db.reject_deposit(request_id, callback.from_user.id)
    # Уведомляем пользователя
    self = db
    self.cursor.execute("SELECT user_id FROM deposit_requests WHERE id = ?", (request_id,))
    row = self.cursor.fetchone()
    if row:
        try:
            await bot.send_message(row[0], "❌ Ваша заявка на пополнение отклонена. Проверьте правильность перевода и попробуйте снова.")
        except:
            pass
    await callback.message.edit_text("❌ Заявка отклонена.", reply_markup=back_to_menu_keyboard())
    await callback.answer()

# ------------------ ДОБАВЛЕНИЕ АККАУНТА (АДМИН) ------------------
@dp.callback_query(F.data == "admin_add_account")
async def admin_add_account(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.message.edit_text(
        "➕ Добавление нового аккаунта.\n"
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
    await message.answer("Введите описание (инструкция по входу, дополнительная информация):")
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

    # Спрашиваем, с отлетой или нет
    await state.update_data({
        "country": country,
        "number": number,
        "code": code,
        "date": date,
        "price": price,
        "description": description,
        "file_id": file_id,
    })
    await message.answer(
        "Это аккаунт с отлетой? (да/нет)",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(AdminAddAccountStates.waiting_departure)

@dp.message(AdminAddAccountStates.waiting_departure)
async def admin_add_departure(message: Message, state: FSMContext):
    is_departure = message.text.lower() in ["да", "yes", "true", "1"]
    data = await state.get_data()
    db.add_account(
        country=data["country"],
        number=data["number"],
        code=data["code"],
        date=data["date"],
        price=data["price"],
        description=data["description"],
        file_id=data["file_id"],
        admin_id=message.from_user.id,
        is_departure=is_departure
    )
    await message.answer(
        f"✅ Аккаунт добавлен!\n"
        f"Страна: {data['country']}\n"
        f"Номер: {data['number']}\n"
        f"Код: {data['code']}\n"
        f"Дата: {data['date']}\n"
        f"Цена: {data['price']}₽\n"
        f"Описание: {data['description'] or 'Нет'}\n"
        f"С отлетой: {'Да' if is_departure else 'Нет'}",
        reply_markup=back_to_menu_keyboard()
    )
    await state.clear()

# ------------------ СТАТИСТИКА (АДМИН) ------------------
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    total_revenue = db.get_total_revenue()
    total_purchases = db.get_total_purchases()
    total_users = db.get_all_users_count()
    text = (
        f"📊 Статистика магазина:\n\n"
        f"Всего пользователей: {total_users}\n"
        f"Всего продаж: {total_purchases}\n"
        f"Общая выручка: {total_revenue:.2f}₽"
    )
    await callback.message.edit_text(text, reply_markup=back_to_menu_keyboard())
    await callback.answer()

# ------------------ МОЙ БАЛАНС (АДМИН) ------------------
@dp.callback_query(F.data == "admin_balance")
async def admin_balance(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    admin_id = callback.from_user.id
    balance = db.get_admin_balance(admin_id)
    total_earned = db.get_admin_total_earned(admin_id)
    text = (
        f"💵 Ваш баланс:\n\n"
        f"Доступно к выводу: {balance:.2f}₽\n"
        f"Всего заработано: {total_earned:.2f}₽"
    )
    await callback.message.edit_text(text, reply_markup=back_to_menu_keyboard())
    await callback.answer()

# ------------------ ВЫВОД СРЕДСТВ (АДМИН) ------------------
@dp.callback_query(F.data == "admin_withdraw")
async def admin_withdraw(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await callback.message.edit_text(
        "💸 Введите сумму для вывода (доступный баланс можно посмотреть в разделе «Мой баланс»):",
        reply_markup=back_to_menu_keyboard()
    )
    await state.set_state(AdminWithdrawStates.waiting_amount)
    await callback.answer()

@dp.message(AdminWithdrawStates.waiting_amount)
async def admin_withdraw_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите корректную сумму (число).")
        return
    admin_id = message.from_user.id
    balance = db.get_admin_balance(admin_id)
    if amount <= 0:
        await message.answer("❌ Сумма должна быть положительной.")
        return
    if amount > balance:
        await message.answer(f"❌ У вас недостаточно средств. Доступно: {balance:.2f}₽")
        return

    # Здесь можно реализовать отправку заявки на вывод, например, в канал админов или сохранение в БД.
    # Для простоты отправим уведомление всем админам.
    for admin in ADMIN_IDS:
        if admin != admin_id:
            try:
                await bot.send_message(
                    admin,
                    f"💸 Запрос на вывод средств!\n"
                    f"Админ: @{message.from_user.username} (ID: {admin_id})\n"
                    f"Сумма: {amount}₽\n"
                    f"Обработайте запрос вручную."
                )
            except:
                pass

    # Списание с баланса админа
    # Здесь нужно реализовать уменьшение баланса, но мы пока просто уведомим.
    # Для полноценной работы нужно создать таблицу withdraw_requests и обрабатывать их.

    # Упрощённо: просто уведомляем и списываем (для теста)
    db.cursor.execute(
        "UPDATE admin_balances SET balance = balance - ? WHERE admin_id = ?",
        (amount, admin_id)
    )
    db.conn.commit()

    await message.answer(
        f"✅ Запрос на вывод {amount}₽ отправлен. Ожидайте обработки.",
        reply_markup=back_to_menu_keyboard()
    )
    await state.clear()

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

# ================== КОМАНДА ДЛЯ АДМИНОВ: ОТВЕТ В ПОДДЕРЖКЕ ==================
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

# ================== ЗАПУСК БОТА + ВЕБ-СЕРВЕР ==================
async def main():
    # Запускаем веб-сервер для Render (чтобы он видел открытый порт)
    from aiohttp import web

    async def health_check(request):
        return web.Response(text="OK", status=200)

    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)

    await asyncio.gather(
        site.start(),
        dp.start_polling(bot)
    )

if __name__ == "__main__":
    asyncio.run(main())
