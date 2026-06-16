import os
import random
import string
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
socketio = SocketIO(app, cors_allowed_origins="*")

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# Active game state configurations tracking stakes
table_bets = {}  # Structure: { username: { color: amount } }
connected_admins = set()
online_users = {}  # Structure: { session_id: username }

def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 3306)), # Add port routing dynamically
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "perya_color_game"),
    )

def init_db():
    """Builds required schema blueprints safely at startup."""
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

init_db()

def update_admin_panels():
    """Helper method compiling structural dashboard metrics for real-time streaming."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Compile total system circulation parameters
    cursor.execute("SELECT SUM(coins) as total FROM users")
    res = cursor.fetchone()
    total_circulation = res['total'] if res and res['total'] else 0
    
    # Fetch structural tracking ledger for user rosters
    cursor.execute("SELECT username, coins, total_earned, total_withdrawn, password, is_admin, active FROM users")
    all_users = cursor.fetchall()
    
    cursor.close()
    conn.close()

    # Match system calculations against table stakes
    system_users = []
    for u in all_users:
        username = u['username']
        is_online = username in online_users.values() or username == ADMIN_USERNAME
        staked_amt = sum(table_bets.get(username, {}).values()) if username in table_bets else 0
        
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

    # Compile the active roster metrics
    active_players = []
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

    socketio.emit('user_list', {"users": system_users, "total_coins": total_circulation}, to=request.sid)
    socketio.emit('player_list', {"players": active_players}, to=request.sid)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join_game(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return emit('login_failed', {"message": "Invalid credentials profile missing."})

    # Core Default Fallback Admin Routing
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        online_users[request.sid] = username
        emit('user_status', {"username": username, "coins": 0, "is_admin": True, "total_earned": 0, "total_withdrawn": 0})
        update_admin_panels()
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        return emit('login_failed', {"message": "Account not found. Ask your admin to register you."})

    if user['password'] != password:
        cursor.close()
        conn.close()
        return emit('login_failed', {"message": "Incorrect structural password profile mismatch."})

    if not user['active']:
        cursor.close()
        conn.close()
        return emit('login_failed', {"message": "This account is banned from the live table."})

    cursor.close()
    conn.close()

    online_users[request.sid] = username
    if username not in table_bets:
        table_bets[username] = {c: 0 for c in COLORS}

    emit('user_status', {
        "username": username,
        "coins": user['coins'],
        "is_admin": bool(user['is_admin']),
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
        return emit('bet_rejected', {"message": "Invalid bet parameters structural layout."})

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()

    if not user or user['coins'] < amount:
        cursor.close()
        conn.close()
        return emit('bet_rejected', {"message": "Insufficient coins configuration layout."})

    # Deduct funds safely matching table state balances
    new_balance = user['coins'] - amount
    cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
    conn.commit()
    cursor.close()
    conn.close()

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

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    current_coins = cursor.fetchone()['coins']
    
    new_balance = current_coins + refund
    cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
    conn.commit()
    cursor.close()
    conn.close()

    emit('bets_cleared', {"username": username, "refunded": refund, "coins": new_balance})
    update_admin_panels()

@socketio.on('trigger_roll')
def handle_trigger_roll(data):
    # Broadcast early tumble state initialization
    socketio.emit('dice_rolling', {})

    # Calculate system landing engine indices
    dice_results = [random.choice(COLORS) for _ in range(3)]
    
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    payout_ledger = {}

    for username, bets in table_bets.items():
        total_won = 0
        staked_total = sum(bets.values())
        if staked_total <= 0: continue

        for color, amount in bets.items():
            if amount <= 0: continue
            matches = dice_results.count(color)
            if matches > 0:
                # Payout distribution engine logic: 1 match = 1:1, 2 matches = 2:1, 3 matches = 3:1 + return original stake
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

    # Clear active staging matrix tables completely for next game frame loop
    table_bets.clear()

    # Broadcast engine computational payouts downstream safely returning matching balances directly in payload
    socketio.emit('roll_results', {
        "dice": dice_results,
        "payouts": payout_ledger
    })
    update_admin_panels()

@socketio.on('create_player')
def handle_create_player(data):
    username = data.get('username', '').strip()
    coins = int(data.get('coins', 1000))
    is_admin = 1 if data.get('is_admin') else 0
    
    # Generate arbitrary alpha-numeric passcode layouts strings cleanly
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
    except mysql.connector.Error as e:
        emit('admin_error', {"message": "Username already taken or structural fault database index."})

@socketio.on('generate_code')
def handle_generate_code(data):
    amount = int(data.get('amount', 100))
    generated_voucher = '-'.join(''.join(random.choices(string.ascii_uppercase + string.digits, k=4)) for _ in range(2))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO codes (code, amount) VALUES (%s, %s)", (generated_voucher, amount))
    conn.commit()
    cursor.close()
    conn.close()

    emit('code_generated', {"code": generated_voucher, "amount": amount}, broadcast=True)

@socketio.on('redeem_code')
def handle_redeem_code(data):
    username = data.get('username')
    code_str = data.get('code', '').strip().upper()

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM codes WHERE code = %s AND redeemed = 0", (code_str,))
    voucher = cursor.fetchone()

    if not voucher:
        cursor.close()
        conn.close()
        return emit('redeem_result', {"success": False, "message": "Invalid or already redeemed system voucher."})

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

@socketio.on('terminate_player')
def handle_terminate_player(data):
    target = data.get('username')
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET active = 0, coins = 0 WHERE username = %s", (target,))
    conn.commit()
    cursor.close()
    conn.close()

    # Find target socket connections and disconnect them immediately
    for sid, name in list(online_users.items()):
        if name == target:
            socketio.emit('force_logout', {"message": "Your account was permanently banned by the dealer admin."}, to=sid)
            
    update_admin_panels()

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        del online_users[request.sid]
    update_admin_panels()

if __name__ == '__main__':
    socketio.run(app, debug=True)
