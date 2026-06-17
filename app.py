import os
import random
import string
import time
import threading  # 1. Added right here at the top!
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import mysql.connector

def _load_dotenv(path=".env"):
    """Loads local environment configurations for development mapping."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass

_load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "perya_secret_key_9921!")
# Force gevent mode explicitly to prevent conflicts
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Structural State Engines
table_bets = {}  
online_users = {}  
pending_withdraws = []  

def get_db_connection():
    """Establishes database connection with standard parameters and custom Aiven ports."""
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 3306)),  
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "perya_color_game"),
        ssl_mode="REQUIRED"  # Forces mysql-connector to use a secure connection to Aiven
    )

def init_db():
    """Validates structural tables safely without crashing web workers."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS codes (
                id INT AUTO_INCREMENT PRIMARY KEY,
                code VARCHAR(20) UNIQUE NOT NULL,
                amount INT NOT NULL,
                redeemed TINYINT(1) NOT NULL DEFAULT 0,
                redeemed_by VARCHAR(50) DEFAULT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR(50) PRIMARY KEY,
                password VARCHAR(50) NOT NULL,
                coins INT NOT NULL DEFAULT 0,
                total_earned INT NOT NULL DEFAULT 0,
                total_withdrawn INT NOT NULL DEFAULT 0,
                is_admin TINYINT(1) NOT NULL DEFAULT 0,
                active TINYINT(1) NOT NULL DEFAULT 1
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Database tables validated successfully.")
    except Exception as e:
        print(f"⚠️ Database initialization delayed or failed: {e}")

def update_admin_panels():
    """Compiles dashboard statistics and streams to authorized connections."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT SUM(coins) as total FROM users")
        res = cursor.fetchone()
        total_circulation = res['total'] if res and res['total'] else 0
        
        cursor.execute("SELECT username, coins, total_earned, total_withdrawn, password, is_admin, active FROM users")
        all_users = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"🚨 Admin metrics refresh error: {e}")
        return

    system_users = []
    for u in all_users:
        username = u['username']
        is_online = username in online_users.values() or username == ADMIN_USERNAME
        system_users.append({
            "username": username,
            "coins": u['coins'],
            "total_earned": u['total_earned'],
            "total_withdrawn": u['total_withdrawn'],
            "password": u['password'],
            "is_admin": bool(u['is_admin']),
            "active": bool(u['active']),
            "online": is_online
        })

    active_players = []
    pending_rolls = []
    
    for sid, name in online_users.items():
        if name == ADMIN_USERNAME: continue
        user_data = next((x for x in system_users if x['username'] == name), None)
        if user_data:
            staked = sum(table_bets.get(name, {}).values()) if name in table_bets else 0
            active_players.append({
                "username": name,
                "coins": user_data['coins'],
                "staked": staked,
                "total": user_data['coins'] + staked
            })
            if staked > 0:
                pending_rolls.append({"username": name, "amount": staked})

    socketio.emit('admin_dashboard_update', {
        "users": system_users, 
        "total_coins": total_circulation,
        "players": active_players,
        "pending_rolls": pending_rolls,
        "pending_withdraws": pending_withdraws
    })

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join_game(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return emit('login_failed', {"message": "Credentials missing."})

    if username == ADMIN_USERNAME:
        if password == ADMIN_PASSWORD:
            online_users[request.sid] = username
            emit('user_status', {"username": username, "coins": 0, "is_admin": True, "total_earned": 0, "total_withdrawn": 0})
            update_admin_panels()
            return
        else:
            print("❌ Wrong admin password match attempt.")
            return emit('login_failed', {"message": "Invalid Administrator Password Profiles."})

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as e:
        return emit('login_failed', {"message": "Database lookup timed out. Please try again."})

    if not user:
        return emit('login_failed', {"message": "Account not found. Contact your admin."})

    if user['password'] != password:
        return emit('login_failed', {"message": "Incorrect structural password."})

    if not user['active']:
        return emit('login_failed', {"message": "This account has been banned."})

    online_users[request.sid] = username
    if username not in table_bets:
        table_bets[username] = {c: 0 for c in COLORS}

    emit('user_status', {
        "username": username,
        "coins": user['coins'],
        "is_admin": False,
        "total_earned": user['total_earned'],
        "total_withdrawn": user['total_withdrawn']
    })
    update_admin_panels()

@socketio.on('place_bet')
def handle_place_bet(data):
    username = data.get('username')
    color = data.get('color')
    amount = int(data.get('amount', 0))

    if color not in COLORS or amount <= 0:
        return emit('bet_rejected', {"message": "Invalid bet selection."})

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()

        if not user or user['coins'] < amount:
            cursor.close()
            conn.close()
            return emit('bet_rejected', {"message": "Insufficient balance."})

        new_balance = user['coins'] - amount
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        return emit('bet_rejected', {"message": "Database transaction failure."})

    if username not in table_bets:
        table_bets[username] = {c: 0 for c in COLORS}
    table_bets[username][color] += amount

    emit('bet_placed', {
        "username": username,
        "color": color,
        "color_total": table_bets[username][color],
        "coins": new_balance
    })
    update_admin_panels()

@socketio.on('clear_bets')
def handle_clear_bets(data):
    username = data.get('username')
    if username not in table_bets: return

    refund = sum(table_bets[username].values())
    if refund <= 0: return

    table_bets[username] = {c: 0 for c in COLORS}

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        current_coins = cursor.fetchone()['coins']
        
        new_balance = current_coins + refund
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        return

    emit('bets_cleared', {"username": username, "refunded": refund, "coins": new_balance})
    update_admin_panels()

@socketio.on('trigger_roll')
def handle_trigger_roll(data):
    socketio.emit('dice_rolling', {})
    dice_results = [random.choice(COLORS) for _ in range(3)]
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    except Exception:
        return

    payout_ledger = {}
    for username, bets in table_bets.items():
        total_won = 0
        staked_total = sum(bets.values())
        if staked_total <= 0: continue

        for color, amount in bets.items():
            if amount <= 0: continue
            matches = dice_results.count(color)
            if matches > 0:
                total_won += (amount * matches) + amount

        if total_won > 0:
            cursor.execute("SELECT coins, total_earned FROM users WHERE username = %s", (username,))
            account = cursor.fetchone()
            new_coins = account['coins'] + total_won
            new_earned = account['total_earned'] + total_won
            cursor.execute("UPDATE users SET coins = %s, total_earned = %s WHERE username = %s", (new_coins, new_earned, username))
            payout_ledger[username] = total_won

    conn.commit()
    cursor.close()
    conn.close()
    table_bets.clear()

    socketio.emit('roll_results', {
        "dice": dice_results,
        "payouts": payout_ledger
    })
    update_admin_panels()

@socketio.on('request_withdrawal')
def handle_withdrawal_request(data):
    username = data.get('username')
    amount = int(data.get('amount', 0))

    if amount <= 0: return

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()

        if not user or user['coins'] < amount:
            cursor.close()
            conn.close()
            return emit('withdraw_result', {"success": False, "message": "Insufficient coins available."})

        new_balance = user['coins'] - amount
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
        conn.commit()
        cursor.close()
        conn.close()

        pending_withdraws.append({"id": len(pending_withdraws)+1, "username": username, "amount": amount})
        emit('user_status', {"username": username, "coins": new_balance})
        emit('withdraw_result', {"success": True, "message": "Withdraw request sent to dealer review layout."})
        update_admin_panels()
    except Exception:
        pass

@socketio.on('approve_withdraw')
def handle_approve_withdraw(data):
    req_id = int(data.get('id'))
    global pending_withdraws
    req = next((x for x in pending_withdraws if x['id'] == req_id), None)
    
    if req:
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT total_withdrawn FROM users WHERE username = %s", (req['username'],))
            current_withdrawn = cursor.fetchone()['total_withdrawn']
            new_withdrawn = current_withdrawn + req['amount']
            
            cursor.execute("UPDATE users SET total_withdrawn = %s WHERE username = %s", (new_withdrawn, req['username']))
            conn.commit()
            cursor.close()
            conn.close()
        except Exception:
            return

        pending_withdraws = [x for x in pending_withdraws if x['id'] != req_id]
        update_admin_panels()

@socketio.on('create_player')
def handle_create_player(data):
    username = data.get('username', '').strip()
    coins = int(data.get('coins', 1000))
    is_admin = 1 if data.get('is_admin') else 0
    generated_password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, password, coins, is_admin, active) VALUES (%s, %s, %s, %s, 1)",
            (username, generated_password, coins, is_admin)
        )
        conn.commit()
        cursor.close()
        conn.close()

        emit('player_created', {
            "username": username,
            "password": generated_password,
            "coins": coins,
            "is_admin": bool(is_admin)
        }, broadcast=True)
        update_admin_panels()
    except Exception:
        emit('admin_error', {"message": "Username taken or database connection dropped."})

@socketio.on('generate_code')
def handle_generate_code(data):
    amount = int(data.get('amount', 100))
    generated_voucher = '-'.join(''.join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(2))

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO codes (code, amount) VALUES (%s, %s)", (generated_voucher, amount))
        conn.commit()
        cursor.close()
        conn.close()
        emit('code_generated', {"code": generated_voucher, "amount": amount}, broadcast=True)
    except Exception:
        pass

@socketio.on('redeem_code')
def handle_redeem_code(data):
    username = data.get('username')
    code_str = data.get('code', '').strip().upper()

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM codes WHERE code = %s AND redeemed = 0", (code_str,))
        voucher = cursor.fetchone()

        if not voucher:
            cursor.close()
            conn.close()
            return emit('redeem_result', {"success": False, "message": "Invalid or already redeemed voucher."})

        cursor.execute("UPDATE codes SET redeemed = 1, redeemed_by = %s WHERE id = %s", (username, voucher['id']))
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        current_bal = cursor.fetchone()['coins']

        new_balance = current_bal + voucher['amount']
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
        conn.commit()
        cursor.close()
        conn.close()

        emit('redeem_result', {"success": True, "message": f"Successfully loaded {voucher['amount']} coins!"})
        emit('user_status', {"username": username, "coins": new_balance})
        update_admin_panels()
    except Exception:
        emit('redeem_result', {"success": False, "message": "Service busy."})

@socketio.on('terminate_player')
def handle_terminate_player(data):
    target = data.get('username')
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET active = 0, coins = 0 WHERE username = %s", (target,))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        return

    for sid, name in list(online_users.items()):
        if name == target:
            socketio.emit('force_logout', {"message": "Your account was banned by the admin."}, to=sid)
    update_admin_panels()

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        del online_users[request.sid]
    update_admin_panels()

# 2. Updated dynamic application background thread configuration right here:
with app.app_context():
    threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
