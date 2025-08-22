import os
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import requests
import json

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
EXCHANGERATE_API_KEY = os.environ.get('EXCHANGERATE_API_KEY')
ADMIN_IDS = [int(admin_id) for admin_id in os.environ.get('ADMIN_IDS', '').split(',') if admin_id]

# Поддерживаемые валюты
CURRENCIES = {
    "RUB": "🇷🇺 RUB",
    "USD": "🇺🇸 USD", 
    "KZT": "🇰🇿 KZT",
    "EUR": "🇪🇺 EUR",
    "CNY": "🇨🇳 CNY"
}

# Кеширование курсов валют
cached_rates = {}
last_update = {}
CACHE_TIME = 3600  # 1 час в секундах

# Состояния пользователей
user_states = {}

# Инициализация базы данных
def init_database():
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    # Таблица пользователей
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        language_code TEXT,
        is_premium BOOLEAN,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Таблица конвертаций
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS conversions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        from_currency TEXT,
        to_currency TEXT,
        amount REAL,
        result REAL,
        conversion_rate REAL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    # Таблица запросов курсов
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS rate_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    # Таблица действий
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action_type TEXT,
        details TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (user_id)
    )
    ''')
    
    conn.commit()
    conn.close()

# Функции для работы с базой данных
def save_user(update: Update):
    user = update.effective_user
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT OR REPLACE INTO users 
    (user_id, username, first_name, last_name, language_code, is_premium, last_activity)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        user.id,
        user.username,
        user.first_name,
        user.last_name,
        user.language_code,
        user.is_premium if hasattr(user, 'is_premium') else False,
        datetime.now()
    ))
    
    conn.commit()
    conn.close()

def log_conversion(user_id, from_currency, to_currency, amount, result, conversion_rate):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO conversions 
    (user_id, from_currency, to_currency, amount, result, conversion_rate)
    VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, from_currency, to_currency, amount, result, conversion_rate))
    
    conn.commit()
    conn.close()

def log_rate_request(user_id):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO rate_requests (user_id) VALUES (?)
    ''', (user_id,))
    
    conn.commit()
    conn.close()

def log_user_action(user_id, action_type, details=None):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    INSERT INTO user_actions (user_id, action_type, details)
    VALUES (?, ?, ?)
    ''', (user_id, action_type, details))
    
    conn.commit()
    conn.close()

def update_user_activity(user_id):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    UPDATE users SET last_activity = ? WHERE user_id = ?
    ''', (datetime.now(), user_id))
    
    conn.commit()
    conn.close()

def get_user_stats(user_id):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM conversions WHERE user_id = ?', (user_id,))
    conversions_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM rate_requests WHERE user_id = ?', (user_id,))
    rate_requests_count = cursor.fetchone()[0]
    
    cursor.execute('SELECT created_at FROM users WHERE user_id = ?', (user_id,))
    join_date = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        'conversions_count': conversions_count,
        'rate_requests_count': rate_requests_count,
        'join_date': join_date
    }

def get_all_users():
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT u.user_id, u.username, u.first_name, u.last_name, 
           u.created_at, u.last_activity,
           COUNT(DISTINCT c.id) as conversions_count,
           COUNT(DISTINCT r.id) as rate_requests_count
    FROM users u
    LEFT JOIN conversions c ON u.user_id = c.user_id
    LEFT JOIN rate_requests r ON u.user_id = r.user_id
    GROUP BY u.user_id
    ORDER BY u.last_activity DESC
    ''')
    
    users = cursor.fetchall()
    conn.close()
    return users

def get_user_by_id(user_id):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT u.*, 
           COUNT(DISTINCT c.id) as conversions_count,
           COUNT(DISTINCT r.id) as rate_requests_count
    FROM users u
    LEFT JOIN conversions c ON u.user_id = c.user_id
    LEFT JOIN rate_requests r ON u.user_id = r.user_id
    WHERE u.user_id = ?
    GROUP BY u.user_id
    ''', (user_id,))
    
    user = cursor.fetchone()
    conn.close()
    return user
 
def get_user_conversions(user_id, page=0, limit=5):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    # Get total count
    cursor.execute('SELECT COUNT(*) FROM conversions WHERE user_id = ?', (user_id,))
    total_count = cursor.fetchone()[0]
    
    # Get paginated results
    offset = page * limit
    cursor.execute('''
    SELECT from_currency, to_currency, amount, result, conversion_rate, created_at
    FROM conversions 
    WHERE user_id = ?
    ORDER BY created_at DESC
    LIMIT ? OFFSET ?
    ''', (user_id, limit, offset))
    
    conversions = cursor.fetchall()
    conn.close()
    return conversions, total_count

def get_recent_conversions(limit=10):
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('''
    SELECT c.*, u.username, u.first_name 
    FROM conversions c
    JOIN users u ON c.user_id = u.user_id
    ORDER BY c.created_at DESC
    LIMIT ?
    ''', (limit,))
    
    conversions = cursor.fetchall()
    conn.close()
    return conversions

def get_bot_stats():
    conn = sqlite3.connect('currency_bot.db')
    cursor = conn.cursor()
    
    cursor.execute('SELECT COUNT(*) FROM users')
    total_users = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM conversions')
    total_conversions = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM rate_requests')
    total_rate_requests = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > datetime("now", "-1 day")')
    active_users_24h = cursor.fetchone()[0]
    
    cursor.execute('SELECT COUNT(*) FROM users WHERE last_activity > datetime("now", "-7 days")')
    active_users_7d = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        'total_users': total_users,
        'total_conversions': total_conversions,
        'total_rate_requests': total_rate_requests,
        'active_users_24h': active_users_24h,
        'active_users_7d': active_users_7d
    }

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    save_user(update)
    update_user_activity(user.id)
    log_user_action(user.id, 'start_command')
    
    welcome_text = (
        "Добро пожаловать в конвертер валют!\n\n"
        "Доступные команды:\n"
        "/convert - Конвертировать валюту\n"
        "/rates - Посмотреть текущие курсы\n"
        "/help - Помощь\n\n"
        "Выберите действие:"
    )
    
    keyboard = [
        [InlineKeyboardButton("Конвертировать", callback_data='convert')],
        [InlineKeyboardButton("Курсы валют", callback_data='rates')],
        [InlineKeyboardButton("Помощь", callback_data='help')]
    ]
    
    if user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("Админ-панель", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    update_user_activity(user.id)
    log_user_action(user.id, 'help_command')
    
    help_text = (
        "<b>Конвертер валют - Помощь</b>\n\n"
        "<b>Как использовать:</b>\n"
        "1. Нажмите /convert или кнопку 'Конвертировать'\n"
        "2. Выберите исходную валюту\n"
        "3. Выберите целевую валюту\n"
        "4. Введите сумму для конвертации\n\n"
        "<b>Доступные валюты:</b>\n"
        "🇷🇺 RUB - Российский рубль\n"
        "🇺🇸 USD - Доллар США\n"
        "🇰🇿 KZT - Казахстанский тенге\n"
        "🇪🇺 EUR - Евро\n"
        "🇨🇳 CNY - Китайский юань\n\n"
        "<b>Команды:</b>\n"
        "/start - Главное меню\n"
        "/convert - Конвертировать валюту\n"
        "/rates - Текущие курсы\n"
        "/help - Эта справка"
    )
    
    await update.message.reply_text(help_text, parse_mode='HTML')

async def get_exchange_rates(base_currency='USD'):
    """Получение курсов валют с кешированием"""
    global cached_rates, last_update, CACHE_TIME
    
    now = datetime.now()
    if base_currency in cached_rates and base_currency in last_update and (now - last_update[base_currency]).seconds < CACHE_TIME:
        logger.info(f"Используются кешированные курсы валют для {base_currency}")
        return cached_rates[base_currency]
    
    try:
        logger.info(f"Запрос новых курсов валют с API для {base_currency}")
        url = f"https://v6.exchangerate-api.com/v6/{EXCHANGERATE_API_KEY}/latest/{base_currency}"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data['result'] == 'success':
            rates = data['conversion_rates']
            cached_rates[base_currency] = rates
            last_update[base_currency] = now
            logger.info(f"Курсы валют для {base_currency} успешно обновлены: {last_update[base_currency]}")
            return rates
        else:
            logger.error(f"API вернуло ошибку: {data.get('error-type', 'Unknown error')}")
            return cached_rates.get(base_currency)
    except Exception as e:
        logger.error(f"Error fetching exchange rates for {base_currency}: {e}")
        return cached_rates.get(base_currency)

async def show_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать курсы валют"""
    user = update.effective_user
    update_user_activity(user.id)
    log_user_action(user.id, 'show_rates_command')

    keyboard = []
    for currency_code, currency_name in CURRENCIES.items():
        keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'rates_{currency_code}')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Выберите базовую валюту для просмотра курсов:", reply_markup=reply_markup)

async def convert_currency(amount, from_currency, to_currency):
    """Конвертация валюты"""
    try:
        rates = await get_exchange_rates('USD')
        
        if rates and from_currency in rates and to_currency in rates:
            amount_in_usd = amount / rates[from_currency]
            result = amount_in_usd * rates[to_currency]
            conversion_rate = rates[to_currency] / rates[from_currency]
            return round(result, 2), conversion_rate
        return None, None
    except Exception as e:
        logger.error(f"Error converting currency: {e}")
        return None, None

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-панель"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    if user.id not in ADMIN_IDS:
        await query.edit_message_text("Доступ запрещен")
        return
    
    stats = get_bot_stats()
    
    admin_text = (
        "<b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"Конвертаций: <b>{stats['total_conversions']}</b>\n"
        f"Запросов курсов: <b>{stats['total_rate_requests']}</b>\n"
        f"Активных за 24ч: <b>{stats['active_users_24h']}</b>\n"
        f"Активных за 7д: <b>{stats['active_users_7d']}</b>\n\n"
        "Выберите действие:"
    )
    
    keyboard = [
        [InlineKeyboardButton("Статистика", callback_data='admin_stats')],
        [InlineKeyboardButton("Список пользователей", callback_data='admin_users')],
        [InlineKeyboardButton("Последние конвертации", callback_data='admin_conversions')],
        [InlineKeyboardButton("Главное меню", callback_data='back_to_main')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(admin_text, parse_mode='HTML', reply_markup=reply_markup)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика для админа"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    if user.id not in ADMIN_IDS:
        await query.edit_message_text("Доступ запрещен")
        return
    
    stats = get_bot_stats()
    recent_conversions = get_recent_conversions(5)
    
    stats_text = (
        "<b>Детальная статистика</b>\n\n"
        f"Всего пользователей: <b>{stats['total_users']}</b>\n"
        f"Всего конвертаций: <b>{stats['total_conversions']}</b>\n"
        f"Всего запросов курсов: <b>{stats['total_rate_requests']}</b>\n"
        f"Активных за 24ч: <b>{stats['active_users_24h']}</b>\n"
        f"Активных за 7д: <b>{stats['active_users_7d']}</b>\n\n"
        "<b>Последние конвертации:</b>\n"
    )
    
    for conv in recent_conversions:
        user_info = f"@{conv[8]}" if conv[8] else f"{conv[9]}"
        stats_text += f"• {user_info}: {conv[2]} → {conv[3]}: {conv[4]} → {conv[5]}\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Назад в админку", callback_data='admin_panel')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(stats_text, parse_mode='HTML', reply_markup=reply_markup)

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Список пользователей для админа"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    if user.id not in ADMIN_IDS:
        await query.edit_message_text("Доступ запрещен")
        return
    
    users = get_all_users()
    users_per_page = 5
    start_index = page * users_per_page
    end_index = start_index + users_per_page
    paginated_users = users[start_index:end_index]
    total_pages = -(-len(users) // users_per_page)
    
    users_text = f"<b>Список пользователей (Страница {page + 1}/{total_pages})</b>\n\n"
    keyboard = []
    
    for i, user_data in enumerate(paginated_users, start_index + 1):
        username = user_data[1] or f"{user_data[2]} {user_data[3] or ''}".strip()
        if not username.strip():
            username = f"User_{user_data[0]}"
        
        users_text += f"{i}. {username}\n"
        users_text += f"ID: {user_data[0]}, Конвертаций: {user_data[6]}\n"
        users_text += f"Активен: {datetime.fromisoformat(user_data[5]).strftime('%Y-%m-%d %H:%M')}\n\n"
        
        # Добавляем кнопку для просмотра профиля
        btn_text = f"{username[:15]}{'...' if len(username) > 15 else ''}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f'user_{user_data[0]}')])
    
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f'admin_users_page_{page - 1}'))
    if end_index < len(users):
        pagination_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f'admin_users_page_{page + 1}'))

    if pagination_buttons:
        keyboard.append(pagination_buttons)

    keyboard.append([InlineKeyboardButton("Назад в админку", callback_data='admin_panel')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(users_text, parse_mode='HTML', reply_markup=reply_markup)

async def admin_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Профиль пользователя для админа"""
    query = update.callback_query
    await query.answer()
    
    admin_user = query.from_user
    if admin_user.id not in ADMIN_IDS:
        await query.edit_message_text("Доступ запрещен")
        return
    
    user_data = get_user_by_id(user_id)
    if not user_data:
        await query.edit_message_text("Пользователь не найден")
        return
    
    user_text = (
        f"<b>Профиль пользователя</b>\n\n"
        f"ID: <code>{user_data[0]}</code>\n"
        f"Username: @{user_data[1] or 'нет'}\n"
        f"Имя: {user_data[2] or 'нет'} {user_data[3] or ''}\n"
        f"Регистрация: {user_data[6][:19]}\n"
        f"Последняя активность: {user_data[7][:19]}\n\n"
        f"Статистика:\n"
        f"Конвертаций: {user_data[8]}\n"
        f"Запросов курсов: {user_data[9]}\n\n"
    )
    
    keyboard = [
        [InlineKeyboardButton("Открыть профиль", url=f"tg://user?id={user_id}")],
    ]
    if user_data[8] > 0:
        keyboard.append([InlineKeyboardButton(f"История конвертаций ({user_data[8]})", callback_data=f'user_convs_{user_id}_0')])

    keyboard.extend([
        [InlineKeyboardButton("К списку пользователей", callback_data='admin_users')],
        [InlineKeyboardButton("В админ-панель", callback_data='admin_panel')]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(user_text, parse_mode='HTML', reply_markup=reply_markup)

async def admin_conversions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Последние конвертации для админа"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    if user.id not in ADMIN_IDS:
        await query.edit_message_text("Доступ запрещен")
        return
    
    conversions = get_recent_conversions(10)
    
    conv_text = "<b>Последние конвертации</b>\n\n"
    
    for conv in conversions:
        user_info = f"@{conv[8]}" if conv[8] else f"{conv[9]}"
        conv_text += (
            f"{user_info} (ID: {conv[1]})\n"
            f"{conv[2]} → {conv[3]}\n"
            f"{conv[4]} → {conv[5]} (курс: {conv[6]:.4f})\n"
            f"{conv[7]}\n"
        )
    
    keyboard = [[InlineKeyboardButton("Назад в админку", callback_data='admin_panel')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(conv_text, parse_mode='HTML', reply_markup=reply_markup)

async def admin_user_conversions(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, page: int = 0):
    """История конвертаций пользователя для админа"""
    query = update.callback_query
    await query.answer()

    admin_user = query.from_user
    if admin_user.id not in ADMIN_IDS:
        await query.edit_message_text("Доступ запрещен")
        return

    limit = 5
    conversions, total_count = get_user_conversions(user_id, page, limit)
    user_data = get_user_by_id(user_id)
    username = user_data[1] or f"{user_data[2]} {user_data[3] or ''}".strip()

    if not conversions:
        await query.edit_message_text(f"У пользователя {username} нет истории конвертаций.",
                                      reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад к профилю", callback_data=f'user_{user_id}')]]))
        return

    total_pages = -(-total_count // limit)

    conv_text = f"<b>Конвертации для {username}</b> (Стр. {page + 1}/{total_pages})\n\n"

    for conv in conversions:
        conv_text += (
            f"{conv[0]} → {conv[1]}\n"
            f"{conv[2]} → {conv[3]} (курс: {conv[4]:.4f})\n"
            f"{conv[5][:19]}\n"
        )

    keyboard = []
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f'user_convs_{user_id}_{page - 1}'))
    if (page + 1) * limit < total_count:
        pagination_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f'user_convs_{user_id}_{page + 1}'))

    if pagination_buttons:
        keyboard.append(pagination_buttons)

    keyboard.append([InlineKeyboardButton("Назад к профилю", callback_data=f'user_{user_id}')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(conv_text, parse_mode='HTML', reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопок"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    save_user(update)
    update_user_activity(user.id)
    
    # Обработка админских команд
    if query.data == 'admin_panel':
        await admin_panel(update, context)
        return
    elif query.data == 'admin_stats':
        await admin_stats(update, context)
        return
    elif query.data.startswith('admin_users_page_'):
        page = int(query.data.split('_')[-1])
        await admin_users(update, context, page=page)
        return
    elif query.data == 'admin_users':
        await admin_users(update, context, page=0)
        return
    elif query.data == 'admin_conversions':
        await admin_conversions(update, context)
        return
    elif query.data.startswith('user_convs_'):
        parts = query.data.split('_')
        user_id = int(parts[2])
        page = int(parts[3])
        await admin_user_conversions(update, context, user_id, page)
        return
    elif query.data.startswith('user_'):
        user_id = int(query.data.split('_')[1])
        await admin_user_profile(update, context, user_id)
        return
    
    log_user_action(user.id, 'button_click', query.data)
    
    if query.data == 'convert':
        # Выбор исходной валюты
        keyboard = []
        for currency in CURRENCIES:
            keyboard.append([InlineKeyboardButton(CURRENCIES[currency], callback_data=f'from_{currency}')])
        
        keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_main')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите исходную валюту:", reply_markup=reply_markup)
    
    elif query.data == 'rates':
        keyboard = []
        for currency_code, currency_name in CURRENCIES.items():
            keyboard.append([InlineKeyboardButton(currency_name, callback_data=f'rates_{currency_code}')])
        
        keyboard.append([InlineKeyboardButton("Назад", callback_data='back_to_main')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Выберите базовую валюту для просмотра курсов:", reply_markup=reply_markup)
        return
    elif query.data.startswith('rates_'):
        base_currency = query.data.split('_')[1]
        await show_rates_for_query(update, context, base_currency)
        return
    
    elif query.data == 'help':
        help_text = (
            "<b>Конвертер валют - Помощь</b>\n\n"
            "<b>Как использовать:</b>\n"
            "1. Выберите 'Конвертировать'\n"
            "2. Выберите исходную валюту\n"
            "3. Выберите целевую валюту\n"
            "4. Введите сумму\n\n"
            "<b>Доступные валюты:</b>\n"
            "🇷🇺 RUB, 🇺🇸 USD, 🇰🇿 KZT, 🇪🇺 EUR, 🇨🇳 CNY"
        )
        keyboard = [[InlineKeyboardButton("Назад", callback_data='back_to_main')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(help_text, parse_mode='HTML', reply_markup=reply_markup)
    
    elif query.data == 'back_to_main':
        keyboard = [
            [InlineKeyboardButton("Конвертировать", callback_data='convert')],
            [InlineKeyboardButton("Курсы валют", callback_data='rates')],
            [InlineKeyboardButton("Помощь", callback_data='help')]
        ]
        
        if user.id in ADMIN_IDS:
            keyboard.append([InlineKeyboardButton("Админ-панель", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Главное меню:", reply_markup=reply_markup)
    
    elif query.data.startswith('from_'):
        from_currency = query.data.split('_')[1]
        user_states[user.id] = {'from_currency': from_currency}
        
        keyboard = []
        for currency in CURRENCIES:
            if currency != from_currency:
                keyboard.append([InlineKeyboardButton(CURRENCIES[currency], callback_data=f'to_{currency}')])
        
        keyboard.append([InlineKeyboardButton("Назад", callback_data='convert')])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"Исходная валюта: {CURRENCIES[from_currency]}\nВыберите целевую валюту:",
            reply_markup=reply_markup
        )
    
    elif query.data.startswith('to_'):
        to_currency = query.data.split('_')[1]
        
        if user.id in user_states and 'from_currency' in user_states[user.id]:
            from_currency = user_states[user.id]['from_currency']
            user_states[user.id]['to_currency'] = to_currency
            
            await query.edit_message_text(
                f"Конвертация: {CURRENCIES[from_currency]} → {CURRENCIES[to_currency]}\n"
                "Введите сумму для конвертации:"
            )
        else:
            await query.edit_message_text("Ошибка. Начните заново.")

async def show_rates_for_query(update: Update, context: ContextTypes.DEFAULT_TYPE, base_currency: str = 'USD'):
    """Показать курсы валют для callback query"""
    query = update.callback_query
    user = query.from_user
    update_user_activity(user.id)
    log_user_action(user.id, 'show_rates', f'base:{base_currency}')
    log_rate_request(user.id)
    
    rates = await get_exchange_rates(base_currency)
    
    if rates:
        rates_text = f"<b>Текущие курсы валют (к {base_currency}):</b>\n"
        
        if base_currency in last_update:
            rates_text += f"<i>Обновлено: {last_update[base_currency].strftime('%d.%m.%Y %H:%M')}</i>\n\n"
        else:
            rates_text += "<i>Данные могут быть неактуальными</i>\n\n"
        
        for currency in CURRENCIES:
            if currency in rates and currency != base_currency:
                rate = rates[currency]
                rates_text += f"{CURRENCIES[currency]}: {rate:.4f}\n"
        
        keyboard = [
            [InlineKeyboardButton("Выбрать другую валюту", callback_data='rates')],
            [InlineKeyboardButton("Назад", callback_data='back_to_main')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(rates_text, parse_mode='HTML', reply_markup=reply_markup)
    else:
        await query.edit_message_text("Не удалось получить курсы валют. Попробуйте позже.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user = update.effective_user
    save_user(update)
    update_user_activity(user.id)
    
    text = update.message.text
    
    # Проверка на админские команды
    if user.id in ADMIN_IDS and text.startswith('/'):
        if text == '/stats':
            stats = get_bot_stats()
            stats_text = (
                "<b>Статистика бота</b>\n\n"
                f"Пользователей: {stats['total_users']}\n"
                f"Конвертаций: {stats['total_conversions']}\n"
                f"Запросов курсов: {stats['total_rate_requests']}\n"
                f"Активных (24ч): {stats['active_users_24h']}\n"
                f"Активных (7д): {stats['active_users_7d']}"
            )
            await update.message.reply_text(stats_text, parse_mode='HTML')
            return
    
    if user.id in user_states and 'from_currency' in user_states[user.id] and 'to_currency' in user_states[user.id]:
        try:
            amount = float(text.replace(',', '.'))
            if amount <= 0:
                await update.message.reply_text("Сумма должна быть положительным числом.")
                return
            
            from_currency = user_states[user.id]['from_currency']
            to_currency = user_states[user.id]['to_currency']
            
            result, conversion_rate = await convert_currency(amount, from_currency, to_currency)
            
            if result is not None:
                # Логируем конвертацию
                log_conversion(user.id, from_currency, to_currency, amount, result, conversion_rate)
                log_user_action(user.id, 'conversion_success', 
                               f"{amount} {from_currency} -> {result} {to_currency}")
                
                rates = await get_exchange_rates('USD')
                if rates:
                    from_rate = rates.get(from_currency, 1)
                    to_rate = rates.get(to_currency, 1)
                    
                    response_text = (
                        f"<b>Результат конвертации:</b>\n\n"
                        f"{amount} {CURRENCIES[from_currency]} =\n"
                        f"{result} {CURRENCIES[to_currency]}\n\n"
                        f"<b>Курсы:</b>\n"
                        f"1 {from_currency} = {to_rate/from_rate:.4f} {to_currency}\n"
                        f"1 {to_currency} = {from_rate/to_rate:.4f} {from_currency}"
                    )
                    
                    if 'USD' in last_update:
                        response_text += f"\n\n<i>Данные актуальны на: {last_update['USD'].strftime('%d.%m.%Y %H:%M')}</i>"
                    
                else:
                    response_text = f"{amount} {CURRENCIES[from_currency]} = {result} {CURRENCIES[to_currency]}"
                
                keyboard = [
                    [InlineKeyboardButton("Новый перевод", callback_data='convert')],
                    [InlineKeyboardButton("Курсы валют", callback_data='rates')],
                    [InlineKeyboardButton("Главное меню", callback_data='back_to_main')]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(response_text, parse_mode='HTML', reply_markup=reply_markup)
            else:
                log_user_action(user.id, 'conversion_failed')
                await update.message.reply_text("Ошибка конвертации. Попробуйте позже.")
            
            # Очищаем состояние пользователя
            if user.id in user_states:
                del user_states[user.id]
                
        except ValueError:
            log_user_action(user.id, 'conversion_error', 'invalid_number')
            await update.message.reply_text("Пожалуйста, введите корректное число.")
    else:
        keyboard = [
            [InlineKeyboardButton("Конвертировать", callback_data='convert')],
            [InlineKeyboardButton("Курсы валют", callback_data='rates')],
            [InlineKeyboardButton("Помощь", callback_data='help')]
        ]
        
        if user.id in ADMIN_IDS:
            keyboard.append([InlineKeyboardButton("Админ-панель", callback_data='admin_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Выберите действие:", reply_markup=reply_markup)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ошибок"""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """Основная функция"""
    if not TELEGRAM_TOKEN or not EXCHANGERATE_API_KEY or not ADMIN_IDS:
        logger.error("Ошибка: Не заданы переменные окружения: TELEGRAM_TOKEN, EXCHANGERATE_API_KEY, ADMIN_IDS")
        print("Пожалуйста, установите переменные окружения: TELEGRAM_TOKEN, EXCHANGERATE_API_KEY, ADMIN_IDS")
        return

    # Инициализация базы данных
    init_database()
    print("База данных инициализирована")
    
    # Создаем приложение
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rates", show_rates))
    application.add_handler(CommandHandler("stats", lambda u, c: None))  # Для админов
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    
    # Запускаем бота
    print("Бот запущен...")
    application.run_polling()

if __name__ == "__main__":
    main()