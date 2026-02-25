import json
import os
import uuid
import sys
import requests
import threading
sys.stdout.reconfigure(encoding='utf-8')
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, abort, send_file
from datetime import datetime, timezone, timedelta
from functools import wraps
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder='app/templates', static_folder='app/static')
app.secret_key = 'supersecretkey'

# Настройка временной зоны для Москвы
MOSCOW_TZ = timezone(timedelta(hours=3))

def get_moscow_time():
    """Возвращает текущее время в московском часовом поясе"""
    return datetime.now(MOSCOW_TZ)

# ====================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ
DEFAULT_STEAM_SETTINGS = {
    'base_fee': 0,
    'discount_levels': [
        (0, 0)
    ],
    'individual_discounts': {}
}

global users, products, cards, steam_discount_levels, steam_base_fee, individual_discounts, stores
global achievements, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

users = {}
products = {}
cards = {}
stores = {}
achievements = {}
steam_discount_levels = DEFAULT_STEAM_SETTINGS['discount_levels']
steam_base_fee = DEFAULT_STEAM_SETTINGS['base_fee']
individual_discounts = DEFAULT_STEAM_SETTINGS['individual_discounts']
TELEGRAM_BOT_TOKEN = '7726856877:AAFIslzTXmB5FCw2zDHuPswiybUaCGxiNSw'
TELEGRAM_CHAT_ID = '-1003175110976'

USERS_FILE = 'users.json'
PAYMENTS_FILE = 'payments.json'
PRODUCTS_FILE = 'products.json'
STEAM_DISCOUNTS_FILE = 'steam_discounts.json'
STORES_FILE = 'stores.json'
MAINTENANCE_FILE = 'maintance.json'


def load_maintenance_status():
    """Загружает статус техработ из JSON"""
    try:
        with open(MAINTENANCE_FILE, 'r') as f:
            data = json.load(f)
            return data.get('enabled', False)
    except FileNotFoundError:
        return False

def save_maintenance_status(enabled: bool):
    """Сохраняет статус техработ в JSON"""
    with open(MAINTENANCE_FILE, 'w') as f:
        json.dump({"enabled": enabled}, f, indent=4)

# ====================== ПЕРЕХВАТ ВСЕХ ЗАПРОСОВ ДЛЯ ТЕХРАБОТ
@app.before_request
def check_maintenance_mode():
    """
    Блокируем обычных пользователей при включенных техработах
    Но разрешаем доступ:
    - к /login
    - к /logout
    - к админке /admin/maintenance
    """
    enabled = load_maintenance_status()
    
    allowed_paths = [
        '/login',
        '/dashboard',
        '/logout',
        '/admin/maintenance'
    ]
    
    if enabled and not request.path.startswith(tuple(allowed_paths)):
        username = session.get('username')
        if username != 'admin':
            return render_template('22.maintance.html'), 503


# ====================== ПЕРЕХВАТ ВСЕХ ЗАПРОСОВ ПРИ БАНЕ АККАУНТА
@app.before_request
def block_banned_users():
    allowed_routes = {'login', 'logout', 'banned', 'static'}

    if 'username' in session:
        user = users.get(session['username'])
        if user and user.get('status') == 'banned':
            if request.endpoint not in allowed_routes:
                return redirect(url_for('banned'))


        

# ====================== Маршрут для админки техработ
@app.route('/admin/maintenance', methods=['GET', 'POST'])
def admin_maintenance():
    """Страница включения/выключения режима техработ"""
    if 'username' not in session or session['username'] != 'admin':
        abort(403)

    enabled = load_maintenance_status()

    if request.method == 'POST':
        enabled = request.form.get('enabled') == 'on'
        save_maintenance_status(enabled)
        return redirect(url_for('admin_maintenance'))

    return render_template('22.admin_maintenance.html', enabled=enabled)



# ====================== AUTOMATIC DATA LOADING
@app.before_request
def load_data_before_request():
    """Автоматически загружает данные перед каждым запросом"""
    load_data()

# ====================== ОСНОВНЫЕ ФУНКЦИИ ДАННЫХ
def sync_user_balance(username):
    """Синхронизирует баланс пользователя с завершенными пополнениями и вычитает расходы на заказы"""
    global users
    
    if username not in users:
        return
    
    user_data = users[username]
    
    # Инициализируем балансы
    if 'balance' not in user_data:
        user_data['balance'] = {'card': 0, 'ton': 0, 'bep20': 0}
    
    # ВАЖНОЕ ИСПРАВЛЕНИЕ: Если баланс изменен вручную, просто обновляем расходы без изменения баланса
    if user_data.get('balance_manually_modified'):
        
        # Рассчитываем общие расходы
        total_expenses = 0
        if 'userorders' in user_data:
            for order in user_data['userorders']:
                total_expenses += order.get('price', 0)
        
        # Обновляем только расходы, баланс оставляем как есть
        user_data['expenses'] = total_expenses
        
        return
    
    # Стандартная логика синхронизации для автоматических операций
    current_balance = {'card': 0, 'ton': 0, 'bep20': 0}
    
    # Добавляем завершенные пополнения
    if 'topups' in user_data:
        for topup in user_data['topups']:
            if (topup.get('status') == 'completed' and 
                topup.get('payment_confirmed') == True and
                topup.get('method') in current_balance):
                current_balance[topup['method']] += topup['amount']
    
    # Вычитаем расходы на заказы
    total_expenses = 0
    if 'userorders' in user_data:
        for order in user_data['userorders']:
            total_expenses += order.get('price', 0)
    
    remaining_expenses = total_expenses
    
    # Списание расходов
    if current_balance['bep20'] > 0 and remaining_expenses > 0:
        if current_balance['bep20'] >= remaining_expenses:
            current_balance['bep20'] -= remaining_expenses
            remaining_expenses = 0
        else:
            remaining_expenses -= current_balance['bep20']
            current_balance['bep20'] = 0
    
    if remaining_expenses > 0 and current_balance['card'] > 0:
        if current_balance['card'] >= remaining_expenses:
            current_balance['card'] -= remaining_expenses
            remaining_expenses = 0
        else:
            remaining_expenses -= current_balance['card']
            current_balance['card'] = 0
    
    if remaining_expenses > 0 and current_balance['ton'] > 0:
        if current_balance['ton'] >= remaining_expenses:
            current_balance['ton'] -= remaining_expenses
            remaining_expenses = 0
        else:
            current_balance['ton'] = max(0, current_balance['ton'] - remaining_expenses)
    
    # Обновляем баланс пользователя
    user_data['balance'] = current_balance
    user_data['expenses'] = total_expenses


def load_data():
    """Загружает все данные из файлов"""
    global users, products, cards, steam_discount_levels, steam_base_fee, individual_discounts, stores
    global achievements, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    try:
        with open(USERS_FILE, 'r') as f:
            users = json.load(f)
            
        # СИНХРОНИЗАЦИЯ БАЛАНСА С ЗАВЕРШЕННЫМИ ПОПОЛНЕНИЯМИ И ЗАКАЗАМИ
        for username, user_data in users.items():
            sync_user_balance(username)
                
    except FileNotFoundError:
        users = {}

    try:
        with open(STEAM_DISCOUNTS_FILE, 'r') as f:
            steam_settings = json.load(f)
            if isinstance(steam_settings, list):
                steam_settings = {
                    'base_fee': 10,
                    'discount_levels': steam_settings,
                    'individual_discounts': {}
                }
            steam_discount_levels = steam_settings.get('discount_levels', [])
            steam_base_fee = steam_settings.get('base_fee', 10)
            individual_discounts = steam_settings.get('individual_discounts', {})
    except FileNotFoundError:
        steam_settings = DEFAULT_STEAM_SETTINGS
        steam_discount_levels = steam_settings['discount_levels']
        steam_base_fee = steam_settings['base_fee']
        individual_discounts = steam_settings['individual_discounts']

    try:
        with open(STORES_FILE, 'r') as f:
            stores = json.load(f)
    except FileNotFoundError:
        stores = {}

    try:
        with open(PRODUCTS_FILE, 'r', encoding='utf-8') as f:
            products = json.load(f)
    except FileNotFoundError:
        products = {}

    try:
        with open('telegram_settings.json', 'r') as f:
            telegram_settings = json.load(f)
        TELEGRAM_BOT_TOKEN = telegram_settings.get('bot_token', '')
        TELEGRAM_CHAT_ID = telegram_settings.get('chat_id', '')
    except FileNotFoundError:
        TELEGRAM_BOT_TOKEN = '7726856877:AAFIslzTXmB5FCw2zDHuPswiybUaCGxiNSw'
        TELEGRAM_CHAT_ID = '-1003175110976'

def save_data():
    """Сохраняет все данные в файлы"""
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=4)
    with open(STEAM_DISCOUNTS_FILE, 'w') as f:
        json.dump({
            'base_fee': steam_base_fee,
            'discount_levels': steam_discount_levels,
            'individual_discounts': individual_discounts
        }, f, indent=4)
    with open(STORES_FILE, 'w') as f:
        json.dump(stores, f, indent=4)


# ====================== Telegram API
# Принудительно устанавливаем значения для группы
TELEGRAM_BOT_TOKEN = '7726856877:AAFIslzTXmB5FCw2zDHuPswiybUaCGxiNSw'
TELEGRAM_CHAT_ID = '-1003175110976'  # ID вашей группы

# Сохраняем оригинальные значения, чтобы их нельзя было переопределить
ORIGINAL_TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN
ORIGINAL_TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID

def send_telegram_notification_async(username, message_type, amount=None, payment_method=None, order_data=None):
    """Асинхронная отправка уведомления в Telegram в отдельном потоке"""
    thread = threading.Thread(
        target=send_telegram_notification,
        args=(username, message_type, amount, payment_method, order_data)
    )
    thread.daemon = True  # Поток завершится при завершении основного процесса
    thread.start()

def send_telegram_notification(username, message_type, amount=None, payment_method=None, order_data=None):
    """Синхронная функция отправки уведомления в Telegram"""
    # Используем оригинальные значения, а не глобальные переменные
    bot_token = ORIGINAL_TELEGRAM_BOT_TOKEN
    chat_id = ORIGINAL_TELEGRAM_CHAT_ID
    
    if not bot_token or not chat_id:
        print("Telegram notifications are not configured")
        return None

    messages = {
        'registration': f"🆕 Новый пользователь зарегистрирован!\nUsername: {username}",
        'payment': f"💳 Новое пополнение баланса!\n\n"
                  f"👤 Пользователь: {username}\n"
                  f"💰 Сумма: {amount} USD\n"
                  f"🔧 Метод: {payment_method}\n"
                  f"🕒 Время: {get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')}",
        'new_order': f"🛒 Новый заказ!\n\n"
                    f"👤 Пользователь: {username}\n"
                    f"📦 Заказ: {order_data.get('product', 'N/A') if order_data else 'N/A'}\n"
                    f"🔢 Количество: {order_data.get('quantity', 1) if order_data else 1}\n"
                    f"💵 Сумма: {order_data.get('amount', 0) if order_data else 0} USD\n"
                    f"📅 Дата: {order_data.get('date', get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')) if order_data else get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"🆔 ID заказа: {order_data.get('id', 'N/A') if order_data else 'N/A'}\n"
                    f"🚩 Логин: {order_data.get('steamLogin', 'N/A') if order_data else 'N/A'}"
    }
    
    message = messages.get(message_type)
    if not message:
        return None

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message
    }
    try:
        response = requests.post(url, data=payload)
        return response.json()
    except Exception as e:
        print(f"Ошибка при отправке сообщения в Telegram: {e}")
        return None




# ====================== STEAM API
# Глобальная переменная для хранения курсов и времени последнего обновления
exchange_rates_cache = {
    'last_updated': None,
    'rates': None
}

# ====================== EXCHANGE RATES API
@app.route('/api/exchange_rates')
def get_exchange_rates():
    """API endpoint для получения курсов валют с кешированием"""
    global exchange_rates_cache
    
    # Проверяем, нужно ли обновлять курсы (не чаще чем раз в 10 минут)
    current_time = get_moscow_time().timestamp()
    if (exchange_rates_cache['last_updated'] and 
        current_time - exchange_rates_cache['last_updated'] < 600):  # 10 минут
        print("Используем кешированные курсы валют")
        return jsonify(exchange_rates_cache['rates'])
    
    print("Обновляем курсы валют...")
    currencies = [
        {'code': 'rub', 'id': 5, 'symbol': '₽'},
        {'code': 'uah', 'id': 18, 'symbol': '₴'},
        {'code': 'kzt', 'id': 37, 'symbol': '₸'}
    ]
    
    api_key = '62e5589d9e984151936b3625afa32774'
    rates = {}
    
    for currency in currencies:
        try:
            url = f"https://desslyhub.com/api/v1/exchange_rate/steam/{currency['id']}"
            response = requests.get(url, headers={'apikey': api_key})
            
            if response.status_code == 200:
                data = response.json()
                # Пробуем разные форматы ответа
                rate = None
                if data and 'rate' in data:
                    rate = data['rate']
                elif data and 'data' in data and 'rate' in data['data']:
                    rate = data['data']['rate']
                elif data and 'exchange_rate' in data:
                    rate = data['exchange_rate']
                elif isinstance(data, (int, float)):
                    rate = data
                
                if rate is not None:
                    rates[currency['code']] = {
                        'rate': float(rate),
                        'symbol': currency['symbol'],
                        'timestamp': current_time,
                        'fake': False
                    }
                    print(f"Курс {currency['code']}: {rate}")
                else:
                    # Используем фиктивные данные если не удалось получить реальные
                    fake_rates = {'rub': 90.5, 'uah': 38.2, 'kzt': 450.3}
                    rates[currency['code']] = {
                        'rate': fake_rates[currency['code']],
                        'symbol': currency['symbol'],
                        'timestamp': current_time,
                        'fake': True
                    }
                    print(f"Используем фиктивный курс {currency['code']}: {fake_rates[currency['code']]}")
            else:
                # Используем фиктивные данные при ошибке
                fake_rates = {'rub': 90.5, 'uah': 38.2, 'kzt': 450.3}
                rates[currency['code']] = {
                    'rate': fake_rates[currency['code']],
                    'symbol': currency['symbol'],
                    'timestamp': current_time,
                    'fake': True
                }
                print(f"Ошибка HTTP {response.status_code}, используем фиктивный курс {currency['code']}")
                
        except Exception as e:
            print(f"Ошибка при получении курса {currency['code']}: {e}")
            # Используем фиктивные данные при исключении
            fake_rates = {'rub': 90.5, 'uah': 38.2, 'kzt': 450.3}
            rates[currency['code']] = {
                'rate': fake_rates[currency['code']],
                'symbol': currency['symbol'],
                'timestamp': current_time,
                'fake': True
            }
            print(f"Исключение, используем фиктивный курс {currency['code']}")
    
    # Обновляем кеш
    exchange_rates_cache = {
        'last_updated': current_time,
        'rates': rates
    }
    
    return jsonify(rates)


# ====================== STEAM TOPUP API
@app.route('/api/steam_topup', methods=['POST'])
def steam_topup():
    """API endpoint для пополнения Steam кошелька"""
    
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    
    # Получаем режим работы заказов пользователя
    order_mode = user_info.get('order_mode', 'api')
    
    # Получаем данные из запроса
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    steam_login = data.get('steamLogin')
    amount = data.get('amount')
    
    if not steam_login or not amount:
        return jsonify({'error': 'Missing steamLogin or amount'}), 400
    
    try:
        requested_amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount format'}), 400
    
    # Проверяем максимальную сумму
    max_amount = 500
    if requested_amount > max_amount:
        return jsonify({'error': f'Maximum allowed amount is ${max_amount}'}), 400
    
    # РАСЧЕТ СКИДКИ И КОМИССИИ
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Определяем базовую скидку (по умолчанию 2%)
    base_discount = 2
    
    # ПРОВЕРЯЕМ ТАРИФ РЕСЕЛЛЕРА И ПРИМЕНЯЕМ СКИДКУ
    reseller_plan = user_info.get('reseller_plan', 'none')
    
    # Скидки по тарифам реселлера - ИЗМЕНЕНО на 4%/6%/8%
    reseller_discounts = {
        'lite': 4,      # Lite тариф = 4% скидка
        'reseller': 6,  # Reseller тариф = 6% скидка
        'pro': 8        # Pro+ тариф = 8% скидка
    }
    
    # Если у пользователя есть тариф реселлера, применяем соответствующую скидку
    if reseller_plan in reseller_discounts:
        reseller_discount = reseller_discounts[reseller_plan]
        # Используем максимальную скидку между программой лояльности и тарифом реселлера
        current_discount = max(base_discount, reseller_discount)
        discount_source = 'reseller_plan'
    else:
        # Используем только программу лояльности (старую логику)
        current_discount = base_discount
        discount_source = 'balance'
    
    # Проверяем индивидуальную скидку для пользователя
    individual_discount = individual_discounts.get(username)
    if individual_discount is not None:
        current_discount = individual_discount
        discount_source = 'individual'
    
    # РАССЧИТЫВАЕМ ФИНАЛЬНУЮ СУММУ ДЛЯ СПИСАНИЯ
    # Сначала применяем скидку (уменьшаем сумму)
    amount_after_discount = requested_amount * (1 - current_discount / 100)
    
    # Затем применяем комиссию (увеличиваем сумму)
    amount_to_pay = amount_after_discount * (1 + steam_base_fee / 100)
    
    # Проверяем баланс пользователя
    if total_balance < amount_to_pay:
        return jsonify({'error': 'Insufficient funds'}), 400
    
    # Генерируем ID транзакции (одинаковый формат для обоих режимов)
    transaction_id = str(uuid.uuid4())
    
    # ОБРАБОТКА В ЗАВИСИМОСТИ ОТ РЕЖИМА
    if order_mode == 'demo':
        # ДЕМО-РЕЖИМ: создаем заказ локально без отправки API
        try:
            # Создаем заказ
            formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
            timestamp = get_moscow_time().timestamp()
            
            new_order = {
                'id': str(uuid.uuid4()),
                'category': 'Steam',
                'product': 'Steam TopUp',
                'price': amount_to_pay,
                'amount': requested_amount,
                'requested_amount': requested_amount,
                'paid_amount': amount_to_pay,
                'base_fee_applied': True,
                'base_fee_percent': steam_base_fee,
                'discount': current_discount,
                'discount_source': discount_source,
                'date': formatted_date,
                'timestamp': timestamp,
                'steamLogin': steam_login,
                'individual_discount_applied': individual_discount is not None,
                'order_mode': order_mode,
                'demo_mode': True,
                'transaction_id': transaction_id,  # Используем нормальный UUID
                'transaction_status': 'demo_success',
                'status': 'completed',
                'demo_message': 'Заказ выполнен в демо-режиме (без реального пополнения)',
                'completed_date': formatted_date
            }
            
            # Списываем средства с баланса пользователя
            remaining = amount_to_pay
            
            # Сначала списываем с card баланса
            if balances.get('card', 0) >= remaining:
                users[username]['balance']['card'] -= remaining
                remaining = 0
            else:
                card_balance = balances.get('card', 0)
                if card_balance > 0:
                    users[username]['balance']['card'] = 0
                    remaining -= card_balance
            
            # Затем списываем с bep20 баланса
            if remaining > 0 and balances.get('bep20', 0) >= remaining:
                users[username]['balance']['bep20'] -= remaining
                remaining = 0
            elif remaining > 0:
                bep20_balance = balances.get('bep20', 0)
                if bep20_balance > 0:
                    users[username]['balance']['bep20'] = 0
                    remaining -= bep20_balance
            
            # Затем списываем с ton баланса
            if remaining > 0 and balances.get('ton', 0) >= remaining:
                users[username]['balance']['ton'] -= remaining
                remaining = 0
            
            # ОБНОВЛЕНИЕ РАСХОДОВ И ЗАКАЗОВ С УЧЕТОМ РУЧНЫХ ЗНАЧЕНИЙ
            if username in users:
                # Обновляем счетчик заказов (учитываем ручную установку)
                if 'manual_orders_count' in users[username]:
                    # Если есть ручная установка, увеличиваем её
                    users[username]['manual_orders_count'] += 1
                    users[username]['orders'] = users[username]['manual_orders_count']
                else:
                    # Иначе увеличиваем обычный счетчик
                    if 'orders' not in users[username]:
                        users[username]['orders'] = 0
                    users[username]['orders'] += 1
                
                # Обновляем расходы (учитываем ручную установку)
                if 'manual_expenses' in users[username]:
                    # Если есть ручная установка, добавляем к ней
                    users[username]['manual_expenses'] += amount_to_pay
                    users[username]['expenses'] = users[username]['manual_expenses']
                else:
                    # Иначе увеличиваем обычные расходы
                    if 'expenses' not in users[username]:
                        users[username]['expenses'] = 0
                    users[username]['expenses'] += amount_to_pay
            
            # Добавляем заказ в историю
            users[username].setdefault('userorders', []).append(new_order)
            
            # СИНХРОНИЗИРУЕМ БАЛАНС ПОСЛЕ СОЗДАНИЯ ЗАКАЗА
            sync_user_balance(username)
            save_data()
            
            # АСИНХРОННАЯ отправка уведомления в Telegram
            send_telegram_notification_async(
                username=username,
                message_type='new_order',
                order_data=new_order
            )
            
            return jsonify({
                'success': True,
                'demo_mode': True,
                'transaction_id': transaction_id,  # Возвращаем нормальный UUID
                'status': 'demo_success',
                'amount_paid': amount_to_pay,
                'amount_received': requested_amount,
                'discount_applied': current_discount,
                'base_fee_applied': steam_base_fee,
                'message': 'Заказ создан в демо-режиме'
            })
            
        except Exception as e:
            return jsonify({'error': f'Demo mode error: {str(e)}'}), 500
            
    else:
        # API-РЕЖИМ: стандартная логика с отправкой API запроса
        try:
            # Отправляем запрос к внешнему API
            api_key = '62e5589d9e984151936b3625afa32774'
            payload = {
                "amount": requested_amount,
                "username": steam_login
            }
            headers = {
                "apikey": api_key,
                "content-type": "application/json"
            }
            
            print(f"Отправляем запрос к внешнему API: {payload}")
            response = requests.post(
                'https://desslyhub.com/api/v1/service/steamtopup/topup',
                json=payload,
                headers=headers,
                timeout=30
            )
            
            print(f"Статус ответа от API: {response.status_code}")
            print(f"Ответ от API: {response.text}")
            
            if response.status_code == 200:
                api_data = response.json()
                
                if 'error_code' in api_data:
                    # Обработка ошибки от API
                    error_message = f"API error: {api_data.get('error_code')}"
                    return jsonify({'error': error_message}), 400
                
                # Успешный запрос - списываем средства и создаем заказ
                # Используем transaction_id из API или генерируем новый
                transaction_id = api_data.get('transaction_id', str(uuid.uuid4()))
                transaction_status = api_data.get('status', 'pending')
                
                # Списываем средства с баланса пользователя
                remaining = amount_to_pay
                
                # Сначала списываем с card баланса
                if balances.get('card', 0) >= remaining:
                    users[username]['balance']['card'] -= remaining
                    remaining = 0
                else:
                    card_balance = balances.get('card', 0)
                    if card_balance > 0:
                        users[username]['balance']['card'] = 0
                        remaining -= card_balance
                
                # Затем списываем с bep20 баланса
                if remaining > 0 and balances.get('bep20', 0) >= remaining:
                    users[username]['balance']['bep20'] -= remaining
                    remaining = 0
                elif remaining > 0:
                    bep20_balance = balances.get('bep20', 0)
                    if bep20_balance > 0:
                        users[username]['balance']['bep20'] = 0
                        remaining -= bep20_balance
                
                # Затем списываем с ton баланса
                if remaining > 0 and balances.get('ton', 0) >= remaining:
                    users[username]['balance']['ton'] -= remaining
                    remaining = 0
                
                # ОБНОВЛЕНИЕ РАСХОДОВ И ЗАКАЗОВ С УЧЕТОМ РУЧНЫХ ЗНАЧЕНИЙ
                if username in users:
                    # Обновляем счетчик заказов (учитываем ручную установку)
                    if 'manual_orders_count' in users[username]:
                        # Если есть ручная установка, увеличиваем её
                        users[username]['manual_orders_count'] += 1
                        users[username]['orders'] = users[username]['manual_orders_count']
                    else:
                        # Иначе увеличиваем обычный счетчик
                        if 'orders' not in users[username]:
                            users[username]['orders'] = 0
                        users[username]['orders'] += 1
                    
                    # Обновляем расходы (учитываем ручную установку)
                    if 'manual_expenses' in users[username]:
                        # Если есть ручная установка, добавляем к ней
                        users[username]['manual_expenses'] += amount_to_pay
                        users[username]['expenses'] = users[username]['manual_expenses']
                    else:
                        # Иначе увеличиваем обычные расходы
                        if 'expenses' not in users[username]:
                            users[username]['expenses'] = 0
                        users[username]['expenses'] += amount_to_pay
                
                # Создаем заказ
                formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
                timestamp = get_moscow_time().timestamp()
                
                new_order = {
                    'id': str(uuid.uuid4()),
                    'category': 'Steam',
                    'product': 'Steam TopUp',
                    'price': amount_to_pay,
                    'amount': requested_amount,
                    'requested_amount': requested_amount,
                    'paid_amount': amount_to_pay,
                    'base_fee_applied': True,
                    'base_fee_percent': steam_base_fee,
                    'discount': current_discount,
                    'discount_source': discount_source,
                    'date': formatted_date,
                    'timestamp': timestamp,
                    'steamLogin': steam_login,
                    'individual_discount_applied': individual_discount is not None,
                    'order_mode': order_mode,
                    'transaction_id': transaction_id,
                    'transaction_status': transaction_status,
                    'external_service_used': True,
                    'status': 'completed' if transaction_status == 'success' else 'pending'
                }
                
                users[username].setdefault('userorders', []).append(new_order)
                # СИНХРОНИЗИРУЕМ БАЛАНС ПОСЛЕ СОЗДАНИЯ ЗАКАЗА
                sync_user_balance(username)
                save_data()
                
                # АСИНХРОННАЯ отправка уведомления в Telegram
                send_telegram_notification_async(
                    username=username,
                    message_type='new_order',
                    order_data=new_order
                )
                
                return jsonify({
                    'success': True,
                    'transaction_id': transaction_id,
                    'status': transaction_status,
                    'amount_paid': amount_to_pay,
                    'amount_received': requested_amount,
                    'discount_applied': current_discount,
                    'base_fee_applied': steam_base_fee
                })
            else:
                return jsonify({'error': f'API returned status {response.status_code}'}), 400
                
        except requests.exceptions.Timeout:
            return jsonify({'error': 'API request timeout'}), 408
        except requests.exceptions.ConnectionError:
            return jsonify({'error': 'Cannot connect to API'}), 503
        except requests.exceptions.RequestException as e:
            return jsonify({'error': f'API request failed: {str(e)}'}), 500
        except Exception as e:
            print(f"Unexpected error: {e}")
            return jsonify({'error': 'Internal server error'}), 500






# ====================== RESELLER.HTML
@app.route('/reseller', methods=['GET', 'POST'])
def reseller():
    if 'username' not in session:
        flash('Пожалуйста, войдите в систему для доступа к разделу реселлера', 'error')
        return redirect(url_for('login'))
    
    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    
    # Рассчитываем общий баланс
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Получаем статус пользователя (идентично dashboard)
    user_status = user_info.get('status', 'active')
    freeze_reason = user_info.get('freeze_reason', '')
    
    # Рассчитываем сумму к возврату если статус "frozen" с возвратом средств
    refund_amount = 0
    tariff_cost = 0
    
    if user_status == 'frozen' and freeze_reason == 'Прекращение обслуживания - возврат средств':
        # Получаем стоимость тарифа и общий баланс для расчета возврата
        tariff_cost = user_info.get('tariff_cost', 0)
        total_user_balance = total_balance
        refund_amount = tariff_cost + total_user_balance
    
    # Получаем дату заморозки
    frozen_date = user_info.get('frozen_date', '')
    
    # Проверяем текущий тариф пользователя
    current_plan = user_info.get('reseller_plan', 'none')
    plan_since = user_info.get('reseller_since', '')
    plan_expires = user_info.get('reseller_expires', '')
    
    # Цены тарифов
    plan_prices = {
        'lite': 50,
        'reseller': 100,
        'pro': 200
    }
    
    # Названия тарифов
    plan_names = {
        'lite': 'Lite',
        'reseller': 'Reseller',
        'pro': 'Pro+'
    }
    
    # Описания преимуществ для каждого тарифа (ОБНОВЛЕНЫ проценты скидок)
    plan_benefits = {
        'lite': [
            'Скидка 4% на пополнение Steam',
            'Доступ к базовым товарам',
            'Базовая статистика',
            'Поддержка 24/7'
        ],
        'reseller': [
            'Скидка 6% на пополнение Steam',
            'Доступ к эксклюзивным товарам',
            'Расширенная аналитика',
            'Приоритетная поддержка',
            'Специальные промо-акции'
        ],
        'pro': [
            'Скидка 8% на пополнение Steam',
            'Доступ к эксклюзивным товарам',
            'Полная детализация аналитики',
            'Приоритетная поддержка',
            'Специальные промо-акции',
            'Самые высокие скидки'
        ]
    }
    
    # Следующие доступные тарифы для апгрейда
    next_plans = {
        'none': ['lite', 'reseller', 'pro'],
        'lite': ['reseller', 'pro'],
        'reseller': ['pro'],
        'pro': []  # Pro - максимальный тариф
    }
    
    # Рассчитываем оставшееся время для действующих тарифов
    # Устанавливаем days_remaining = 0 по умолчанию, чтобы избежать None в шаблоне
    days_remaining = 0
    if current_plan != 'none' and current_plan != 'pro' and plan_expires:
        try:
            expire_date = datetime.strptime(plan_expires, "%d.%m.%Y")
            current_date = datetime.now()
            delta = expire_date - current_date
            days_remaining = delta.days
        except Exception as e:
            print(f"Error calculating days remaining: {e}")
            days_remaining = 0
    
    # Флаг для показа блока с тарифами (для апгрейда)
    show_upgrade_section = False
    
    # Обработка покупки/продления/апгрейда тарифа
    if request.method == 'POST':
        action = request.form.get('action')
        
        # Проверяем, не заморожен ли аккаунт
        if user_status == 'frozen':
            flash('Ваш аккаунт заморожен. Операции с тарифами недоступны.', 'error')
            return redirect(url_for('reseller'))
        
        if action in ['buy_plan', 'renew_plan', 'upgrade_plan']:
            plan = request.form.get('plan')
            
            if plan not in plan_prices:
                flash('Неверный тариф', 'error')
                return redirect(url_for('reseller'))
            
            plan_price = plan_prices[plan]
            
            # Для апгрейда проверяем, что это более дорогой тариф
            if action == 'upgrade_plan':
                current_plan_level = {'none': 0, 'lite': 1, 'reseller': 2, 'pro': 3}
                current_level = current_plan_level.get(current_plan, 0)
                new_level = current_plan_level.get(plan, 0)
                
                if new_level <= current_level:
                    flash('Вы можете перейти только на более высокий тариф', 'error')
                    return redirect(url_for('reseller'))
                
                # Рассчитываем цену апгрейда (разница в стоимости)
                upgrade_price = plan_price - plan_prices.get(current_plan, 0)
                plan_price = max(0, upgrade_price)  # Не может быть отрицательной
            
            # Проверяем, достаточно ли средств
            if total_balance < plan_price:
                flash(f'Недостаточно средств на балансе. Требуется: {plan_price} USD', 'error')
                return redirect(url_for('reseller'))
            
            try:
                # Вычитаем стоимость тарифа с баланса
                remaining = plan_price
                
                # Сначала списываем с карточного баланса
                card_balance = balances.get('card', 0)
                if card_balance >= remaining:
                    users[username]['balance']['card'] = card_balance - remaining
                    remaining = 0
                elif card_balance > 0:
                    remaining -= card_balance
                    users[username]['balance']['card'] = 0
                
                # Затем с BEP20 баланса
                bep20_balance = balances.get('bep20', 0)
                if remaining > 0 and bep20_balance > 0:
                    if bep20_balance >= remaining:
                        users[username]['balance']['bep20'] = bep20_balance - remaining
                        remaining = 0
                    else:
                        remaining -= bep20_balance
                        users[username]['balance']['bep20'] = 0
                
                # Затем с TON баланса
                ton_balance = balances.get('ton', 0)
                if remaining > 0 and ton_balance > 0:
                    if ton_balance >= remaining:
                        users[username]['balance']['ton'] = ton_balance - remaining
                        remaining = 0
                    else:
                        remaining -= ton_balance
                        users[username]['balance']['ton'] = 0
                
                if remaining > 0:
                    flash('Недостаточно средств на всех балансах', 'error')
                    return redirect(url_for('reseller'))
                
                # Обработка разных типов действий
                if action == 'buy_plan':
                    # Покупка нового тарифа
                    users[username]['reseller_plan'] = plan
                    users[username]['reseller_status'] = True
                    users[username]['reseller_since'] = datetime.now().strftime("%d.%m.%Y")
                    
                    # Устанавливаем дату окончания для месячных тарифов
                    if plan in ['lite', 'reseller']:
                        expire_date = datetime.now() + timedelta(days=30)
                        users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
                    else:
                        # Pro тариф - навсегда
                        users[username]['reseller_expires'] = None
                    
                    action_text = f'Тариф {plan_names[plan]} активирован'
                    
                elif action == 'renew_plan':
                    # Продление текущего тарифа
                    if current_plan != plan:
                        flash('Нельзя продлить другой тариф', 'error')
                        return redirect(url_for('reseller'))
                    
                    # Продлеваем на 30 дней от текущей даты окончания или от сегодня
                    if plan_expires:
                        try:
                            expire_date = datetime.strptime(plan_expires, "%d.%m.%Y")
                            # Если тариф уже истек, продлеваем от сегодняшней даты
                            if expire_date < datetime.now():
                                expire_date = datetime.now()
                            expire_date += timedelta(days=30)
                            users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
                        except Exception as e:
                            print(f"Error parsing expire date: {e}")
                            # Если ошибка парсинга, устанавливаем от сегодня
                            expire_date = datetime.now() + timedelta(days=30)
                            users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
                    else:
                        # Если даты окончания нет, устанавливаем от сегодня
                        expire_date = datetime.now() + timedelta(days=30)
                        users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
                    
                    action_text = f'Тариф {plan_names[plan]} продлен на 30 дней'
                
                elif action == 'upgrade_plan':
                    # Апгрейд на более высокий тариф
                    users[username]['reseller_plan'] = plan
                    users[username]['reseller_status'] = True
                    
                    # Для Pro тарифа - навсегда, для других - сохраняем оставшееся время или устанавливаем 30 дней
                    if plan == 'pro':
                        users[username]['reseller_expires'] = None
                    elif plan_expires:
                        # Сохраняем текущую дату окончания
                        users[username]['reseller_expires'] = plan_expires
                    else:
                        # Если даты окончания нет, устанавливаем 30 дней
                        expire_date = datetime.now() + timedelta(days=30)
                        users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
                    
                    action_text = f'Тариф улучшен до {plan_names[plan]}'
                
                # Добавляем информацию о покупке/продлении/апгрейде в историю
                purchase_history = users[username].get('purchase_history', [])
                purchase_history.append({
                    'type': 'reseller_plan',
                    'action': action,
                    'plan': plan,
                    'amount': plan_price,
                    'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'description': f'{action_text}'
                })
                users[username]['purchase_history'] = purchase_history
                
                # Помечаем, что баланс был изменен вручную
                users[username]['balance_manually_modified'] = True
                users[username]['balance_last_manual_update'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Синхронизируем баланс после списания
                sync_user_balance(username)
                save_data()
                
                success_msg = f'{action_text}. С баланса списано {plan_price} USD'
                flash(success_msg, 'success')
                return redirect(url_for('reseller'))
                
            except Exception as e:
                # Детально логируем ошибку
                import traceback
                error_details = f"Error processing plan {plan} for user {username}: {str(e)}"
                print(f"❌ Ошибка при обработке тарифа:")
                print(f"   Пользователь: {username}")
                print(f"   Тариф: {plan}")
                print(f"   Действие: {action}")
                print(f"   Ошибка: {str(e)}")
                print(f"   Полная трассировка:")
                traceback.print_exc()
                
                flash(f'Ошибка при обработке операции с тарифом: {str(e)[:100]}', 'error')
                return redirect(url_for('reseller'))
        
        elif action == 'show_upgrade':
            # Показать секцию с тарифами для апгрейда
            show_upgrade_section = True
    
    return render_template('reseller.html', 
                         username=username,
                         total_balance=total_balance,
                         current_plan=current_plan,
                         plan_since=plan_since,
                         plan_expires=plan_expires,
                         days_remaining=days_remaining,
                         plan_prices=plan_prices,
                         plan_names=plan_names,
                         plan_benefits=plan_benefits,
                         next_plans=next_plans.get(current_plan, []),
                         show_upgrade_section=show_upgrade_section,
                         user_status=user_status,
                         freeze_reason=freeze_reason,
                         refund_amount=refund_amount,
                         tariff_cost=tariff_cost,
                         frozen_date=frozen_date)




# ====================== 1. INDEX.HTML
@app.route('/')
def main():
    
    
    # Получаем уровни скидок для Steam
    sorted_levels = sorted(steam_discount_levels, key=lambda x: x[0])
    
    return render_template('1.index.html', 
                         discount_levels=sorted_levels,
                         steam_base_fee=steam_base_fee)



# ====================== 2. USER_AGREEMENT.HTML
@app.route('/user_agreement')
def user_agreement():
    
    return render_template('2.user_agreement.html')



# ====================== 3. PRIVACY_POLICY.HTML
@app.route('/privacy_policy')
def privacy_policy():
    
    return render_template('3.privacy_policy.html')



# ====================== 4. SUPPORT.HTML
@app.route('/support', methods=['GET', 'POST'])
def support():
    if request.method == 'POST':
        # Обработка данных формы обратной связи
        name = request.form.get('name')
        email = request.form.get('email')
        message = request.form.get('message')
        
        # Здесь можно добавить логику обработки формы
        # Например, отправка email или сохранение в базу данных
        
        flash('Ваше сообщение отправлено! Мы ответим в ближайшее время.', 'success')
        return redirect(url_for('support'))
    
    return render_template('4.support.html')



# ====================== 5. LOGIN.HTML
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        if username in users and users[username]['password'] == password:
            session['username'] = username
            users[username]['last_login'] = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
            save_data()

            # ⛔ Заблокирован
            if users[username].get('status') == 'banned':
                return redirect(url_for('banned'))

            return redirect(url_for('dashboard'))

        flash("Неверный логин или пароль!", 'error')
        return redirect(url_for('login'))

    return render_template('5.login.html')

@app.route('/banned')
def banned():
    if 'username' not in session:
        return redirect(url_for('login'))

    user = users.get(session['username'])

    if not user or user.get('status') != 'banned':
        return redirect(url_for('dashboard'))

    return render_template(
        'banned.html',
        reason=user.get('ban_reason', 'Причина не указана'),
        contact_type=user.get('contact_type'),
        contact_info=user.get('contact_info')
    )





# ====================== 6. REGISTER.HTML
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password1']
        password_confirm = request.form['password2']
        contact = request.form.get('contact', '').strip()
        
        # ========== ЖЕСТКАЯ ВАЛИДАЦИЯ USERNAME ==========
        
        #
        if len(username) > 15:
            flash('Имя пользователя не может быть длиннее 15 символов', 'error')
            return render_template('6.register.html')
        
        if len(username) < 3:
            flash('Имя пользователя должно содержать минимум 3 символа', 'error')
            return render_template('6.register.html')
        
        # 2. Запрещенные символы в начале и конце
        forbidden_edge_chars = ['*', '.', ',', '!', '?', '@', '#', '$', '%', '^', '&', '(', ')', '-', '+', '=', '[', ']', '{', '}', '|', '\\', '/', '<', '>', '`', '~', ':', ';', '"', "'"]
        
        if username[0] in forbidden_edge_chars:
            flash('Имя пользователя не может начинаться со специальных символов', 'error')
            return render_template('6.register.html')
        
        if username[-1] in forbidden_edge_chars:
            flash('Имя пользователя не может заканчиваться специальными символами', 'error')
            return render_template('6.register.html')
        
        # 3. Запрет на URL и ссылки
        url_patterns = [
            'http://', 'https://', 'www.', '.com', '.ru', '.org', '.net', '.io',
            '.ua', '.by', '.kz', '.su', '.рф', '.онлайн', '.сайт',
            '://', 'href=', 'url=', 'link=', 'click', 'redirect',
            'invencio', 'deposit', 'credit', 'cash', 'money', 'bonus',
            'free', 'win', 'prize', 'visit', 'earn', 'profit', 'income',
            'dollar', 'euro', 'usd', 'btc', 'bitcoin', 'eth', 'crypto',
            'wallet', 'payment', 'transfer', 'promo', 'gift', 'reward',
            'claim', 'get', 'now', 'today', 'urgent', 'important',
            'action', 'required', 'verify', 'confirm'
        ]
        
        username_lower = username.lower()
        for pattern in url_patterns:
            if pattern in username_lower:
                flash('Имя пользователя не может содержать ссылки или рекламные слова', 'error')
                return render_template('6.register.html')
        
        # 4. Запрет на HTML теги
        html_patterns = ['<', '>', '&lt;', '&gt;', '<a', '</a>', '<br', '<div', '<span', 'href']
        for pattern in html_patterns:
            if pattern in username_lower:
                flash('Имя пользователя не может содержать HTML теги', 'error')
                return render_template('6.register.html')
        
        # 5. Проверка на повторяющиеся специальные символы
        special_chars_count = 0
        for i, char in enumerate(username):
            if char in forbidden_edge_chars:
                special_chars_count += 1
                # Проверка на 3 и более спецсимволов подряд
                if i > 0 and username[i-1] in forbidden_edge_chars and i < len(username)-1 and username[i+1] in forbidden_edge_chars:
                    flash('Имя пользователя не может содержать более 2 специальных символов подряд', 'error')
                    return render_template('6.register.html')
        
        # Общее ограничение на количество спецсимволов (не более 30% от длины)
        if special_chars_count > len(username) * 0.3:
            flash('Слишком много специальных символов в имени пользователя', 'error')
            return render_template('6.register.html')
        
        # 6. Разрешенные символы (буквы, цифры, точка, подчеркивание, дефис)
        allowed_chars = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')
        for char in username:
            if char not in allowed_chars:
                flash('Имя пользователя может содержать только буквы, цифры, точки, подчеркивания и дефисы', 'error')
                return render_template('6.register.html')
        
        # 7. Проверка на цифры в начале (не более 3 цифр подряд в начале)
        digit_count_at_start = 0
        for char in username:
            if char.isdigit():
                digit_count_at_start += 1
            else:
                break
        if digit_count_at_start > 3:
            flash('Имя пользователя не может начинаться с более чем 3 цифр подряд', 'error')
            return render_template('6.register.html')
        
        # 8. Проверка на то, что имя не состоит только из цифр
        if username.replace('.', '').replace('_', '').replace('-', '').isdigit():
            flash('Имя пользователя не может состоять только из цифр', 'error')
            return render_template('6.register.html')
        
        # 9. Проверка на наличие как минимум одной буквы
        has_letter = False
        for char in username:
            if char.isalpha():
                has_letter = True
                break
        if not has_letter:
            flash('Имя пользователя должно содержать хотя бы одну букву', 'error')
            return render_template('6.register.html')
        
        # 10. Дополнительная проверка на спам-паттерны через регулярные выражения
        import re
        spam_regexes = [
            r'\*.*\*.*\*',  # Звездочки с текстом между ними
            r'<.*>',         # Любые HTML теги
            r'https?://',    # HTTP ссылки
            r'www\.',        # www ссылки
            r'\[.*\]\(.*\)', # Markdown ссылки
            r'bit\.ly',      # Сокращатели ссылок
            r'tinyurl',      # Сокращатели ссылок
            r'goo\.gl'       # Сокращатели ссылок
        ]
        
        for regex in spam_regexes:
            if re.search(regex, username):
                flash('Обнаружен недопустимый формат имени пользователя', 'error')
                return render_template('6.register.html')
        
        # 11. Проверка на системные имена
        system_names = ['admin', 'root', 'administrator', 'moderator', 'support', 'help', 'info', 'contact']
        if username.lower() in system_names:
            flash('Данное имя пользователя зарезервировано системой', 'error')
            return render_template('6.register.html')
        
        # 12. Проверка на частые мошеннические паттерны
        fraud_patterns = ['xn--', 'porn', 'sex', 'xxx', 'cvv', 'ccv', 'dumps', 'hack', 'exploit']
        for pattern in fraud_patterns:
            if pattern in username_lower:
                flash('Обнаружены недопустимые слова в имени пользователя', 'error')
                return render_template('6.register.html')
        
        # ========== КОНЕЦ ВАЛИДАЦИИ USERNAME ==========
        
        # Проверка паролей
        if password != password_confirm:
            flash('Пароли не совпадают', 'error')
            return render_template('6.register.html')
        
        # Проверка пароля на сложность
        if len(password) < 8:
            flash('Пароль должен содержать минимум 8 символов', 'error')
            return render_template('6.register.html')
        
        if password.isdigit():
            flash('Пароль не может состоять только из цифр', 'error')
            return render_template('6.register.html')
        
        # Проверка существования пользователя
        if username in users:
            flash('Пользователь с таким именем уже существует', 'error')
            return render_template('6.register.html')
        
        # Проверка валидности контактных данных
        contact_type = None
        if contact:
            contact = contact.replace(' ', '')
            
            if contact.startswith('@'):
                # Телеграм
                if len(contact) >= 2 and contact[1:].replace('_', '').isalnum():
                    contact_type = 'telegram'
                else:
                    flash('Некорректный формат Telegram username. Используйте @username (только буквы, цифры и _)', 'error')
                    return render_template('6.register.html')
            elif '@' in contact:
                # Email
                email_parts = contact.split('@')
                if len(email_parts) == 2 and '.' in email_parts[1] and len(email_parts[1].split('.')[-1]) >= 2:
                    # Дополнительная проверка email на спам
                    if any(pattern in contact.lower() for pattern in ['mailinator', 'tempmail', '10minute', 'guerrilla']):
                        flash('Использование временных email адресов запрещено', 'error')
                        return render_template('6.register.html')
                    contact_type = 'email'
                else:
                    flash('Некорректный формат email адреса', 'error')
                    return render_template('6.register.html')
            else:
                flash('Пожалуйста, укажите Telegram (@username) или Email адрес', 'error')
                return render_template('6.register.html')
        
        # Проверка на существование похожего username (для защиты от спама)
        similar_usernames = [u for u in users.keys() if username.lower() in u.lower() or u.lower() in username.lower()]
        if similar_usernames and len(similar_usernames) > 5:
            flash('Слишком много похожих имен пользователей. Попробуйте другое имя.', 'error')
            return render_template('6.register.html')
        
        # Создание пользователя
        users[username] = {
            'password': password,
            'balance': {'card': 0, 'ton': 0, 'bep20': 0},
            'orders': 0,
            'expenses': 0,
            'userorders': [],
            'topups': [],
            'status': 'active',
            'registration_date': get_moscow_time().strftime('%Y-%m-%d %H:%M:%S'),
            'contact_info': contact if contact else None,
            'contact_type': contact_type if contact else None,
            'registration_ip': request.remote_addr  # Сохраняем IP для отслеживания
        }
        
        # Сохраняем данные
        save_data()
        
        # АСИНХРОННАЯ отправка уведомления в Telegram
        send_telegram_notification_async(username, 'registration')
        
        flash('Регистрация успешно завершена! Теперь вы можете войти в систему.', 'success')
        return redirect(url_for('login'))
    
    return render_template('6.register.html')



# ====================== 7. DASHBOARD.HTML
@app.route('/dashboard')
def dashboard():
    if 'username' not in session:
        flash('Please login to access the dashboard', 'error')
        return redirect(url_for('login'))
    
    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    
    # Рассчитываем общий баланс (все типы балансов)
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Получаем статус пользователя
    user_status = user_info.get('status', 'active')
    freeze_reason = user_info.get('freeze_reason', '')
    
    # Рассчитываем сумму к возврату если статус "frozen" с возвратом средств
    refund_amount = 0
    tariff_cost = 0
    
    if user_status == 'frozen' and freeze_reason == 'Прекращение обслуживания - возврат средств':
        # Получаем стоимость тарифа и общий баланс для расчета возврата
        tariff_cost = user_info.get('tariff_cost', 0)
        total_user_balance = total_balance
        refund_amount = tariff_cost + total_user_balance
    
    # Получаем дату заморозки
    frozen_date = user_info.get('frozen_date', '')
    
    return render_template('7.dashboard.html', 
                         username=username, 
                         balances=balances,
                         total_balance=total_balance,
                         user_status=user_status,
                         freeze_reason=freeze_reason,
                         refund_amount=refund_amount,
                         tariff_cost=tariff_cost,
                         frozen_date=frozen_date)


@app.route('/refund-info')
def refund_info():
    """Страница с информацией о возврате средств"""
    
    if 'username' not in session:
        flash('Please login to access this page', 'error')
        return redirect(url_for('login'))
    
    username = session['username']
    user_info = users.get(username, {})
    
    # Проверяем, что пользователь действительно заморожен с возвратом средств
    if user_info.get('status') != 'frozen':
        flash('Эта страница доступна только пользователям с активным возвратом средств', 'error')
        return redirect(url_for('dashboard'))
    
    if user_info.get('freeze_reason') != 'Прекращение обслуживания - возврат средств':
        flash('Возврат средств не активирован для вашего аккаунта', 'error')
        return redirect(url_for('dashboard'))
    
    # Получаем данные для отображения
    balances = user_info.get('balance', {})
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    tariff_cost = user_info.get('tariff_cost', 0)
    refund_amount = tariff_cost + total_balance
    freeze_reason = user_info.get('freeze_reason', '')
    frozen_date = user_info.get('frozen_date', '')
    
    return render_template('refund_info.html',
                         username=username,
                         total_balance=total_balance,
                         tariff_cost=tariff_cost,
                         refund_amount=refund_amount,
                         freeze_reason=freeze_reason,
                         frozen_date=frozen_date)



# ====================== ДОП. ФУНКЦИЯ - МОДУЛЬ ДЛЯ ДЕМО-РЕЖИМА
def process_order_in_demo_mode(username, order_data):
    """Обработка заказа в демо-режиме"""
    try:
        # Получаем информацию о пользователе
        user_info = users.get(username, {})
        
        # Рассчитываем сумму к списанию
        final_amount = order_data.get('paid_amount', 0)
        
        # Списываем средства с баланса (например, с карточного баланса)
        if user_info['balance']['card'] >= final_amount:
            user_info['balance']['card'] -= final_amount
        else:
            # Если недостаточно средств на карточном балансе, используем другие методы
            remaining_amount = final_amount
            
            # Сначала списываем с карточного баланса
            if user_info['balance']['card'] > 0:
                card_amount = min(remaining_amount, user_info['balance']['card'])
                user_info['balance']['card'] -= card_amount
                remaining_amount -= card_amount
            
            # Затем с TON
            if remaining_amount > 0 and user_info['balance']['ton'] > 0:
                ton_amount = min(remaining_amount, user_info['balance']['ton'])
                user_info['balance']['ton'] -= ton_amount
                remaining_amount -= ton_amount
            
            # Затем с BEP20
            if remaining_amount > 0 and user_info['balance']['bep20'] > 0:
                bep20_amount = min(remaining_amount, user_info['balance']['bep20'])
                user_info['balance']['bep20'] -= bep20_amount
                remaining_amount -= bep20_amount
            
            if remaining_amount > 0:
                raise ValueError(f"Недостаточно средств. Осталось неоплаченным: ${remaining_amount:.2f}")
        
        # Обновляем расходы пользователя
        user_info['expenses'] = user_info.get('expenses', 0) + final_amount
        
        # Добавляем флаг демо-режима
        order_data['demo_mode'] = True
        order_data['demo_processed'] = True
        order_data['status'] = 'completed'
        
        # Добавляем заказ в историю
        user_info.setdefault('userorders', []).append(order_data)
        
        # Сохраняем данные
        save_data()
        
        return True, "Заказ успешно обработан в демо-режиме"
        
    except Exception as e:
        return False, str(e)


# ====================== 8. PRODUCT_1.HTML
@app.route('/product/1', methods=['GET', 'POST'])
def product1():
    if 'username' not in session:
        return redirect(url_for('login'))

    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})

    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Получаем статус пользователя для отображения badge
    user_status = user_info.get('status', 'active')
    freeze_reason = user_info.get('freeze_reason', '')
    frozen_date = user_info.get('frozen_date', '')
    
    # Получаем режим работы заказов пользователя (по умолчанию 'api')
    order_mode = user_info.get('order_mode', 'api')
    
    # Рассчитываем сумму к возврату если статус "frozen" с возвратом средств
    refund_amount = 0
    tariff_cost = 0
    
    if user_status == 'frozen' and freeze_reason == 'Прекращение обслуживания - возврат средств':
        # Получаем стоимость тарифа и общий баланс для расчета возврата
        tariff_cost = user_info.get('tariff_cost', 0)
        total_user_balance = total_balance
        refund_amount = tariff_cost + total_user_balance

    error = None
    max_amount = 500
    purchase_limit = None
    purchases_count = 0

    # Считаем количество совершенных покупок Steam
    if 'userorders' in user_info:
        steam_purchases = [order for order in user_info['userorders'] 
                          if order.get('category') == 'Steam']
        purchases_count = len(steam_purchases)

    # Базовые скидки - ОБНОВЛЕНЫ проценты
    discount_levels = [(0, 2)]
    reseller_plan = user_info.get('reseller_plan', 'none')
    if reseller_plan == 'lite':
        discount_levels = [(0, 4)]  # Изменено с 4% на 4%
    elif reseller_plan == 'reseller':
        discount_levels = [(0, 6)]  # Изменено с 7% на 6%
    elif reseller_plan == 'pro':
        discount_levels = [(0, 8)]  # Изменено с 10% на 8%

    current_discount_from_balance = discount_levels[0][1] if discount_levels else 2
    individual_discount = individual_discounts.get(username)

    if individual_discount is not None:
        current_discount = individual_discount
        discount_source = 'individual'
    else:
        current_discount = current_discount_from_balance
        discount_source = 'reseller_plan' if reseller_plan != 'none' else 'base'

    # Если POST — используем API endpoint для создания заказа
    if request.method == 'POST':
        # Проверяем, не заблокирован ли пользователь
        if user_status == 'frozen' or user_status == 'banned':
            return redirect(url_for('product1'))
        
        # Получаем данные из формы
        steam_login = request.form.get('steamLogin', '')
        amount_str = request.form.get('amount', '0')
        
        try:
            amount = float(amount_str)
            if amount <= 0:
                return redirect(url_for('product1'))
        except ValueError:
            return redirect(url_for('product1'))
        
        if not steam_login:
            return redirect(url_for('product1'))
        
        # Рассчитываем сумму к списанию
        amount_after_discount = amount * (1 - current_discount / 100)
        final_amount = amount_after_discount * (1 + steam_base_fee / 100)
        
        # Проверяем достаточно ли средств
        if total_balance < final_amount:
            return redirect(url_for('product1'))
        
        # Используем API endpoint для создания заказа
        # Создаем JSON данные для API
        api_data = {
            'steamLogin': steam_login,
            'amount': amount
        }
        
        # Используем session для аутентификации
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess['username'] = username
            
            # Отправляем запрос к нашему же API endpoint
            response = client.post('/api/steam_topup', 
                                  json=api_data,
                                  content_type='application/json')
            
            if response.status_code == 200:
                # Успешно - обновляем данные и перезагружаем страницу
                # Перезагружаем данные пользователя
                load_data()
                user_info = users.get(username, {})
                balances = user_info.get('balance', {})
                total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
                
                # Обновляем количество покупок
                if 'userorders' in user_info:
                    steam_purchases = [order for order in user_info['userorders'] 
                                      if order.get('category') == 'Steam']
                    purchases_count = len(steam_purchases)
            else:
                # Ошибка - можно добавить логирование
                pass
        
        # После POST запроса всегда делаем redirect для предотвращения повторной отправки
        return redirect(url_for('product1'))

    # Для GET запроса просто рендерим страницу
    return render_template('8.product_1.html',
                           username=username,
                           balances=balances,
                           total_balance=total_balance,
                           error=error,
                           steam_base_fee=steam_base_fee,
                           current_discount=current_discount,
                           discount_levels=discount_levels,
                           max_amount=max_amount,
                           purchases_count=purchases_count,
                           purchase_limit=purchase_limit,
                           individual_discount=individual_discount,
                           reseller_plan=reseller_plan,
                           user_status=user_status,
                           freeze_reason=freeze_reason,
                           refund_amount=refund_amount,
                           tariff_cost=tariff_cost,
                           frozen_date=frozen_date,
                           order_mode=order_mode)



# ====================== 9. PRODUCT_2.HTML
@app.route('/product/2', methods=['GET', 'POST'])
def product2():
    
    if 'username' not in session:
        return redirect(url_for('login'))
    
    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    
    # Учитываем ВСЕ типы балансов для total_balance
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Получаем статус пользователя
    user_status = user_info.get('status', 'active')
    freeze_reason = user_info.get('freeze_reason', '')
    
    # Рассчитываем сумму к возврату если статус "frozen" с возвратом средств
    refund_amount = 0
    tariff_cost = 0
    
    if user_status == 'frozen' and freeze_reason == 'Прекращение обслуживания - возврат средств':
        # Получаем стоимость тарифа и общий баланс для расчета возврата
        tariff_cost = user_info.get('tariff_cost', 0)
        total_user_balance = total_balance
        refund_amount = tariff_cost + total_user_balance
    
    # Получаем дату заморозки
    frozen_date = user_info.get('frozen_date', '')
    
    error = None
    
    # Получаем товары из products.json
    category_products = {}
    if 'categories' in products and 'steam_wallet_us' in products['categories']:
        category_products = products['categories']['steam_wallet_us']['products']
    
    if request.method == 'POST':
        # Проверяем, не заморожен ли пользователь
        if user_status == 'frozen':
            error = "Ваш аккаунт заморожен. Операции недоступны."
        else:
            product_id = request.form.get('product_id')
            amount_str = request.form.get('amount', '0')
            
            try:
                amount = int(amount_str)
            except ValueError:
                error = "Invalid amount format"
                amount = 0
            
            if not product_id:
                error = "Product ID is required"
            elif amount <= 0:
                error = "Amount must be greater than 0"
            else:
                # Получаем цену из products.json
                product_price = None
                if product_id in category_products:
                    product_price = category_products[product_id]['price']
                    product_name = category_products[product_id]['name']
                    in_stock = category_products[product_id].get('in_stock', True)
                else:
                    error = "Product not found"
                    product_price = 0

                if not in_stock:
                    error = "Product is out of stock"
                else:
                    total_price = amount * product_price
                    formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
                    timestamp = get_moscow_time().timestamp()

                    if total_price <= 0:
                        error = "Invalid total price."
                    # Проверяем ВСЕ доступные балансы
                    elif total_balance >= total_price:
                        # Сначала списываем с card баланса
                        if balances.get('card', 0) >= total_price:
                            users[username]['balance']['card'] -= total_price
                        else:
                            # Если card баланса недостаточно, используем другие балансы
                            remaining = total_price
                            
                            # Списываем с card баланса всё что есть
                            card_balance = balances.get('card', 0)
                            if card_balance > 0:
                                if card_balance >= remaining:
                                    users[username]['balance']['card'] -= remaining
                                    remaining = 0
                                else:
                                    users[username]['balance']['card'] = 0
                                    remaining -= card_balance
                            
                            # Затем списываем с bep20 баланса
                            if remaining > 0 and balances.get('bep20', 0) >= remaining:
                                users[username]['balance']['bep20'] -= remaining
                                remaining = 0
                            elif remaining > 0:
                                bep20_balance = balances.get('bep20', 0)
                                if bep20_balance > 0:
                                    users[username]['balance']['bep20'] = 0
                                    remaining -= bep20_balance
                            
                            # Затем списываем с ton баланса
                            if remaining > 0 and balances.get('ton', 0) >= remaining:
                                users[username]['balance']['ton'] -= remaining
                                remaining = 0
                            elif remaining > 0:
                                # Если всё равно недостаточно - ошибка
                                error = "Insufficient funds across all balance types"
                    else:
                        error = "Insufficient funds."

                    if not error:
                        users[username]['expenses'] += total_price
                        new_order = {
                            'id': str(uuid.uuid4()),
                            'category': 'Steam Wallet Code | USA',
                            'product': product_name,
                            'price': total_price,
                            'amount': amount,
                            'date': formatted_date,
                            'timestamp': timestamp,
                            'status': 'completed'
                        }
                        users[username].setdefault('userorders', []).append(new_order)
                        # СИНХРОНИЗИРУЕМ БАЛАНС ПОСЛЕ СОЗДАНИЯ ЗАКАЗА
                        sync_user_balance(username)
                        save_data()
                        
                        # АСИНХРОННАЯ отправка уведомления в Telegram
                        send_telegram_notification_async(
                            username=username,
                            message_type='new_order',
                            order_data=new_order
                        )
                        return redirect(url_for('product2'))

    return render_template('9.product_2.html',
                         username=username,
                         balances=balances,
                         total_balance=total_balance,
                         error=error,
                         products=category_products,
                         user_status=user_status,
                         freeze_reason=freeze_reason,
                         refund_amount=refund_amount,
                         tariff_cost=tariff_cost,
                         frozen_date=frozen_date)




# ====================== 10. PRODUCT_3.HTML
@app.route('/product/3', methods=['GET', 'POST'])
def product3():
   
    if 'username' not in session:
        return redirect(url_for('login'))
    
    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    
    # Учитываем ВСЕ типы балансов для total_balance
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Получаем статус пользователя
    user_status = user_info.get('status', 'active')
    freeze_reason = user_info.get('freeze_reason', '')
    
    # Рассчитываем сумму к возврату если статус "frozen" с возвратом средств
    refund_amount = 0
    tariff_cost = 0
    
    if user_status == 'frozen' and freeze_reason == 'Прекращение обслуживания - возврат средств':
        # Получаем стоимость тарифа и общий баланс для расчета возврата
        tariff_cost = user_info.get('tariff_cost', 0)
        total_user_balance = total_balance
        refund_amount = tariff_cost + total_user_balance
    
    # Получаем дату заморозки
    frozen_date = user_info.get('frozen_date', '')
    
    error = None
    
    # Получаем товары из products.json для EU региона
    category_products = {}
    if 'categories' in products and 'steam_wallet_eu' in products['categories']:
        category_products = products['categories']['steam_wallet_eu']['products']
    
    if request.method == 'POST':
        # Проверяем, не заморожен ли пользователь
        if user_status == 'frozen':
            error = "Ваш аккаунт заморожен. Операции недоступны."
        else:
            product_id = request.form.get('product_id')
            amount_str = request.form.get('amount', '0')
            
            try:
                amount = int(amount_str)
            except ValueError:
                error = "Invalid amount format"
                amount = 0
            
            if not product_id:
                error = "Product ID is required"
            elif amount <= 0:
                error = "Amount must be greater than 0"
            else:
                # Получаем цену из products.json
                product_price = None
                if product_id in category_products:
                    product_price = category_products[product_id]['price']
                    product_name = category_products[product_id]['name']
                    in_stock = category_products[product_id].get('in_stock', True)
                else:
                    error = "Product not found"
                    product_price = 0

                if not in_stock:
                    error = "Product is out of stock"
                else:
                    total_price = amount * product_price
                    formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
                    timestamp = get_moscow_time().timestamp()

                    if total_price <= 0:
                        error = "Invalid total price."
                    # Проверяем ВСЕ доступные балансы
                    elif total_balance >= total_price:
                        # Сначала списываем с card баланса
                        if balances.get('card', 0) >= total_price:
                            users[username]['balance']['card'] -= total_price
                        else:
                            # Если card баланса недостаточно, используем другие балансы
                            remaining = total_price
                            
                            # Списываем с card баланса всё что есть
                            card_balance = balances.get('card', 0)
                            if card_balance > 0:
                                if card_balance >= remaining:
                                    users[username]['balance']['card'] -= remaining
                                    remaining = 0
                                else:
                                    users[username]['balance']['card'] = 0
                                    remaining -= card_balance
                            
                            # Затем списываем с bep20 баланса
                            if remaining > 0 and balances.get('bep20', 0) >= remaining:
                                users[username]['balance']['bep20'] -= remaining
                                remaining = 0
                            elif remaining > 0:
                                bep20_balance = balances.get('bep20', 0)
                                if bep20_balance > 0:
                                    users[username]['balance']['bep20'] = 0
                                    remaining -= bep20_balance
                            
                            # Затем списываем с ton баланса
                            if remaining > 0 and balances.get('ton', 0) >= remaining:
                                users[username]['balance']['ton'] -= remaining
                                remaining = 0
                            elif remaining > 0:
                                # Если всё равно недостаточно - ошибка
                                error = "Insufficient funds across all balance types"
                    else:
                        error = "Insufficient funds."

                    if not error:
                        users[username]['expenses'] += total_price
                        new_order = {
                            'id': str(uuid.uuid4()),
                            'category': 'Steam Wallet Code | EU',
                            'product': product_name,
                            'price': total_price,
                            'amount': amount,
                            'date': formatted_date,
                            'timestamp': timestamp,
                            'status': 'completed'
                        }
                        users[username].setdefault('userorders', []).append(new_order)
                        # СИНХРОНИЗИРУЕМ БАЛАНС ПОСЛЕ СОЗДАНИЯ ЗАКАЗА
                        sync_user_balance(username)
                        save_data()
                        
                        # АСИНХРОННАЯ отправка уведомления в Telegram
                        send_telegram_notification_async(
                            username=username,
                            message_type='new_order',
                            order_data=new_order
                        )
                        return redirect(url_for('product3'))

    return render_template('10.product_3.html',
                         username=username,
                         balances=balances,
                         total_balance=total_balance,
                         error=error,
                         products=category_products,
                         user_status=user_status,
                         freeze_reason=freeze_reason,
                         refund_amount=refund_amount,
                         tariff_cost=tariff_cost,
                         frozen_date=frozen_date)



# ====================== 10. PRODUCT_4.HTML
@app.route('/product/4', methods=['GET', 'POST'])
def product4():
    """Страница для отправки Steam игр в подарок"""
    print(f"[DEBUG] Запрос к /product/4 метод: {request.method}")
    
    if 'username' not in session:
        print("[DEBUG] Пользователь не авторизован, перенаправление на логин")
        return redirect(url_for('login'))

    username = session['username']
    print(f"[DEBUG] Пользователь: {username}")
    
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    print(f"[DEBUG] Общий баланс пользователя: ${total_balance}")
    
    # Получаем список игр при загрузке страницы
    games_list = []
    error_message = None
    
    if request.method == 'GET':
        print("[DEBUG] Обработка GET запроса, получение списка игр...")
        try:
            # Получаем список игр из API
            api_key = '62e5589d9e984151936b3625afa32774'
            url = "https://desslyhub.com/api/v1/service/steamgift/games"
            headers = {'apikey': api_key}
            
            print(f"[DEBUG] Отправка GET запроса к API: {url}")
            print(f"[DEBUG] Заголовки запроса: {headers}")
            
            response = requests.get(url, headers=headers, timeout=15)
            
            print(f"[DEBUG] Статус ответа API: {response.status_code}")
            print(f"[DEBUG] Заголовки ответа: {dict(response.headers)}")
            
            if response.status_code == 200:
                print(f"[DEBUG] API вернул успешный ответ")
                print(f"[DEBUG] Длина ответа: {len(response.text)} символов")
                print(f"[DEBUG] Первые 500 символов ответа: {response.text[:500]}")
                
                try:
                    data = response.json()
                    print(f"[DEBUG] JSON успешно декодирован")
                    print(f"[DEBUG] Тип данных: {type(data)}")
                    
                    # Парсим ответ в зависимости от формата
                    if isinstance(data, dict):
                        print(f"[DEBUG] Данные - словарь, ключи: {list(data.keys())}")
                        
                        # Проверяем различные возможные ключи
                        possible_keys = ['data', 'games', 'items', 'results', 'list']
                        for key in possible_keys:
                            if key in data:
                                games_list = data[key]
                                print(f"[DEBUG] Найден ключ '{key}', количество игр: {len(games_list)}")
                                break
                        
                        # Если не нашли стандартные ключи, проверяем структуру
                        if not games_list:
                            for key, value in data.items():
                                if isinstance(value, list):
                                    if value:
                                        first_item = value[0]
                                        if isinstance(first_item, dict):
                                            # Проверяем, есть ли нужные поля
                                            if 'app_id' in first_item or 'id' in first_item or 'name' in first_item:
                                                games_list = value
                                                print(f"[DEBUG] Использован ключ '{key}', количество игр: {len(games_list)}")
                                                break
                    
                    elif isinstance(data, list):
                        games_list = data
                        print(f"[DEBUG] Данные - список, количество игр: {len(games_list)}")
                    
                    else:
                        print(f"[DEBUG] Неизвестный формат данных: {type(data)}")
                        error_message = "Неизвестный формат ответа от сервера"
                    
                    if games_list and len(games_list) > 0:
                        print(f"[DEBUG] Пример первой игры: {games_list[0]}")
                        
                        # Логируем структуру первой игры
                        if isinstance(games_list[0], dict):
                            print(f"[DEBUG] Ключи первой игры: {list(games_list[0].keys())}")
                    
                    print(f"[DEBUG] Итоговое количество игр: {len(games_list)}")
                        
                except json.JSONDecodeError as e:
                    error_message = f"Ошибка декодирования JSON: {str(e)}"
                    print(f"[DEBUG] Ошибка JSONDecodeError: {e}")
                    print(f"[DEBUG] Ответ API (первые 200 символов): {response.text[:200]}")
                    
            else:
                error_message = f"Ошибка API: {response.status_code}"
                print(f"[DEBUG] API вернул ошибку: {response.status_code}")
                print(f"[DEBUG] Текст ошибки: {response.text[:500]}")
                
        except requests.exceptions.Timeout:
            error_message = "Таймаут соединения с API (более 15 секунд)"
            print("[DEBUG] Таймаут запроса к API")
        except requests.exceptions.ConnectionError as e:
            error_message = f"Ошибка соединения: {str(e)}"
            print(f"[DEBUG] Ошибка подключения к API: {e}")
        except Exception as e:
            error_message = f"Ошибка при загрузке игр: {str(e)}"
            print(f"[DEBUG] Неожиданная ошибка: {e}")
            import traceback
            traceback.print_exc()
    
    # Логика для POST запроса (отправка игры)
    elif request.method == 'POST':
        print("[DEBUG] Обработка POST запроса, отправка игры...")
        try:
            # Получаем данные из формы
            invite_url = request.form.get('invite_url')
            game_id = request.form.get('game_id')
            package_id = request.form.get('package_id')
            region = request.form.get('region')
            reference = request.form.get('reference', '')
            
            print(f"[DEBUG] Данные формы: game_id={game_id}, package_id={package_id}, region={region}")
            
            if not all([invite_url, game_id, package_id, region]):
                flash('Пожалуйста, заполните все обязательные поля', 'danger')
                return redirect(url_for('product4'))
            
            # Получаем информацию о цене игры
            api_key = '62e5589d9e984151936b3625afa32774'
            
            # Сначала получаем информацию об игре для получения цены
            game_info_url = f"https://desslyhub.com/api/v1/service/steamgift/games/{game_id}"
            print(f"[DEBUG] Получение информации об игре: {game_info_url}")
            
            game_response = requests.get(game_info_url, headers={'apikey': api_key})
            
            if game_response.status_code != 200:
                flash('Не удалось получить информацию об игре', 'danger')
                return redirect(url_for('product4'))
            
            game_data = game_response.json()
            print(f"[DEBUG] Информация об игре получена, тип данных: {type(game_data)}")
            
            # Ищем выбранный пакет и его цену
            game_price = 0
            package_found = False
            
            if isinstance(game_data, dict) and 'packages' in game_data:
                for package in game_data['packages']:
                    if str(package.get('id')) == str(package_id):
                        game_price = float(package.get('price', 0))
                        package_found = True
                        print(f"[DEBUG] Найден пакет: ID={package_id}, цена=${game_price}")
                        break
            
            if not package_found or game_price <= 0:
                flash('Не удалось определить цену выбранного издания', 'danger')
                return redirect(url_for('product4'))
            
            # РАСЧЕТ СКИДКИ И КОМИССИИ
            # Определяем базовую скидку
            base_discount = 2
            
            # ПРОВЕРЯЕМ ТАРИФ РЕСЕЛЛЕРА
            reseller_plan = user_info.get('reseller_plan', 'none')
            reseller_discounts = {
                'lite': 4,
                'reseller': 7,
                'pro': 10
            }
            
            if reseller_plan in reseller_discounts:
                reseller_discount = reseller_discounts[reseller_plan]
                current_discount = max(base_discount, reseller_discount)
                discount_source = 'reseller_plan'
            else:
                current_discount = base_discount
                discount_source = 'balance'
            
            # Проверяем индивидуальную скидку
            individual_discount = individual_discounts.get(username)
            if individual_discount is not None:
                current_discount = individual_discount
                discount_source = 'individual'
            
            print(f"[DEBUG] Скидка пользователя: {current_discount}%, источник: {discount_source}")
            
            # Рассчитываем финальную сумму
            amount_after_discount = game_price * (1 - current_discount / 100)
            amount_to_pay = amount_after_discount * (1 + steam_base_fee / 100)
            
            print(f"[DEBUG] Цена игры: ${game_price}, после скидки: ${amount_after_discount}, с комиссией: ${amount_to_pay}")
            
            # Проверяем баланс
            if total_balance < amount_to_pay:
                flash('Недостаточно средств на балансе', 'danger')
                return redirect(url_for('product4'))
            
            # Отправляем запрос к API для отправки игры
            payload = {
                "invite_url": invite_url,
                "package_id": package_id,
                "region": region
            }
            
            if reference:
                payload["reference"] = reference
            
            headers = {
                "apikey": api_key,
                "content-type": "application/json"
            }
            
            print(f"[DEBUG] Отправка игры в подарок, payload: {payload}")
            
            response = requests.post(
                'https://desslyhub.com/api/v1/service/steamgift/sendgames',
                json=payload,
                headers=headers,
                timeout=30
            )
            
            print(f"[DEBUG] Ответ от API отправки игры: статус {response.status_code}")
            
            if response.status_code == 200:
                api_data = response.json()
                print(f"[DEBUG] Данные ответа: {api_data}")
                
                if api_data.get('success') == True or api_data.get('status') == 'success':
                    # Успешный запрос - списываем средства
                    transaction_id = api_data.get('transaction_id', str(uuid.uuid4()))
                    
                    # Списываем средства с баланса
                    remaining = amount_to_pay
                    
                    if balances.get('card', 0) >= remaining:
                        users[username]['balance']['card'] -= remaining
                        remaining = 0
                    else:
                        card_balance = balances.get('card', 0)
                        if card_balance > 0:
                            users[username]['balance']['card'] = 0
                            remaining -= card_balance
                    
                    if remaining > 0 and balances.get('bep20', 0) >= remaining:
                        users[username]['balance']['bep20'] -= remaining
                        remaining = 0
                    elif remaining > 0:
                        bep20_balance = balances.get('bep20', 0)
                        if bep20_balance > 0:
                            users[username]['balance']['bep20'] = 0
                            remaining -= bep20_balance
                    
                    if remaining > 0 and balances.get('ton', 0) >= remaining:
                        users[username]['balance']['ton'] -= remaining
                        remaining = 0
                    
                    # Обновляем расходы
                    if username in users:
                        if 'expenses' not in users[username]:
                            users[username]['expenses'] = 0
                        users[username]['expenses'] += amount_to_pay
                    
                    # Получаем название игры для заказа
                    game_name = game_data.get('name', f'Steam Game ID: {game_id}')
                    
                    # Создаем заказ
                    formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
                    timestamp = get_moscow_time().timestamp()
                    
                    new_order = {
                        'id': str(uuid.uuid4()),
                        'category': 'Steam Gift',
                        'product': game_name,
                        'price': amount_to_pay,
                        'amount': 1,
                        'requested_amount': game_price,
                        'paid_amount': amount_to_pay,
                        'base_fee_applied': True,
                        'base_fee_percent': steam_base_fee,
                        'discount': current_discount,
                        'discount_source': discount_source,
                        'date': formatted_date,
                        'timestamp': timestamp,
                        'game_id': game_id,
                        'package_id': package_id,
                        'region': region,
                        'invite_url': invite_url,
                        'reference': reference,
                        'individual_discount_applied': individual_discount is not None,
                        'transaction_id': transaction_id,
                        'transaction_status': 'completed',
                        'external_service_used': True
                    }
                    
                    users[username].setdefault('userorders', []).append(new_order)
                    sync_user_balance(username)
                    save_data()
                    
                    # Отправляем уведомление в Telegram
                    send_telegram_notification_async(
                        username=username,
                        message_type='new_gift_order',
                        order_data=new_order
                    )
                    
                    flash('Игра успешно отправлена в подарок!', 'success')
                    return redirect(url_for('orders'))
                else:
                    error_msg = api_data.get('error', 'Неизвестная ошибка API')
                    flash(f'Ошибка при отправке игры: {error_msg}', 'danger')
            else:
                flash(f'Ошибка API: {response.status_code}', 'danger')
                
        except requests.exceptions.Timeout:
            flash('Таймаут соединения с API', 'danger')
        except requests.exceptions.ConnectionError:
            flash('Ошибка соединения с API', 'danger')
        except Exception as e:
            flash(f'Ошибка: {str(e)}', 'danger')
    
    # Скидки для пользователя
    discount_levels = [(0, 2)]
    reseller_plan = user_info.get('reseller_plan', 'none')
    if reseller_plan == 'lite':
        discount_levels = [(0, 4)]
    elif reseller_plan == 'reseller':
        discount_levels = [(0, 7)]
    elif reseller_plan == 'pro':
        discount_levels = [(0, 10)]

    current_discount_from_balance = discount_levels[0][1] if discount_levels else 2
    individual_discount = individual_discounts.get(username)

    if individual_discount is not None:
        current_discount = individual_discount
        discount_source = 'individual'
    else:
        current_discount = current_discount_from_balance
        discount_source = 'reseller_plan' if reseller_plan != 'none' else 'base'
    
    # Если список игр пуст и нет ошибки, добавляем тестовые данные
    if not games_list and not error_message:
        print("[DEBUG] Список игр пустой, используем тестовые данные")
        games_list = [
            {'app_id': '730', 'name': 'Counter-Strike 2', 'price': 0.00},
            {'app_id': '570', 'name': 'Dota 2', 'price': 0.00},
            {'app_id': '578080', 'name': 'PUBG: BATTLEGROUNDS', 'price': 0.00},
            {'app_id': '1172470', 'name': 'Apex Legends', 'price': 0.00},
            {'app_id': '271590', 'name': 'Grand Theft Auto V', 'price': 29.99},
            {'app_id': '292030', 'name': 'The Witcher 3: Wild Hunt', 'price': 39.99},
            {'app_id': '1245620', 'name': 'ELDEN RING', 'price': 59.99},
            {'app_id': '1085660', 'name': 'Destiny 2', 'price': 0.00},
            {'app_id': '1091500', 'name': 'Cyberpunk 2077', 'price': 59.99},
            {'app_id': '1240440', 'name': 'Halo Infinite', 'price': 0.00},
        ]
    
    print(f"[DEBUG] Рендеринг шаблона с {len(games_list)} играми")
    print(f"[DEBUG] Передаваемые переменные: username={username}, total_balance={total_balance}, error_message={error_message}")
    
    return render_template('10.product_4.html',
                           username=username,
                           balances=balances,
                           total_balance=total_balance,
                           games_list=games_list[:50],  # Ограничиваем список для производительности
                           error_message=error_message,
                           steam_base_fee=steam_base_fee,
                           current_discount=current_discount,
                           discount_levels=discount_levels,
                           individual_discount=individual_discount,
                           reseller_plan=reseller_plan)


@app.route('/api/steam_gift_game_info/<game_id>')
def get_steam_gift_game_info(game_id):
    """API endpoint для получения информации об игре и ее изданиях"""
    print(f"[DEBUG] API запрос информации об игре: {game_id}")
    
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        api_key = '62e5589d9e984151936b3625afa32774'
        url = f"https://desslyhub.com/api/v1/service/steamgift/games/{game_id}"
        headers = {'apikey': api_key}
        
        print(f"[DEBUG] Запрос информации об игре к API: {url}")
        
        response = requests.get(url, headers=headers, timeout=10)
        
        print(f"[DEBUG] Статус ответа: {response.status_code}")
        print(f"[DEBUG] Ответ API: {response.text[:500]}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"[DEBUG] Информация об игре получена, тип: {type(data)}")
            print(f"[DEBUG] Структура данных: {list(data.keys()) if isinstance(data, dict) else 'list'}")
            
            # Проверяем структуру ответа
            if isinstance(data, dict):
                if 'game' in data:
                    # Формат согласно документации: {"game": [...]}
                    game_data = data['game']
                else:
                    # Пытаемся найти данные в другом формате
                    game_data = data.get('packages', [])
                
                # Преобразуем данные в нужный формат для фронтенда
                packages = []
                if isinstance(game_data, list):
                    for item in game_data:
                        # Обрабатываем формат из документации
                        if 'regions_info' in item:
                            edition_name = item.get('edition', 'Standard Edition')
                            package_id = item.get('package_id', '')
                            
                            # Берем первую цену из списка регионов
                            if item['regions_info']:
                                first_region = item['regions_info'][0]
                                price = float(first_region.get('price', 0))
                                
                                packages.append({
                                    'id': str(package_id),
                                    'name': edition_name,
                                    'price': price
                                })
                elif isinstance(game_data, dict):
                    # Если данные пришли в другом формате
                    for key, value in game_data.items():
                        if isinstance(value, (int, float, str)):
                            packages.append({
                                'id': key,
                                'name': f'Издание {key}',
                                'price': float(value) if str(value).replace('.', '').isdigit() else 0
                            })
                
                return jsonify({'packages': packages})
            else:
                return jsonify({'packages': []})
        else:
            print(f"[DEBUG] Ошибка API: {response.status_code}")
            return jsonify({'error': f'API returned status {response.status_code}'}), 400
            
    except json.JSONDecodeError as e:
        print(f"[DEBUG] Ошибка декодирования JSON: {e}")
        print(f"[DEBUG] Ответ: {response.text[:500] if 'response' in locals() else 'No response'}")
        return jsonify({'error': f'Invalid JSON response: {str(e)}'}), 500
    except requests.exceptions.RequestException as e:
        print(f"[DEBUG] Ошибка запроса: {e}")
        return jsonify({'error': f'API request failed: {str(e)}'}), 500
    except Exception as e:
        print(f"[DEBUG] Неожиданная ошибка: {e}")
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500



# ====================== 11. ORDERS.HTML
@app.route('/orders')
def orders():
    if 'username' not in session:
        flash('Please login to view your orders', 'error')
        return redirect(url_for('login'))
    
    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Получаем статус пользователя
    user_status = user_info.get('status', 'active')
    freeze_reason = user_info.get('freeze_reason', '')
    
    # Рассчитываем сумму к возврату если статус "frozen" с возвратом средств
    refund_amount = 0
    tariff_cost = 0
    
    if user_status == 'frozen' and freeze_reason == 'Прекращение обслуживания - возврат средств':
        # Получаем стоимость тарифа и общий баланс для расчета возврата
        tariff_cost = user_info.get('tariff_cost', 0)
        total_user_balance = total_balance
        refund_amount = tariff_cost + total_user_balance
    
    # Получаем дату заморозки
    frozen_date = user_info.get('frozen_date', '')
    
    # Get user orders, sorted by timestamp (newest first)
    user_orders = user_info.get('userorders', [])
    user_orders_sorted = sorted(user_orders, key=lambda x: x.get('timestamp', 0), reverse=True)
    
    total_orders = len(user_orders_sorted)
    
    return render_template('11.orders.html',
                         username=username,
                         total_balance=total_balance,
                         user_orders=user_orders_sorted,
                         total_orders=total_orders,
                         user_status=user_status,
                         freeze_reason=freeze_reason,
                         refund_amount=refund_amount,
                         tariff_cost=tariff_cost,
                         frozen_date=frozen_date)



# ====================== 12. ACCOUNT.HTML 
@app.route('/account', methods=['GET', 'POST'])
def account():
    if 'username' not in session:
        flash('Please login to access your account', 'error')
        return redirect(url_for('login'))
    
    username = session['username']
    user_info = users.get(username, {})
    balances = user_info.get('balance', {})
    
    # Рассчитываем общий баланс (все типы балансов)
    total_balance = balances.get('card', 0) + balances.get('bep20', 0) + balances.get('ton', 0)
    
    # Получаем статус пользователя
    user_status = user_info.get('status', 'active')
    freeze_reason = user_info.get('freeze_reason', '')
    
    # Рассчитываем сумму к возврату если статус "frozen" с возвратом средств
    refund_amount = 0
    tariff_cost = 0
    
    if user_status == 'frozen' and freeze_reason == 'Прекращение обслуживания - возврат средств':
        tariff_cost = user_info.get('tariff_cost', 0)
        total_user_balance = total_balance
        refund_amount = tariff_cost + total_user_balance
    
    frozen_date = user_info.get('frozen_date', '')
    
    # Получаем общее количество заказов (с учетом ручной установки)
    if 'manual_orders_count' in user_info:
        total_orders = user_info['manual_orders_count']
    else:
        total_orders = user_info.get('orders', 0)
    
    # Получаем общие расходы (с учетом ручной установки)
    if 'manual_expenses' in user_info:
        total_expenses = user_info['manual_expenses']
    else:
        total_expenses = user_info.get('expenses', 0)
    
    # Получаем историю пополнений
    topup_history = user_info.get('topups', [])
    topup_history_sorted = sorted(topup_history, key=lambda x: x.get('timestamp', 0), reverse=True)
    
    # Получаем минимальное пополнение для пользователя
    min_topup = user_info.get('min_topup', 0)
    
    # Получаем информацию о последнем изменении минимального пополнения
    min_topup_last_change = user_info.get('min_topup_last_change', {})
    
    # Проверяем, было ли показано уведомление пользователю
    min_topup_notification_shown = user_info.get('min_topup_notification_shown', False)
    
    return render_template('12.account.html',
                         username=username,
                         balances=balances,
                         total_balance=total_balance,
                         user_status=user_status,
                         freeze_reason=freeze_reason,
                         refund_amount=refund_amount,
                         tariff_cost=tariff_cost,
                         frozen_date=frozen_date,
                         total_orders=total_orders,
                         total_expenses=total_expenses,
                         topup_history=topup_history_sorted,
                         min_topup=min_topup,
                         min_topup_last_change=min_topup_last_change,
                         min_topup_notification_shown=min_topup_notification_shown)


@app.route('/account/acknowledge_min_topup_notification', methods=['POST'])
def acknowledge_min_topup_notification():
    """Обработка подтверждения уведомления об изменении минимального пополнения"""
    
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    username = session['username']
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    # Помечаем, что уведомление было показано пользователю
    users[username]['min_topup_notification_shown'] = True
    
    # Сохраняем информацию в историю
    if 'admin_actions' not in users[username]:
        users[username]['admin_actions'] = []
    
    users[username]['admin_actions'].append({
        'type': 'acknowledge_min_topup_notification',
        'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'description': f'Пользователь подтвердил уведомление об изменении минимального пополнения'
    })
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': 'Уведомление подтверждено'
    })

# ====================== 13. PAYMENT PAGES.HTML 
@app.route('/payment/bep20', methods=['GET', 'POST'])
def payment_bep20():
    """Страница оплаты через BEP20"""
    
    if 'username' not in session:
        return redirect(url_for('login'))
    
    username = session['username']
    
    # Проверяем, не заморожен ли пользователь
    user_info = users.get(username, {})
    user_status = user_info.get('status', 'active')
    
    if user_status == 'frozen':
        flash('Пополнение баланса недоступно для замороженных аккаунтов', 'error')
        return redirect(url_for('account'))
    
    # Получаем данные из сессии
    payment_data = session.get('payment_data')
    if not payment_data or payment_data.get('method') != 'bep20':
        flash('Неверные данные оплаты', 'error')
        return redirect(url_for('account'))
    
    amount = payment_data.get('amount')
    
    # Загружаем адрес кошелька BEP20 из файла
    try:
        with open('payment_wallets.json', 'r') as f:
            wallets = json.load(f)
        wallet_address = wallets.get('bep20', '')
    except FileNotFoundError:
        wallet_address = "0x742d35Cc6634C0532925a3b8D4B5b875aD0B0000"  # fallback адрес
    
    if not wallet_address:
        flash('Адрес кошелька BEP20 не настроен', 'error')
        return redirect(url_for('account'))
    
    if request.method == 'POST':
        # Пользователь нажал "Оплачено"
        # Создаем запись о пополнении
        formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
        timestamp = get_moscow_time().timestamp()
        
        new_topup = {
            'id': str(uuid.uuid4()),
            'amount': amount,
            'method': 'bep20',
            'date': formatted_date,
            'timestamp': timestamp,
            'status': 'pending',
            'wallet_address': wallet_address,
            'payment_confirmed': False
        }
        
        # Добавляем в историю пополнений пользователя
        if 'topups' not in users[username]:
            users[username]['topups'] = []
        users[username]['topups'].append(new_topup)
        save_data()
        
        # Очищаем данные сессии
        session.pop('payment_data', None)
        
        # АСИНХРОННАЯ отправка уведомления в Telegram
        send_telegram_notification_async(
            username=username,
            message_type='payment',
            amount=amount,
            payment_method='BEP20 (USDT)'
        )
        
        return render_template('13.payment_waiting.html', 
                             username=username,
                             amount=amount,
                             method='BEP20 (USDT)',
                             wallet_address=wallet_address)
    
    # Устанавливаем время истечения (10 минут)
    expiry_time = get_moscow_time().timestamp() + 600  # 10 минут
    
    return render_template('13.payment_bep20.html',
                         username=username,
                         amount=amount,
                         wallet_address=wallet_address,
                         expiry_time=expiry_time)


@app.route('/payment/ton', methods=['GET', 'POST'])
def payment_ton():
    """Страница оплаты через TON"""
    
    if 'username' not in session:
        return redirect(url_for('login'))
    
    username = session['username']
    
    # Проверяем, не заморожен ли пользователь
    user_info = users.get(username, {})
    user_status = user_info.get('status', 'active')
    
    if user_status == 'frozen':
        flash('Пополнение баланса недоступно для замороженных аккаунтов', 'error')
        return redirect(url_for('account'))
    
    # Получаем данные из сессии
    payment_data = session.get('payment_data')
    if not payment_data or payment_data.get('method') != 'ton':
        flash('Неверные данные оплаты', 'error')
        return redirect(url_for('account'))
    
    amount = payment_data.get('amount')
    
    # Загружаем адрес кошелька TON из файла
    try:
        with open('payment_wallets.json', 'r') as f:
            wallets = json.load(f)
        wallet_address = wallets.get('ton', '')
    except FileNotFoundError:
        wallet_address = "UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N"  # fallback адрес
    
    if not wallet_address:
        flash('Адрес кошелька TON не настроен', 'error')
        return redirect(url_for('account'))
    
    if request.method == 'POST':
        # Пользователь нажал "Оплачено"
        # Создаем запись о пополнении
        formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
        timestamp = get_moscow_time().timestamp()
        
        new_topup = {
            'id': str(uuid.uuid4()),
            'amount': amount,
            'method': 'ton',
            'date': formatted_date,
            'timestamp': timestamp,
            'status': 'pending',
            'wallet_address': wallet_address,
            'payment_confirmed': False
        }
        
        # Добавляем в историю пополнений пользователя
        if 'topups' not in users[username]:
            users[username]['topups'] = []
        users[username]['topups'].append(new_topup)
        save_data()
        
        # Очищаем данные сессии
        session.pop('payment_data', None)
        
        # АСИНХРОННАЯ отправка уведомления в Telegram
        send_telegram_notification_async(
            username=username,
            message_type='payment',
            amount=amount,
            payment_method='TON (USDT)'
        )
        
        return render_template('13.payment_waiting.html', 
                             username=username,
                             amount=amount,
                             method='TON (USDT)',
                             wallet_address=wallet_address)
    
    # Устанавливаем время истечения (10 минут)
    expiry_time = get_moscow_time().timestamp() + 600  # 10 минут
    
    return render_template('13.payment_ton.html',
                         username=username,
                         amount=amount,
                         wallet_address=wallet_address,
                         expiry_time=expiry_time)


@app.route('/payment/create', methods=['POST'])
def create_payment():
    """Создание платежа и перенаправление на страницу оплаты"""
    
    if 'username' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    username = session['username']
    
    # Проверяем, не заморожен ли пользователь
    user_info = users.get(username, {})
    user_status = user_info.get('status', 'active')
    
    if user_status == 'frozen':
        return jsonify({
            'error': 'Пополнение баланса недоступно для замороженных аккаунтов'
        }), 403
    
    data = request.get_json()
    amount = data.get('amount')
    method = data.get('method')
    
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount format'}), 400
    
    # Проверяем минимальное пополнение, если установлено
    min_topup = user_info.get('min_topup', 0)
    
    if min_topup > 0 and amount < min_topup:
        return jsonify({
            'error': f'Минимальная сумма пополнения: ${min_topup}'
        }), 400
    
    if amount < 1 or amount > 10000:
        return jsonify({'error': 'Amount must be between $1 and $10,000'}), 400
    
    if method not in ['bep20', 'ton']:
        return jsonify({'error': 'Invalid payment method'}), 400
    
    # Сохраняем данные оплаты в сессии
    session['payment_data'] = {
        'amount': amount,
        'method': method,
        'timestamp': get_moscow_time().timestamp()
    }
    
    # Перенаправляем на соответствующую страницу оплаты
    if method == 'bep20':
        return jsonify({'redirect': url_for('payment_bep20')})
    else:  # ton
        return jsonify({'redirect': url_for('payment_ton')})



# ====================== АДМИН ФУНКЦИИ

@app.route('/admin')
def admin_dashboard():
    """Главная страница админ-панели"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    # Статистика для дашборда
    total_users = len(users) - 1  # исключаем админа
    active_users = len([u for u in users.values() if u.get('status', 'active') == 'active' and u != users.get('admin')])
    banned_users = total_users - active_users
    
    # Общая статистика по заказам и балансам
    total_orders = sum(len(u.get('userorders', [])) for u in users.values() if u != users.get('admin'))
    total_balance = sum(
        u.get('balance', {}).get('card', 0) + 
        u.get('balance', {}).get('ton', 0) + 
        u.get('balance', {}).get('bep20', 0) 
        for u in users.values() if u != users.get('admin')
    )
    total_expenses = sum(u.get('expenses', 0) for u in users.values() if u != users.get('admin'))
    
    # Последние 5 заказов
    all_orders = []
    for username, user_data in users.items():
        if username == 'admin':
            continue
        for order in user_data.get('userorders', []):
            order_with_user = order.copy()
            order_with_user['username'] = username
            all_orders.append(order_with_user)
    
    latest_orders = sorted(all_orders, key=lambda x: x.get('timestamp', 0), reverse=True)[:5]
    
    return render_template('15.admin_dashboard.html',
                         total_users=total_users,
                         active_users=active_users,
                         banned_users=banned_users,
                         total_orders=total_orders,
                         total_balance=total_balance,
                         total_expenses=total_expenses,
                         latest_orders=latest_orders)


# ====================== 1. УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
@app.route('/admin/users')
def admin_users():
    """Админская страница для управления пользователями"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    # Собираем информацию о всех пользователях
    users_list = []
    for username, user_info in users.items():
        if username == 'admin':  # Пропускаем самого админа
            continue
            
        # Рассчитываем общий баланс пользователя
        balance_data = user_info.get('balance', {'card': 0, 'ton': 0, 'bep20': 0})
        total_balance = balance_data.get('card', 0) + balance_data.get('ton', 0) + balance_data.get('bep20', 0)
        
        # Получаем режим работы заказов (по умолчанию 'api')
        order_mode = user_info.get('order_mode', 'api')
        
        # Получаем количество заказов (с учетом ручной установки)
        if 'manual_orders_count' in user_info:
            orders_count = user_info['manual_orders_count']
        else:
            orders_count = len(user_info.get('userorders', []))
        
        # Получаем расходы (с учетом ручной установки)
        if 'manual_expenses' in user_info:
            expenses = user_info['manual_expenses']
        else:
            expenses = user_info.get('expenses', 0)
        
        user_data = {
            'username': username,
            'status': user_info.get('status', 'active'),
            'ban_reason': user_info.get('ban_reason', ''),
            'freeze_reason': user_info.get('freeze_reason', ''),
            'balance': balance_data,
            'tariff_cost': user_info.get('tariff_cost', 0),
            'total_balance': total_balance,
            'orders_count': orders_count,  # Используем значение с учетом ручной установки
            'expenses': expenses,  # Используем значение с учетом ручной установки
            'registration_date': user_info.get('registration_date', 'N/A'),
            'last_login': user_info.get('last_login', 'N/A'),
            'contact_info': user_info.get('contact_info', ''),
            'contact_type': user_info.get('contact_type', ''),
            'order_mode': order_mode,
            # Добавляем информацию о наличии ручных значений для отладки
            'has_manual_orders': 'manual_orders_count' in user_info,
            'has_manual_expenses': 'manual_expenses' in user_info
        }
        users_list.append(user_data)
    
    # Сортируем по дате регистрации (новые сверху)
    def get_sort_key(user):
        reg_date = user['registration_date']
        if reg_date == 'N/A':
            return datetime.min
        try:
            return datetime.strptime(reg_date, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return datetime.min
    
    users_list_sorted = sorted(users_list, key=get_sort_key, reverse=True)
    
    return render_template('16.admin_users.html', users=users_list_sorted)

@app.route('/admin/user/<username>/update_counters', methods=['POST'])
def admin_update_user_counters(username):
    """Обновление счетчиков заказов и расходов пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    orders_count = data.get('orders_count')
    expenses = data.get('expenses')
    
    # Сохраняем старые значения для логирования
    old_orders = users[username].get('orders', 0)
    old_expenses = users[username].get('expenses', 0)
    
    # Обновляем счетчики если они переданы
    changes = []
    
    if orders_count is not None:
        try:
            new_orders = int(orders_count)
            # Сохраняем как отдельную переменную для ручной установки
            users[username]['manual_orders_count'] = new_orders
            # Также обновляем поле orders для обратной совместимости
            users[username]['orders'] = new_orders
            changes.append(f"заказы: {old_orders} -> {new_orders}")
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid orders count format'}), 400
    
    if expenses is not None:
        try:
            new_expenses = float(expenses)
            users[username]['manual_expenses'] = new_expenses
            users[username]['expenses'] = new_expenses
            changes.append(f"расходы: ${old_expenses:.2f} -> ${new_expenses:.2f}")
        except (ValueError, TypeError):
            return jsonify({'error': 'Invalid expenses format'}), 400
    
    # Логируем изменение
    if changes:
        log_security_event(
            username=session['username'],
            event_type='counters_manual_update',
            description=f'Ручное обновление счетчиков для {username}: {", ".join(changes)}',
            ip_address=request.remote_addr
        )
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Счетчики пользователя {username} обновлены',
        'new_orders': users[username].get('orders', 0),
        'new_expenses': users[username].get('expenses', 0),
        'manual_orders': users[username].get('manual_orders_count', 0),
        'manual_expenses': users[username].get('manual_expenses', 0)
    })


@app.route('/admin/user/<username>/update', methods=['POST'])
def admin_update_user(username):
    """Обновление статуса пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    status = data.get('status', 'active')
    ban_reason = data.get('ban_reason', '')
    freeze_reason = data.get('freeze_reason', '')
    tariff_cost = data.get('tariff_cost', '')
    total_balance = data.get('total_balance', '')
    
    # Сохраняем старый статус для логирования
    old_status = users[username].get('status', 'active')
    
    # Обновляем статус пользователя
    users[username]['status'] = status
    
    current_time = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
    
    if status == 'banned':
        users[username]['ban_reason'] = ban_reason
        users[username]['freeze_reason'] = ''  # Очищаем причину заморозки
        users[username]['tariff_cost'] = ''  # Очищаем стоимость тарифа
        users[username]['banned_by'] = session['username']
        users[username]['banned_date'] = current_time
        users[username].pop('frozen_by', None)
        users[username].pop('frozen_date', None)
        users[username].pop('unfrozen_by', None)
        users[username].pop('unfrozen_date', None)
        
        # Добавляем в историю блокировок
        if 'ban_history' not in users[username]:
            users[username]['ban_history'] = []
        users[username]['ban_history'].append({
            'action': 'banned',
            'by': session['username'],
            'reason': ban_reason,
            'date': current_time
        })
        
    elif status == 'frozen':
        users[username]['freeze_reason'] = freeze_reason
        users[username]['ban_reason'] = ''  # Очищаем причину блокировки
        users[username]['frozen_by'] = session['username']
        users[username]['frozen_date'] = current_time
        users[username].pop('banned_by', None)
        users[username].pop('banned_date', None)
        users[username].pop('unbanned_by', None)
        users[username].pop('unbanned_date', None)
        
        # Если причина - возврат средств, сохраняем детали
        if freeze_reason == 'Прекращение обслуживания - возврат средств':
            try:
                users[username]['tariff_cost'] = float(tariff_cost) if tariff_cost else 0
            except (ValueError, TypeError):
                users[username]['tariff_cost'] = 0
            
            # Рассчитываем текущий общий баланс
            balance_data = users[username].get('balance', {'card': 0, 'ton': 0, 'bep20': 0})
            calculated_total_balance = (
                balance_data.get('card', 0) + 
                balance_data.get('ton', 0) + 
                balance_data.get('bep20', 0)
            )
            users[username]['total_balance'] = calculated_total_balance
            
            # Рассчитываем сумму к возврату
            refund_amount = users[username]['tariff_cost'] + calculated_total_balance
            users[username]['refund_amount'] = refund_amount
            users[username]['refund_date'] = current_time
            users[username]['refund_processed_by'] = session['username']
            
            # Логируем инициирование возврата
            log_security_event(
                username=session['username'],
                event_type='refund_initiated',
                description=f'Инициирован возврат средств для {username}: ${refund_amount:.2f} (тариф: ${users[username]["tariff_cost"]:.2f}, баланс: ${calculated_total_balance:.2f})',
                ip_address=request.remote_addr
            )
        else:
            # Для других причин заморозки очищаем поля возврата
            users[username].pop('tariff_cost', None)
            users[username].pop('total_balance', None)
            users[username].pop('refund_amount', None)
            users[username].pop('refund_date', None)
            users[username].pop('refund_processed_by', None)
        
        # Добавляем в историю заморозок
        if 'freeze_history' not in users[username]:
            users[username]['freeze_history'] = []
        users[username]['freeze_history'].append({
            'action': 'frozen',
            'by': session['username'],
            'reason': freeze_reason,
            'date': current_time,
            'tariff_cost': users[username].get('tariff_cost', 0) if freeze_reason == 'Прекращение обслуживания - возврат средств' else None,
            'total_balance': users[username].get('total_balance', 0) if freeze_reason == 'Прекращение обслуживания - возврат средств' else None
        })
        
    elif status == 'active':
        # Очищаем все причины и информацию о предыдущих статусах
        users[username].pop('ban_reason', None)
        users[username].pop('freeze_reason', None)
        users[username].pop('tariff_cost', None)
        users[username].pop('total_balance', None)
        users[username].pop('refund_amount', None)
        users[username].pop('refund_date', None)
        users[username].pop('refund_processed_by', None)
        users[username].pop('banned_by', None)
        users[username].pop('banned_date', None)
        users[username].pop('frozen_by', None)
        users[username].pop('frozen_date', None)
        
        # Добавляем запись о разблокировке/разморозке в зависимости от предыдущего статуса
        if old_status == 'banned':
            users[username]['unbanned_by'] = session['username']
            users[username]['unbanned_date'] = current_time
            # Добавляем в историю
            if 'ban_history' in users[username]:
                users[username]['ban_history'].append({
                    'action': 'unbanned',
                    'by': session['username'],
                    'date': current_time
                })
        elif old_status == 'frozen':
            users[username]['unfrozen_by'] = session['username']
            users[username]['unfrozen_date'] = current_time
            # Добавляем в историю
            if 'freeze_history' in users[username]:
                users[username]['freeze_history'].append({
                    'action': 'unfrozen',
                    'by': session['username'],
                    'date': current_time
                })
    
    # Логируем изменение статуса
    log_security_event(
        username=session['username'],
        event_type='user_status_change',
        description=f'Изменен статус пользователя {username}: {old_status} -> {status}',
        ip_address=request.remote_addr
    )
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Статус пользователя {username} обновлен',
        'old_status': old_status,
        'new_status': status,
        'tariff_cost': users[username].get('tariff_cost', 0),
        'total_balance': users[username].get('total_balance', 0)
    })


@app.route('/admin/user/<username>/order_mode', methods=['POST'])
def admin_update_order_mode(username):
    """Обновление режима работы заказов пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    order_mode = data.get('order_mode', 'api')
    
    if order_mode not in ['demo', 'api']:
        return jsonify({'error': 'Invalid order mode'}), 400
    
    # Сохраняем старый режим для логирования
    old_mode = users[username].get('order_mode', 'api')
    
    # Обновляем режим работы заказов
    users[username]['order_mode'] = order_mode
    
    # Логируем изменение режима
    log_security_event(
        username=session['username'],
        event_type='order_mode_change',
        description=f'Изменен режим заказов пользователя {username}: {old_mode} -> {order_mode}',
        ip_address=request.remote_addr
    )
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Режим заказов пользователя {username} изменен на {order_mode}',
        'old_mode': old_mode,
        'new_mode': order_mode
    })


@app.route('/admin/user/<username>/delete', methods=['POST'])
def admin_delete_user(username):
    """Полное удаление пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    if username == 'admin':
        return jsonify({'error': 'Cannot delete admin user'}), 400
    
    # Полностью удаляем пользователя
    deleted_user = users.pop(username)
    save_data()
    
    # Логируем удаление
    log_security_event(
        username=session['username'],
        event_type='user_deleted',
        description=f'Удален пользователь: {username}',
        ip_address=request.remote_addr
    )
    
    # АСИНХРОННАЯ отправка уведомления в Telegram
    send_telegram_notification_async(
        username=session['username'],
        message_type='user_deleted',
        amount=None,
        payment_method=None,
        order_data={'deleted_user': username}
    )
    
    return jsonify({
        'success': True,
        'message': f'Пользователь {username} полностью удален'
    })


@app.route('/admin/user/<username>/balance/update', methods=['POST'])
def admin_update_user_balance(username):
    """Обновление баланса пользователя администратором"""
    
    print(f"🔧 DEBUG: Получен запрос на обновление баланса для пользователя: {username}")
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    data = request.get_json()
    print(f"🔧 DEBUG: Полученные данные: {data}")
    
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    method = data.get('method')
    action = data.get('action')
    amount = data.get('amount')
    
    print(f"🔧 DEBUG: Метод: {method}, Действие: {action}, Сумма: {amount}")
    print(f"🔧 DEBUG: Текущий баланс пользователя {username}: {users[username]['balance']}")
    
    try:
        amount = float(amount)
        if amount < 0:
            return jsonify({'error': 'Amount cannot be negative'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount format'}), 400
    
    if method not in ['card', 'ton', 'bep20']:
        return jsonify({'error': 'Invalid payment method'}), 400
    
    if action not in ['add', 'subtract', 'set']:
        return jsonify({'error': 'Invalid action'}), 400
    
    # Проверяем, можно ли изменять баланс (пользователь не заблокирован)
    if users[username].get('status') == 'banned':
        return jsonify({'error': 'Cannot modify balance for banned user'}), 400
    
    # Сохраняем старый баланс для отладки
    old_balance = users[username]['balance'].copy()
    
    # ВАЖНО: Устанавливаем флаг ручного изменения и время изменения
    users[username]['balance_manually_modified'] = True
    users[username]['balance_last_manual_update'] = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
    users[username]['balance_last_admin'] = session['username']
    
    # Обновляем баланс
    if action == 'add':
        users[username]['balance'][method] += amount
        balance_change = f"+{amount}"
    elif action == 'subtract':
        users[username]['balance'][method] = max(0, users[username]['balance'][method] - amount)
        balance_change = f"-{amount}"
    elif action == 'set':
        users[username]['balance'][method] = max(0, amount)
        balance_change = f"установлен на {amount}"
    
    print(f"🔧 DEBUG: Баланс изменен: {old_balance} -> {users[username]['balance']}")
    
    # Логируем изменение баланса
    log_security_event(
        username=session['username'],
        event_type='balance_manual_update',
        description=f'Ручное изменение баланса {username} ({method}): {balance_change} $',
        ip_address=request.remote_addr
    )
    
    # Выполняем синхронизацию с учетом ручного изменения
    sync_user_balance(username)
    
    # Сохраняем данные
    print("💾 Сохранение данных...")
    save_data()
    
    # Проверяем сохранение
    try:
        with open('users.json', 'r', encoding='utf-8') as f:
            saved_data = json.load(f)
        saved_balance = saved_data[username]['balance'][method]
        current_balance = users[username]['balance'][method]
        
        print(f"💾 Проверка: в памяти = {current_balance}, в файле = {saved_balance}")
        
        if saved_balance == current_balance:
            print("✅ Данные успешно сохранены!")
        else:
            print("❌ Данные не совпадают!")
            
    except Exception as e:
        print(f"❌ Ошибка при проверке: {e}")
    
    return jsonify({
        'success': True,
        'message': f'Баланс пользователя {username} обновлен',
        'new_balance': users[username]['balance'][method]
    })


# Вспомогательная функция для расчета суммы
def calculate_final_amount(requested_amount, discount_percent, base_fee_percent):
    """Рассчитывает итоговую сумму с учетом скидки и комиссии"""
    amount_after_discount = requested_amount * (1 - discount_percent / 100)
    discount_amount = requested_amount - amount_after_discount
    final_amount = amount_after_discount * (1 + base_fee_percent / 100)
    fee_amount = final_amount - amount_after_discount
    
    return {
        'requested_amount': requested_amount,
        'discount_percent': discount_percent,
        'discount_amount': discount_amount,
        'base_fee_percent': base_fee_percent,
        'fee_amount': fee_amount,
        'final_amount': final_amount,
        'amount_after_discount': amount_after_discount
    }


# Функция для логирования событий безопасности
def log_security_event(username, event_type, description, ip_address):
    """Логирование событий безопасности"""
    
    log_entry = {
        'timestamp': get_moscow_time().strftime('%Y-%m-%d %H:%M:%S'),
        'username': username,
        'event_type': event_type,
        'description': description,
        'ip_address': ip_address
    }
    
    # Загружаем существующие логи или создаем новые
    try:
        with open('security_logs.json', 'r', encoding='utf-8') as f:
            security_logs = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        security_logs = []
    
    # Добавляем новую запись
    security_logs.append(log_entry)
    
    # Сохраняем логи (ограничиваем количество записей, например, 1000)
    if len(security_logs) > 1000:
        security_logs = security_logs[-1000:]
    
    try:
        with open('security_logs.json', 'w', encoding='utf-8') as f:
            json.dump(security_logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка при сохранении логов безопасности: {e}")


# ====================== 2. УПРАВЛЕНИЕ СРЕДСТВАМИ
@app.route('/admin/finances')
def admin_finances():
    """Управление средствами пользователей"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    # Получаем параметр фильтрации
    filter_user = request.args.get('user', '')
    
    # Собираем все пополнения
    all_topups = []
    for username, user_info in users.items():
        if username == 'admin':
            continue
        user_topups = user_info.get('topups', [])
        for topup in user_topups:
            topup_with_user = topup.copy()
            topup_with_user['username'] = username
            # Применяем фильтр по пользователю
            if not filter_user or username == filter_user:
                all_topups.append(topup_with_user)
    
    # Сортируем по дате (новые сверху)
    all_topups_sorted = sorted(all_topups, key=lambda x: x.get('timestamp', 0), reverse=True)
    
    # Получаем список пользователей для выпадающего списка
    user_list = [username for username in users.keys() if username != 'admin']
    
    return render_template('17.admin_finances.html', 
                         topups=all_topups_sorted,
                         users=user_list,
                         filter_user=filter_user)  # Добавляем текущий фильтр


@app.route('/admin/topup/add', methods=['POST'])
def admin_add_topup():
    """Добавление пополнения вручную"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    username = data.get('username')
    amount = data.get('amount')
    method = data.get('method')
    status = data.get('status', 'completed')
    wallet_type = data.get('wallet_type', 'admin')
    custom_date = data.get('custom_date')
    custom_time = data.get('custom_time')
    custom_seconds = data.get('custom_seconds', '00')  # Новое поле: секунды
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid amount format'}), 400
    
    if method not in ['card', 'ton', 'bep20']:
        return jsonify({'error': 'Invalid payment method'}), 400
    
    # Определяем дату и время
    if custom_date and custom_time:
        try:
            # Собираем полную дату и время с секундами
            datetime_str = f"{custom_date} {custom_time}:{custom_seconds}"
            formatted_date = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
            timestamp = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S').timestamp()
        except ValueError:
            return jsonify({'error': 'Invalid date/time format'}), 400
    else:
        # Используем текущее время
        formatted_date = get_moscow_time().strftime('%Y-%m-%d %H:%M:%S')
        timestamp = get_moscow_time().timestamp()

    # Определяем адрес кошелька в зависимости от типа
    if wallet_type == 'crypto':
        # Используем реальные крипто-адреса из payment_wallets.json
        try:
            with open('payment_wallets.json', 'r') as f:
                payment_wallets = json.load(f)
            wallet_address = payment_wallets.get(method, 'Адрес не найден')
        except:
            wallet_address = 'Ошибка загрузки адреса'
    else:
        wallet_address = 'Административное пополнение'
    
    # Создаем запись о пополнении
    new_topup = {
        'id': str(uuid.uuid4()),
        'amount': amount,
        'method': method,
        'date': formatted_date,
        'timestamp': timestamp,
        'status': status,
        'payment_confirmed': status == 'completed',
        'added_by': session['username'],
        'wallet_address': wallet_address,
        'wallet_type': wallet_type  # Сохраняем тип кошелька
    }
    
    # Добавляем в историю пополнений пользователя
    users[username].setdefault('topups', []).append(new_topup)
    
    # Если статус completed, пополняем баланс
    if status == 'completed':
        if method not in users[username]['balance']:
            users[username]['balance'][method] = 0
        users[username]['balance'][method] += amount
    
    # Синхронизируем баланс
    sync_user_balance(username)
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Пополнение для {username} добавлено'
    })


@app.route('/admin/topup/<username>/<topup_id>/update', methods=['POST'])
def admin_update_topup(username, topup_id):
    """Обновление данных пополнения (дата, время, секунды, тип кошелька)"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users or 'topups' not in users[username]:
        return jsonify({'error': 'User or topup not found'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Находим пополнение
    topup_found = None
    for topup in users[username]['topups']:
        if topup['id'] == topup_id:
            topup_found = topup
            break
    
    if not topup_found:
        return jsonify({'error': 'Topup not found'}), 404
    
    # Получаем новые данные
    wallet_type = data.get('wallet_type', topup_found.get('wallet_type', 'admin'))
    custom_date = data.get('custom_date')
    custom_time = data.get('custom_time')
    custom_seconds = data.get('custom_seconds', '00')  # Новое поле: секунды
    
    # Обновляем дату, время и секунды если указаны
    if custom_date and custom_time:
        try:
            datetime_str = f"{custom_date} {custom_time}:{custom_seconds}"
            topup_found['date'] = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S')
            topup_found['timestamp'] = datetime.strptime(datetime_str, '%Y-%m-%d %H:%M:%S').timestamp()
        except ValueError:
            return jsonify({'error': 'Invalid date/time format'}), 400
    
    # Обновляем адрес кошелька в зависимости от типа
    if wallet_type == 'crypto':
        try:
            with open('payment_wallets.json', 'r') as f:
                payment_wallets = json.load(f)
            wallet_address = payment_wallets.get(topup_found['method'], 'Адрес не найден')
        except:
            wallet_address = 'Ошибка загрузки адреса'
    else:
        wallet_address = 'Административное пополнение'
    
    topup_found['wallet_address'] = wallet_address
    topup_found['wallet_type'] = wallet_type
    
    # Сохраняем изменения
    save_data()
    
    return jsonify({
        'success': True,
        'message': 'Данные пополнения обновлены'
    })


@app.route('/admin/topup/<username>/<topup_id>/update_status', methods=['POST'])
def admin_update_topup_status(username, topup_id):
    """Обновление статуса пополнения"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users or 'topups' not in users[username]:
        return jsonify({'error': 'User or topup not found'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    new_status = data.get('status')
    if new_status not in ['completed', 'pending', 'failed']:
        return jsonify({'error': 'Invalid status'}), 400
    
    # Находим пополнение
    topup_found = None
    for topup in users[username]['topups']:
        if topup['id'] == topup_id:
            topup_found = topup
            break
    
    if not topup_found:
        return jsonify({'error': 'Topup not found'}), 404
    
    old_status = topup_found['status']
    method = topup_found['method']
    amount = topup_found['amount']
    
    # Обновляем статус
    topup_found['status'] = new_status
    topup_found['payment_confirmed'] = new_status == 'completed'
    
    # Обрабатываем изменения баланса
    if old_status == 'completed' and new_status != 'completed':
        # Убираем сумму из баланса
        users[username]['balance'][method] -= amount
    elif old_status != 'completed' and new_status == 'completed':
        # Добавляем сумму в баланс
        if method not in users[username]['balance']:
            users[username]['balance'][method] = 0
        users[username]['balance'][method] += amount
    
    # Синхронизируем баланс
    sync_user_balance(username)
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Статус обновлен на "{new_status}"'
    })


@app.route('/admin/topup/<username>/<topup_id>/delete', methods=['POST'])
def admin_delete_topup(username, topup_id):
    """Удаление пополнения"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users or 'topups' not in users[username]:
        return jsonify({'error': 'User or topup not found'}), 404
    
    # Находим и удаляем пополнение
    original_topups = users[username]['topups']
    users[username]['topups'] = [topup for topup in original_topups if topup['id'] != topup_id]
    
    # Если удалили, синхронизируем баланс
    if len(users[username]['topups']) < len(original_topups):
        sync_user_balance(username)
        save_data()
        return jsonify({'success': True, 'message': 'Пополнение удалено'})
    else:
        return jsonify({'error': 'Topup not found'}), 404


@app.route('/admin/topup/<username>/clear', methods=['POST'])
def admin_clear_user_topups(username):
    """Очистка всей истории пополнений пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    # Очищаем историю пополнений
    users[username]['topups'] = []
    sync_user_balance(username)
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'История пополнений пользователя {username} очищена'
    })


# ====================== 3. УПРАВЛЕНИЕ ЗАКАЗАМИ
@app.route('/admin/orders')
def admin_orders():
    """Управление заказами пользователей"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    # Получаем параметры фильтрации
    filter_type = request.args.get('filter', 'recent')
    username_filter = request.args.get('username', '')
    
    # Собираем все заказы
    all_orders = []
    for username, user_info in users.items():
        if username == 'admin':
            continue
        if username_filter and username != username_filter:
            continue
            
        user_orders = user_info.get('userorders', [])
        for order in user_orders:
            order_with_user = order.copy()
            order_with_user['username'] = username
            all_orders.append(order_with_user)
    
    # Сортируем и фильтруем
    all_orders_sorted = sorted(all_orders, key=lambda x: x.get('timestamp', 0), reverse=True)
    
    if filter_type == 'recent':
        orders_to_show = all_orders_sorted[:15]
    else:
        orders_to_show = all_orders_sorted
    
    return render_template('18.admin_orders.html', 
                         orders=orders_to_show,
                         filter_type=filter_type,
                         username_filter=username_filter,
                         all_usernames=[u for u in users.keys() if u != 'admin'])


@app.route('/admin/order/<username>/<order_id>/update', methods=['POST'])
def admin_update_order(username, order_id):
    """Обновление заказа"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users or 'userorders' not in users[username]:
        return jsonify({'error': 'User or order not found'}), 404
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Находим заказ
    order_index = None
    for i, order in enumerate(users[username]['userorders']):
        if order['id'] == order_id:
            order_index = i
            break
    
    if order_index is None:
        return jsonify({'error': 'Order not found'}), 404
    
    # Обновляем поля
    if 'date' in data:
        users[username]['userorders'][order_index]['date'] = data['date']
    if 'status' in data:
        new_status = data['status']
        users[username]['userorders'][order_index]['status'] = new_status
        
        # Для Steam заказов также обновляем transaction_status
        if 'steamLogin' in users[username]['userorders'][order_index]:
            if new_status == 'completed':
                users[username]['userorders'][order_index]['transaction_status'] = 'success'
            elif new_status == 'failed':
                users[username]['userorders'][order_index]['transaction_status'] = 'failed'
            else:
                users[username]['userorders'][order_index]['transaction_status'] = 'pending'
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': 'Заказ обновлен'
    })

@app.route('/admin/order/<username>/<order_id>/delete', methods=['POST'])
def admin_delete_order(username, order_id):
    """Удаление заказа"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users or 'userorders' not in users[username]:
        return jsonify({'error': 'User or order not found'}), 404
    
    # Удаляем заказ
    original_orders = users[username]['userorders']
    users[username]['userorders'] = [order for order in original_orders if order['id'] != order_id]
    
    # Если удалили, синхронизируем баланс
    if len(users[username]['userorders']) < len(original_orders):
        sync_user_balance(username)
        save_data()
        return jsonify({'success': True, 'message': 'Заказ удален'})
    else:
        return jsonify({'error': 'Order not found'}), 404


# ====================== 4. НАСТРОЙКИ ПРИЕМА СРЕДСТВ
@app.route('/admin/payment_settings')
def admin_payment_settings():
    """Настройки приема средств"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    # Загружаем настройки кошельков
    try:
        with open('payment_wallets.json', 'r') as f:
            wallets = json.load(f)
    except FileNotFoundError:
        wallets = {
            'bep20': '0x742d35Cc6634C0532925a3b8D4B5b875aD0B0000',
            'ton': 'UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N'
        }
    
    return render_template('19.admin_payment_settings.html', wallets=wallets)


@app.route('/admin/payment_settings/update', methods=['POST'])
def admin_update_payment_settings():
    """Обновление настроек приема средств"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Сохраняем настройки кошельков
    wallets = {
        'bep20': data.get('bep20', ''),
        'ton': data.get('ton', '')
    }
    
    with open('payment_wallets.json', 'w') as f:
        json.dump(wallets, f, indent=4)
    
    return jsonify({
        'success': True,
        'message': 'Настройки приема средств обновлены'
    })



# ====================== 5. УПРАВЛЕНИЕ ДАННЫМИ (ИМПОРТ/ЭКСПОРТ)
@app.route('/admin/data_management')
def admin_data_management():
    """Управление данными - импорт и экспорт JSON файлов"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    # Получаем информацию о файлах
    files_info = {
        'users': {
            'name': 'users.json',
            'description': 'Данные пользователей',
            'size': get_file_size('users.json'),
            'last_modified': get_file_last_modified('users.json')
        },
        'steam_discounts': {
            'name': 'steam_discounts.json',
            'description': 'Настройки скидок Steam',
            'size': get_file_size('steam_discounts.json'),
            'last_modified': get_file_last_modified('steam_discounts.json')
        },
        'stores': {
            'name': 'stores.json',
            'description': 'Данные магазинов',
            'size': get_file_size('stores.json'),
            'last_modified': get_file_last_modified('stores.json')
        },
        'payment_wallets': {
            'name': 'payment_wallets.json',
            'description': 'Настройки кошельков',
            'size': get_file_size('payment_wallets.json'),
            'last_modified': get_file_last_modified('payment_wallets.json')
        }
    }
    
    return render_template('20.admin_data_management.html', files_info=files_info)

@app.route('/admin/data/export/<file_type>')
def admin_export_data(file_type):
    """Экспорт JSON файла"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    file_mapping = {
        'users': USERS_FILE,
        'steam_discounts': STEAM_DISCOUNTS_FILE,
        'stores': STORES_FILE,
        'payment_wallets': 'payment_wallets.json'
    }
    
    if file_type not in file_mapping:
        flash('Неверный тип файла', 'error')
        return redirect(url_for('admin_data_management'))
    
    filename = file_mapping[file_type]
    
    try:
        return send_file(
            filename,
            as_attachment=True,
            download_name=f"{file_type}_{get_moscow_time().strftime('%Y%m%d_%H%M%S')}.json",
            mimetype='application/json'
        )
    except FileNotFoundError:
        flash('Файл не найден', 'error')
        return redirect(url_for('admin_data_management'))

@app.route('/admin/data/import/<file_type>', methods=['POST'])
def admin_import_data(file_type):
    """Импорт JSON файла"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    file_mapping = {
        'users': USERS_FILE,
        'steam_discounts': STEAM_DISCOUNTS_FILE,
        'stores': STORES_FILE,
        'payment_wallets': 'payment_wallets.json'
    }
    
    if file_type not in file_mapping:
        return jsonify({'error': 'Invalid file type'}), 400
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    if not file.filename.lower().endswith('.json'):
        return jsonify({'error': 'File must be JSON'}), 400
    
    try:
        # Читаем и проверяем JSON
        content = file.read().decode('utf-8')
        data = json.loads(content)
        
        # Валидация данных в зависимости от типа файла
        if file_type == 'users':
            if not isinstance(data, dict):
                return jsonify({'error': 'Invalid users data format'}), 400
        elif file_type == 'steam_discounts':
            required_keys = ['base_fee', 'discount_levels', 'individual_discounts']
            if not all(key in data for key in required_keys):
                return jsonify({'error': 'Invalid steam discounts format'}), 400
        elif file_type == 'stores':
            if not isinstance(data, dict):
                return jsonify({'error': 'Invalid stores data format'}), 400
        elif file_type == 'payment_wallets':
            required_keys = ['bep20', 'ton']
            if not all(key in data for key in required_keys):
                return jsonify({'error': 'Invalid payment wallets format'}), 400
        
        # Создаем резервную копию
        backup_filename = f"{file_mapping[file_type]}.backup_{get_moscow_time().strftime('%Y%m%d_%H%M%S')}"
        try:
            with open(file_mapping[file_type], 'r') as original:
                with open(backup_filename, 'w') as backup:
                    backup.write(original.read())
        except FileNotFoundError:
            pass  # Если файла нет, пропускаем создание бэкапа
        
        # Сохраняем новые данные
        with open(file_mapping[file_type], 'w') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        
        # Перезагружаем данные в память
        load_data()
        
        return jsonify({
            'success': True,
            'message': f'Файл {file_type} успешно импортирован',
            'backup_created': os.path.exists(backup_filename)
        })
        
    except json.JSONDecodeError:
        return jsonify({'error': 'Invalid JSON file'}), 400
    except Exception as e:
        return jsonify({'error': f'Import failed: {str(e)}'}), 500

@app.route('/admin/data/backup/all')
def admin_backup_all_data():
    """Создание резервной копии всех данных"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    try:
        # Создаем папку для бэкапов если её нет
        backup_dir = 'backups'
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        timestamp = get_moscow_time().strftime('%Y%m%d_%H%M%S')
        backup_data = {}
        
        # Собираем все данные
        files_to_backup = {
            'users': USERS_FILE,
            'steam_discounts': STEAM_DISCOUNTS_FILE,
            'stores': STORES_FILE,
            'payment_wallets': 'payment_wallets.json'
        }
        
        for key, filename in files_to_backup.items():
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    backup_data[key] = json.load(f)
            except FileNotFoundError:
                backup_data[key] = None
        
        # Сохраняем бэкап
        backup_filename = f"{backup_dir}/backup_{timestamp}.json"
        with open(backup_filename, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, indent=4, ensure_ascii=False)
        
        return send_file(
            backup_filename,
            as_attachment=True,
            download_name=f"backup_{timestamp}.json",
            mimetype='application/json'
        )
        
    except Exception as e:
        flash(f'Ошибка при создании бэкапа: {str(e)}', 'error')
        return redirect(url_for('admin_data_management'))

# Вспомогательные функции
def get_file_size(filename):
    """Получает размер файла в читаемом формате"""
    try:
        size = os.path.getsize(filename)
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"
    except FileNotFoundError:
        return "Файл не найден"

def get_file_last_modified(filename):
    """Получает дату последнего изменения файла"""
    try:
        timestamp = os.path.getmtime(filename)
        return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
    except FileNotFoundError:
        return "Файл не найден"




# ====================== 21. УПРАВЛЕНИЕ ТАРИФАМИ РЕСЕЛЛЕРА 
@app.route('/admin/reseller_tariffs')
def admin_reseller_tariffs():
    """Управление тарифами реселлера"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        abort(403)
    
    # Собираем пользователей с тарифами
    users_with_tariffs = {}
    for username, user_data in users.items():
        if username == 'admin':
            continue
        
        reseller_plan = user_data.get('reseller_plan', 'none')
        if reseller_plan != 'none':
            users_with_tariffs[username] = {
                'plan': reseller_plan,
                'since': user_data.get('reseller_since', ''),
                'expires': user_data.get('reseller_expires', ''),
                'status': user_data.get('reseller_status', False),
                'min_topup': user_data.get('min_topup', 0)  # Добавляем минимальное пополнение
            }
    
    # Вычисляем оставшиеся дни для каждого пользователя
    current_time = get_moscow_time()
    for username, data in users_with_tariffs.items():
        if data['plan'] != 'pro' and data['expires']:
            try:
                expire_date = datetime.strptime(data['expires'], "%d.%m.%Y")
                delta = expire_date - current_time
                data['days_remaining'] = delta.days
            except Exception as e:
                print(f"Error parsing date for {username}: {e}")
                data['days_remaining'] = 0
    
    # Названия тарифов для отображения с ОБНОВЛЕННЫМИ скидками
    plan_names = {
        'lite': 'Lite (4% скидка)',
        'reseller': 'Reseller (6% скидка)',
        'pro': 'Pro+ (8% скидка)'
    }
    
    # Все пользователи без тарифов (для выпадающего списка)
    users_without_tariffs = [u for u in users.keys() 
                           if u != 'admin' and users.get(u, {}).get('reseller_plan', 'none') == 'none']
    
    return render_template('21.admin_reseller_tariffs.html',
                         steam_base_fee=steam_base_fee,
                         users_with_tariffs=users_with_tariffs,
                         users_without_tariffs=users_without_tariffs,
                         plan_names=plan_names)

@app.route('/admin/reseller_tariffs/set_min_topup', methods=['POST'])
def admin_set_min_topup():
    """Установка минимального пополнения для пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    username = data.get('username')
    min_topup = data.get('min_topup')
    notify_user = data.get('notify_user', True)  # По умолчанию уведомляем
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    try:
        min_topup = float(min_topup)
        if min_topup < 0:
            return jsonify({'error': 'Minimum topup cannot be negative'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid minimum topup format'}), 400
    
    # Сохраняем старое значение
    old_min_topup = users[username].get('min_topup', 0)
    
    # Устанавливаем минимальное пополнение
    users[username]['min_topup'] = min_topup
    
    # Записываем информацию о последнем изменении
    current_datetime = datetime.now()
    users[username]['min_topup_last_change'] = {
        'old_value': old_min_topup,
        'new_value': min_topup,
        'changed_by_admin': True,
        'date': current_datetime.strftime("%d.%m.%Y"),
        'time': current_datetime.strftime("%H:%M:%S"),
        'timestamp': current_datetime.timestamp()
    }
    
    # Сбрасываем флаг показа уведомления (если нужно уведомлять)
    if notify_user:
        users[username]['min_topup_notification_shown'] = False
    
    # Добавляем информацию в историю
    if 'admin_actions' not in users[username]:
        users[username]['admin_actions'] = []
    
    users[username]['admin_actions'].append({
        'type': 'set_min_topup',
        'old_value': old_min_topup,
        'new_value': min_topup,
        'date': current_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        'description': f'Администратор установил минимальное пополнение: ${min_topup} (было: ${old_min_topup})',
        'notify_user': notify_user
    })
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Минимальное пополнение установлено на ${min_topup} для пользователя {username}'
    })

@app.route('/admin/reseller_tariffs/remove_min_topup/<username>', methods=['POST'])
def admin_remove_min_topup(username):
    """Удаление минимального пополнения у пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    # Проверяем, установлено ли минимальное пополнение
    if 'min_topup' not in users[username]:
        return jsonify({'error': 'Minimum topup is not set for this user'}), 400
    
    # Сохраняем старое значение для истории
    old_min_topup = users[username]['min_topup']
    
    # Записываем информацию о последнем изменении
    current_datetime = datetime.now()
    users[username]['min_topup_last_change'] = {
        'old_value': old_min_topup,
        'new_value': 0,
        'changed_by_admin': True,
        'date': current_datetime.strftime("%d.%m.%Y"),
        'time': current_datetime.strftime("%H:%M:%S"),
        'timestamp': current_datetime.timestamp()
    }
    
    # Сбрасываем флаг показа уведомления
    users[username]['min_topup_notification_shown'] = False
    
    # Удаляем минимальное пополнение
    del users[username]['min_topup']
    
    # Добавляем информацию в историю
    if 'admin_actions' not in users[username]:
        users[username]['admin_actions'] = []
    
    users[username]['admin_actions'].append({
        'type': 'remove_min_topup',
        'old_value': old_min_topup,
        'date': current_datetime.strftime("%Y-%m-%d %H:%M:%S"),
        'description': f'Администратор удалил минимальное пополнение (было: ${old_min_topup})'
    })
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Минимальное пополнение удалено у пользователя {username}'
    })

@app.route('/admin/reseller_tariffs/update_base_fee', methods=['POST'])
def admin_update_base_fee():
    """Обновление базовой комиссии"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    new_fee = data.get('base_fee')
    try:
        new_fee = float(new_fee)
        if new_fee < 0 or new_fee > 100:
            return jsonify({'error': 'Commission must be between 0 and 100'}), 400
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid commission format'}), 400
    
    # Обновляем базовую комиссию
    global steam_base_fee
    steam_base_fee = new_fee
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Базовая комиссия обновлена до {new_fee}%'
    })

@app.route('/admin/reseller_tariffs/assign_tariff', methods=['POST'])
def admin_assign_tariff():
    """Назначение тарифа пользователю"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    username = data.get('username')
    plan = data.get('plan')
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    # Валидация тарифа
    valid_plans = ['lite', 'reseller', 'pro']
    if plan not in valid_plans:
        return jsonify({'error': 'Invalid plan'}), 400
    
    # Назначаем тариф пользователю
    users[username]['reseller_plan'] = plan
    users[username]['reseller_status'] = True
    users[username]['reseller_since'] = get_moscow_time().strftime("%d.%m.%Y")
    
    # Устанавливаем дату окончания для месячных тарифов
    if plan in ['lite', 'reseller']:
        expire_date = get_moscow_time() + timedelta(days=30)
        users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
    else:
        # Pro тариф - навсегда
        users[username]['reseller_expires'] = None
    
    # Добавляем информацию в историю
    purchase_history = users[username].get('purchase_history', [])
    purchase_history.append({
        'type': 'reseller_plan',
        'action': 'admin_assign',
        'plan': plan,
        'amount': 0,  # Бесплатно от администратора
        'date': get_moscow_time().strftime("%Y-%m-%d %H:%M:%S"),
        'description': f'Тариф {plan} назначен администратором'
    })
    users[username]['purchase_history'] = purchase_history
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Тариф {plan} успешно назначен пользователю {username}'
    })

@app.route('/admin/reseller_tariffs/remove_tariff/<username>', methods=['POST'])
def admin_remove_tariff(username):
    """Удаление тарифа у пользователя"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    # Проверяем, есть ли тариф у пользователя
    current_plan = users[username].get('reseller_plan', 'none')
    if current_plan == 'none':
        return jsonify({'error': 'User does not have a tariff'}), 400
    
    # Сохраняем информацию о удаленном тарифе
    removed_plan = current_plan
    
    # Удаляем тариф
    users[username]['reseller_plan'] = 'none'
    users[username]['reseller_status'] = False
    
    # Сохраняем информацию в историю
    purchase_history = users[username].get('purchase_history', [])
    purchase_history.append({
        'type': 'reseller_plan',
        'action': 'admin_remove',
        'plan': removed_plan,
        'amount': 0,
        'date': get_moscow_time().strftime("%Y-%m-%d %H:%M:%S"),
        'description': f'Тариф {removed_plan} удален администратором'
    })
    users[username]['purchase_history'] = purchase_history
    
    # Очищаем даты
    if 'reseller_since' in users[username]:
        del users[username]['reseller_since']
    if 'reseller_expires' in users[username]:
        del users[username]['reseller_expires']
    
    # Минимальное пополнение НЕ удаляем при снятии тарифа
    # Оно остается, даже если пользователь теряет тариф
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Тариф {removed_plan} удален у пользователя {username}'
    })

@app.route('/admin/reseller_tariffs/extend_tariff/<username>', methods=['POST'])
def admin_extend_tariff(username):
    """Продление тарифа пользователю"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    # Проверяем, есть ли тариф у пользователя
    current_plan = users[username].get('reseller_plan', 'none')
    if current_plan == 'none':
        return jsonify({'error': 'User does not have a tariff'}), 400
    
    # Для Pro тарифа продление не требуется
    if current_plan == 'pro':
        return jsonify({'error': 'Pro tariff is lifetime, no need to extend'}), 400
    
    # Продлеваем на 30 дней
    if 'reseller_expires' in users[username] and users[username]['reseller_expires']:
        try:
            expire_date = datetime.strptime(users[username]['reseller_expires'], "%d.%m.%Y")
            # Если тариф уже истек, продлеваем от сегодняшней даты
            if expire_date < datetime.now():
                expire_date = datetime.now()
            expire_date += timedelta(days=30)
            users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
        except Exception as e:
            print(f"Error parsing expire date: {e}")
            # Если ошибка парсинга, устанавливаем от сегодня
            expire_date = datetime.now() + timedelta(days=30)
            users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
    else:
        # Если даты окончания нет, устанавливаем от сегодня
        expire_date = datetime.now() + timedelta(days=30)
        users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
    
    # Сохраняем информацию в историю
    purchase_history = users[username].get('purchase_history', [])
    purchase_history.append({
        'type': 'reseller_plan',
        'action': 'admin_extend',
        'plan': current_plan,
        'amount': 0,
        'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'description': f'Тариф {current_plan} продлен на 30 дней администратором'
    })
    users[username]['purchase_history'] = purchase_history
    
    save_data()
    
    return jsonify({
        'success': True,
        'message': f'Тариф {current_plan} продлен на 30 дней для пользователя {username}'
    })

@app.route('/admin/reseller_tariffs/upgrade_tariff', methods=['POST'])
def admin_upgrade_tariff():
    """Апгрейд тарифа пользователю"""
    
    # Проверка прав администратора
    if 'username' not in session or session['username'] != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    username = data.get('username')
    new_plan = data.get('plan')
    
    if username not in users:
        return jsonify({'error': 'User not found'}), 404
    
    # Валидация нового тарифа
    valid_plans = ['lite', 'reseller', 'pro']
    if new_plan not in valid_plans:
        return jsonify({'error': 'Invalid plan'}), 400
    
    current_plan = users[username].get('reseller_plan', 'none')
    
    # Проверяем, что это действительно апгрейд
    plan_levels = {'none': 0, 'lite': 1, 'reseller': 2, 'pro': 3}
    current_level = plan_levels.get(current_plan, 0)
    new_level = plan_levels.get(new_plan, 0)
    
    if new_level <= current_level:
        return jsonify({'error': 'Can only upgrade to a higher plan'}), 400
    
    # Обновляем тариф
    old_plan = current_plan
    users[username]['reseller_plan'] = new_plan
    
    # Для Pro тарифа - навсегда, для других - сохраняем оставшееся время
    if new_plan == 'pro':
        users[username]['reseller_expires'] = None
    elif 'reseller_expires' in users[username] and users[username]['reseller_expires']:
        # Сохраняем текущую дату окончания
        pass
    else:
        # Если даты окончания нет, устанавливаем 30 дней
        expire_date = datetime.now() + timedelta(days=30)
        users[username]['reseller_expires'] = expire_date.strftime("%d.%m.%Y")
    
    # Сохраняем информацию в историю
    purchase_history = users[username].get('purchase_history', [])
    purchase_history.append({
        'type': 'reseller_plan',
        'action': 'admin_upgrade',
        'old_plan': old_plan,
        'new_plan': new_plan,
        'amount': 0,
        'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'description': f'Тариф улучшен с {old_plan} до {new_plan} администратором'
    })
    users[username]['purchase_history'] = purchase_history
    
    # Помечаем, что баланс был изменен вручную
    users[username]['balance_manually_modified'] = True
    users[username]['balance_last_manual_update'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Синхронизируем баланс
    sync_user_balance(username)
    save_data()
    
    message = f'Тариф пользователя {username} улучшен с {old_plan} до {new_plan}'
    
    return jsonify({
        'success': True,
        'message': message
    })







# ====================== ERROR HANDLERS
@app.errorhandler(404)
def page_not_found(e):
    """Обработчик для 404 ошибки - страница не найдена"""
    return render_template('404.html'), 404

@app.errorhandler(403)
def forbidden(e):
    """Обработчик для 403 ошибки - доступ запрещен"""
    return render_template('404.html'), 403

@app.errorhandler(500)
def internal_server_error(e):
    """Обработчик для 500 ошибки - внутренняя ошибка сервера"""
    return render_template('404.html'), 500


@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))
if __name__ == '__main__':
    app.run(debug=True)