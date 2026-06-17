import os
import random
import string
import time
import threading
from flask import Flask, render_template, request
# Change the flask_socketio import line at the top to this:
from flask_socketio import SocketIO, emit, join_room
import mysql.connector

def _load_dotenv(path=".env"):
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Multi-Tenant Structural Engines
table_bets = {}         
online_users = {}       
pending_withdraws = []  
pull_requests = {}      

def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", 3306)),  
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "perya_color_game"),
        ssl_ca="",
        ssl_verify_cert=False
    )

def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username VARCHAR(50) PRIMARY KEY,
                password VARCHAR(50) NOT NULL,
                coins INT NOT NULL DEFAULT 0,
                total_earned INT NOT NULL DEFAULT 0,
                total_withdrawn INT NOT NULL DEFAULT 0,
                is_admin TINYINT(1) NOT NULL DEFAULT 0,
                room_id VARCHAR(20) NOT NULL DEFAULT 'Server_1',
                active TINYINT(1) NOT NULL DEFAULT 1
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("✅ Database multi-tenant environment validated.")
    except Exception as e:
        print(f"⚠️ Table verification bypass: {e}")

def update_admin_panels(room_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s", (room_id,))
        res = cursor.fetchone()
        
        # Decimal fix fully integrated here
        total_circulation = int(res['total']) if res and res['total'] else 0
        
        cursor.execute("SELECT username, coins, total_earned, total_withdrawn, password, is_admin, active FROM users WHERE room_id = %s", (room_id,))
        all_users = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"🚨 Admin room state sync error: {e}")
        return

    system_users = [{**u, "online": any(x['username'] == u['username'] for x in online_users.values()), "is_admin": bool(u['is_admin']), "active": bool(u['active'])} for u in all_users]
    
    active_players = []
    pending_rolls = []
    
    room_sids = [sid for sid, data in online_users.items() if data['room_id'] == room_id]
    for sid in room_sids:
        name = online_users[sid]['username']
        user_data = next((x for x in system_users if x['username'] == name), None)
        if user_data and not user_data['is_admin']:
            staked = sum(table_bets.get(room_id, {}).get(name, {}).values()) if room_id in table_bets and name in table_bets[room_id] else 0
            active_players.append({"username": name, "coins": user_data['coins'], "staked": staked, "total": user_data['coins'] + staked})
            if staked > 0:
                pending_rolls.append({"username": name, "amount": staked})

    socketio.emit('admin_dashboard_update', {
        "users": system_users, 
        "total_coins": total_circulation,
        "players": active_players,
        "pending_rolls": pending_rolls,
        "pull_requests": pull_requests.get(room_id, []),
        "pending_withdraws": [w for w in pending_withdraws if w.get('room_id') == room_id]
    }, to=room_id)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join_game(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')
    room_id = data.get('room_id', 'Server_1')

    # Hardcoded master system admin credentials bypass safety checks
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        request.sid_room = room_id
        join_room(room_id)
        emit('user_status', {
            'username': username,
            'is_admin': True,
            'room_id': room_id,
            'coins': 0
        })
        return

    connection = get_db_connection()
    if connection is None:
        emit('login_failed', {'message': 'Database connection error. Admin has not turned on the instance.'})
        return

    try:
        cursor = connection.cursor(dictionary=True)
        
        # LOOKUP MATCHES BOTH USER PROFILE AND ROOM ID SCOPE
        cursor.execute(
            "SELECT username, coins, room_id FROM users WHERE username = %s AND password = %s",
            (username, password)
        )
        user = cursor.fetchone()

        if user:
            # ENFORCE ROOM RESTRICITON MATCHING
            if user['room_id'] != room_id:
                emit('login_failed', {
                    'message': f'❌ Access Denied: Your profile is registered to {user["room_id"].replace("_", " ")}, not this table group.'
                })
                return

            request.sid_room = room_id
            join_room(room_id)
            
            emit('user_status', {
                'username': user['username'],
                'is_admin': False,
                'room_id': user['room_id'],
                'coins': int(user['coins'])
            })
        else:
            emit('login_failed', {'message': 'Invalid Username or Password profile configuration combination.'})
            
    except Exception as e:
        emit('login_failed', {'message': f'Database lookup crash: {str(e)}'})
    finally:
        cursor.close()
        connection.close()
@socketio.on('request_pull')
def handle_request_pull(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    room_id = sid_data['room_id']
    username = sid_data['username']

    if room_id not in pull_requests:
        pull_requests[room_id] = []
    if username not in pull_requests[room_id]:
        pull_requests[room_id].append(username)
    
    update_admin_panels(room_id)
    emit('pull_requested_status', {"message": "Request sent to Admin! Wait for approval."})

@socketio.on('grant_pull_permission')
def handle_grant_permission(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    room_id = sid_data['room_id']
    target_user = data.get('username')

    if room_id in pull_requests and target_user in pull_requests[room_id]:
        pull_requests[room_id].remove(target_user)
        for sid, u_data in online_users.items():
            if u_data['username'] == target_user and u_data['room_id'] == room_id:
                socketio.emit('pull_permission_granted', {}, to=sid)
                break
                
    update_admin_panels(room_id)

@socketio.on('place_bet')
def handle_place_bet(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    room_id = sid_data['room_id']
    username = sid_data['username']
    color = data.get('color')
    amount = int(data.get('amount', 0))

    if color not in COLORS or amount <= 0: return

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
    except Exception: return

    if room_id not in table_bets: table_bets[room_id] = {}
    if username not in table_bets[room_id]: table_bets[room_id][username] = {c: 0 for c in COLORS}

    table_bets[room_id][username][color] += amount
    emit('bet_placed', {"username": username, "color": color, "color_total": table_bets[room_id][username][color], "coins": new_balance})
    update_admin_panels(room_id)

@socketio.on('trigger_roll')
def handle_trigger_roll(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    room_id = sid_data['room_id']

    socketio.emit('dice_rolling', {}, to=room_id)
    dice_results = [random.choice(COLORS) for _ in range(3)]
    time.sleep(3)

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    except Exception: return

    payout_ledger = {}
    room_bets = table_bets.get(room_id, {})
    
    for username, bets in room_bets.items():
        total_won = 0
        if sum(bets.values()) <= 0: continue

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
    if room_id in table_bets: table_bets[room_id].clear()

    socketio.emit('roll_results', {"dice": dice_results, "payouts": payout_ledger}, to=room_id)
    update_admin_panels(room_id)

@socketio.on('create_player')
def handle_create_player(data):
    # Detect which room dashboard the admin is currently managing
    room_id = getattr(request, 'sid_room', None) or data.get('room_id')
    if not room_id:
        # Fallback tracking if room context isn't implicitly stored in the socket session
        for r_id, players in active_rooms.items():
            if request.sid in players:
                room_id = r_id
                break

    if not room_id:
        emit('admin_error', {'message': 'Could not identify your current active room group.'})
        return

    username = data.get('username', '').strip()
    starting_coins = data.get('coins', 1000)

    if not username:
        emit('admin_error', {'message': 'Username field cannot be left blank.'})
        return

    # Generate a simple 6-character random password string
    password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    connection = get_db_connection()
    if connection is None:
        emit('admin_error', {'message': 'Database connection is offline. Please Power On your Aiven database panel!'})
        return

    try:
        cursor = connection.cursor(dictionary=True)
        
        # 1. Check if username is already taken INSIDE THIS SPECIFIC SERVER GROUP
        cursor.execute(
            "SELECT id FROM users WHERE username = %s AND room_id = %s", 
            (username, room_id)
        )
        if cursor.fetchone():
            emit('admin_error', {'message': f'Username "{username}" is already taken in this server room group.'})
            return

        # 2. Insert new user bound strictly to this server room ID
        cursor.execute(
            "INSERT INTO users (username, password, coins, room_id, is_admin) VALUES (%s, %s, %s, %s, 0)",
            (username, password, starting_coins, room_id)
        )
        connection.commit()

        # Emit successful output feedback to the dashboard UI frame
        emit('player_created', {'username': username, 'password': password})
        
    except Exception as e:
        emit('admin_error', {'message': f'SQL Write Failure: {str(e)}'})
    finally:
        cursor.close()
        connection.close()
@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_panels(room_id)

with app.app_context():
    threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
