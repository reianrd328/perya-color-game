import os
import random
import string
import time
import pymysql
import threading
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

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

# Global Super Admin Credentials
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Multi-Tenant Memory Pools
online_users = {}       # sid -> {username, room_id, is_admin}
pull_requests = {}      # room_id -> [usernames]
withdraw_requests = {}  # room_id -> [{username, amount, id}]
dice_results = {}       # room_id -> ["color", "color", "color"]

def get_db_connection():
    try:
        return pymysql.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 3306)),  
            user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASSWORD", ""),
            database=os.environ.get("DB_NAME", "perya_color_game"),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10
        )
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return None

def init_db():
    connection = get_db_connection()
    if connection is None:
        return
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) NOT NULL UNIQUE,
                password VARCHAR(255) NOT NULL,
                coins INT DEFAULT 1000,
                is_admin TINYINT DEFAULT 0,
                room_id VARCHAR(50) DEFAULT 'Server_1'
            )
        """)
        connection.commit()
        print("✅ Core MySQL schema check cleared successfully.")
    except Exception as e:
        print(f"❌ Database Initialization Error: {e}")
    finally:
        if cursor: cursor.close()
        if connection: connection.close()

def update_admin_panels(room_id):
    try:
        conn = get_db_connection()
        if not conn: return
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s AND is_admin = 0", (room_id,))
        res = cursor.fetchone()
        total_circulation = int(res['total']) if res and res['total'] else 0
        
        cursor.execute("SELECT username, coins, password, is_admin, room_id FROM users WHERE room_id = %s", (room_id,))
        all_users = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"🚨 Admin room sync error: {e}")
        return

    system_users = []
    for u in all_users:
        is_online = any(x['username'] == u['username'] and x['room_id'] == room_id for x in online_users.values())
        system_users.append({
            "username": u['username'],
            "coins": u['coins'],
            "password": u['password'],
            "is_admin": bool(u['is_admin']),
            "room_id": u['room_id'],
            "online": is_online
        })

    socketio.emit('admin_dashboard_update', {
        "users": system_users, 
        "total_coins": total_circulation,
        "pull_requests": pull_requests.get(room_id, []),
        "withdraw_requests": withdraw_requests.get(room_id, [])
    }, to=room_id)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join_game(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')
    room_id = data.get('room_id', 'Server_1').strip() 

    if not username or not password:
        return emit('login_failed', {"message": "Credentials missing."})

    # Check for Global Master Super-Admin Bypass
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": True}
        join_room(room_id)
        emit('user_status', {"username": username, "coins": 0, "is_admin": True, "room_id": room_id})
        update_admin_panels(room_id)
        return

    conn = get_db_connection()
    if not conn:
        return emit('login_failed', {"message": "Database offline."})

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as e:
        return emit('login_failed', {"message": f"Lookup Error: {e}"})

    if not user:
        return emit('login_failed', {"message": "Invalid Username or Password."})

    # Strict structural server locking check
    if bool(user['is_admin']):
        # Room Master Manager access check
        if user['room_id'] != room_id:
            return emit('login_failed', {"message": f"❌ Master Authorization locked to {user['room_id']} only."})
        
        online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": True}
        join_room(room_id)
        emit('user_status', {"username": username, "coins": user['coins'], "is_admin": True, "room_id": room_id})
        update_admin_panels(room_id)
    else:
        # Standard player access verification check
        if user['room_id'] != room_id:
            return emit('login_failed', {"message": f"❌ Access Denied: You are registered to {user['room_id']} only."})

        online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": False}
        join_room(room_id)
        emit('user_status', {"username": username, "coins": user['coins'], "is_admin": False, "room_id": room_id})
        update_admin_panels(room_id)

@socketio.on('request_pull')
def handle_request_pull():
    user_session = online_users.get(request.sid)
    if not user_session or user_session['is_admin']: return
    
    room_id = user_session['room_id']
    username = user_session['username']

    if room_id not in pull_requests: pull_requests[room_id] = []
    if username not in pull_requests[room_id]:
        pull_requests[room_id].append(username)
        
    update_admin_panels(room_id)
    emit('pull_requested_status', {"message": "Rope pull requested! Waiting for room master approval."})

@socketio.on('approve_pull')
def handle_approve_pull(data):
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: return
    
    room_id = admin_session['room_id']
    target_user = data.get('username')

    if room_id in pull_requests and target_user in pull_requests[room_id]:
        pull_requests[room_id].remove(target_user)
        # Broadcast to room to spin up all active views instantly
        socketio.emit('start_dice_spin', {}, to=room_id)

@socketio.on('execute_spin_results')
def handle_execute_spin_results():
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: return
    
    room_id = admin_session['room_id']
    
    # Calculate pure random land matrix configurations
    rolled_dice = [random.choice(COLORS) for _ in range(3)]
    dice_results[room_id] = rolled_dice
    
    socketio.emit('roll_result', {"dice": rolled_dice}, to=room_id)
    update_admin_panels(room_id)

@socketio.on('submit_withdraw')
def handle_submit_withdraw(data):
    user_session = online_users.get(request.sid)
    if not user_session or user_session['is_admin']: return
    
    room_id = user_session['room_id']
    username = user_session['username']
    try:
        amount = int(data.get('amount', 0))
    except ValueError:
        return emit('withdraw_response', {"success": False, "message": "Invalid amount structure."})

    if amount <= 0:
        return emit('withdraw_response', {"success": False, "message": "Amount must be greater than 0."})

    conn = get_db_connection()
    if not conn: return
    cursor = conn.cursor()
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    
    if not user or user['coins'] < amount:
        cursor.close()
        conn.close()
        return emit('withdraw_response', {"success": False, "message": "Insufficient coin balance."})

    # Lock requested tokens from balance immediately
    cursor.execute("UPDATE users SET coins = coins - %s WHERE username = %s", (amount, username))
    conn.commit()
    cursor.close()
    conn.close()

    if room_id not in withdraw_requests: withdraw_requests[room_id] = []
    withdraw_requests[room_id].append({
        "id": ''.join(random.choices(string.digits, k=6)),
        "username": username,
        "amount": amount
    })

    emit('user_status_update', {"coins": user['coins'] - amount})
    update_admin_panels(room_id)
    emit('withdraw_response', {"success": True, "message": "Withdraw request submitted to Room Master!"})

@socketio.on('process_withdraw')
def handle_process_withdraw(data):
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: return
    
    room_id = admin_session['room_id']
    req_id = data.get('id')
    action = data.get('action') # 'approve' or 'reject'

    if room_id not in withdraw_requests: return
    req = next((r for r in withdraw_requests[room_id] if r['id'] == req_id), None)
    if not req: return

    withdraw_requests[room_id].remove(req)

    if action == 'reject':
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (req['amount'], req['username']))
            conn.commit()
            cursor.close()
            conn.close()

    # Inform target player if they are active on terminal socket channels
    for sid, u_data in online_users.items():
        if u_data['username'] == req['username'] and u_data['room_id'] == room_id:
            conn = get_db_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute("SELECT coins FROM users WHERE username = %s", (req['username'],))
                updated_user = cursor.fetchone()
                socketio.emit('user_status_update', {"coins": updated_user['coins']}, to=sid)
                cursor.close()
                conn.close()
            break

    update_admin_panels(room_id)

@socketio.on('create_player')
def handle_create_player(data):
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: return
    
    room_id = admin_session['room_id']
    username = data.get('username', '').strip()
    is_target_admin = 1 if data.get('make_admin') else 0
    coins = int(data.get('coins', 1000))
    generated_password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    if not username:
        return emit('admin_error', {"message": "Username required."})

    conn = get_db_connection()
    if not conn: return emit('admin_error', {"message": "Database error."})

    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, password, coins, is_admin, room_id) VALUES (%s, %s, %s, %s, %s)",
            (username, generated_password, coins, is_target_admin, room_id)
        )
        conn.commit()
        cursor.close()
        conn.close()

        emit('player_created', {"username": username, "password": generated_password, "coins": coins, "role": "Master" if is_target_admin else "Player"})
        update_admin_panels(room_id)
    except Exception as e:
        emit('admin_error', {"message": "Username already exists in system."})

@socketio.on('leave_session')
def handle_leave_session():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        leave_room(room_id)
        del online_users[request.sid]
        update_admin_panels(room_id)
    emit('logout_confirmed', {})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_panels(room_id)

threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
