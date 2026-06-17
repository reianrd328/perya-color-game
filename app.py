import os
import random
import string
import time
import pymysql
import threading
import uuid
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

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

# Global Master Administrator configurations
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Memory structures
online_users = {}       # sid -> {username, room_id, is_admin}
pull_requests = {}      # room_id -> [usernames]
pending_withdraws = {}  # withdraw_id -> {username, room_id, amount}

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
        
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN room_id VARCHAR(50) DEFAULT 'Server_1'")
        except Exception:
            pass # Column already processed safely

        connection.commit()
        cursor.close()
        connection.close()
        print("✅ Database verification checks completely initialized.")
    except Exception as e:
        print(f"❌ Database Initialization Error: {e}")

def update_admin_panels(room_id):
    """Gathers and pushes data exclusively to the administrative controllers listening within room_id context."""
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT username, coins, password, is_admin FROM users WHERE room_id = %s", (room_id,))
        all_users = cursor.fetchall()
        cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s AND is_admin = 0", (room_id,))
        res = cursor.fetchone()
        total_circulation = int(res['total']) if res and res['total'] else 0
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"🚨 Admin room ledger state sync error: {e}")
        return

    system_users = [{
        "username": u['username'],
        "password": u['password'],
        "coins": u['coins'],
        "is_admin": bool(u['is_admin']),
        "online": any(x['username'] == u['username'] for x in online_users.values())
    } for u in all_users]

    room_withdraws = [{"id": wid, "username": w['username'], "amount": w['amount']} 
                      for wid, w in pending_withdraws.items() if w['room_id'] == room_id]

    socketio.emit('admin_dashboard_update', {
        "users": system_users, 
        "total_coins": total_circulation,
        "pull_requests": pull_requests.get(room_id, []),
        "pending_withdraws": room_withdraws
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
        return emit('login_failed', {"message": "Credentials cannot be blank."})

    # Global Master Override Validation Check
    if username == ADMIN_USERNAME:
        if password == ADMIN_PASSWORD:
            online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": True}
            join_room(room_id)
            emit('user_status', {"username": username, "coins": 0, "is_admin": True, "room_id": room_id})
            update_admin_panels(room_id)
            return
        else:
            return emit('login_failed', {"message": "Invalid Administrator Authentication."})

    # Segmented Player Verification Checks
    conn = get_db_connection()
    if not conn:
        return emit('login_failed', {"message": "Database storage subsystem is offline."})

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as e:
        return emit('login_failed', {"message": f"Subsystem standard lookup error: {e}"})

    if not user:
        return emit('login_failed', {"message": "Invalid Username or Password configuration."})

    # Strict isolation parameter check
    if user['room_id'] != room_id:
        return emit('login_failed', {"message": f"❌ Access Denied: This account is assigned to {user['room_id'].replace('_', ' ')} only."})

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

    if room_id not in pull_requests:
        pull_requests[room_id] = []
        
    if username not in pull_requests[room_id]:
        pull_requests[room_id].append(username)
        
    update_admin_panels(room_id)
    emit('pull_requested_status', {"message": "Rope pull authorization request sent to room administrator."})

@socketio.on('grant_pull_permission')
def handle_grant_permission(data):
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: return
    
    room_id = admin_session['room_id']
    target_user = data.get('username')

    if room_id in pull_requests and target_user in pull_requests[room_id]:
        pull_requests[room_id].remove(target_user)
        
        # Broadcast authorization selectively to the matching target socket session
        for sid, u_data in online_users.items():
            if u_data['username'] == target_user and u_data['room_id'] == room_id:
                socketio.emit('pull_permission_granted', {}, to=sid)
                break
                
        update_admin_panels(room_id)

@socketio.on('execute_pull')
def handle_execute_pull():
    user_session = online_users.get(request.sid)
    if not user_session: return
    
    room_id = user_session['room_id']
    
    # Broadcast the rolling sequence immediately to all connections inside this room
    socketio.emit('roll_start', {}, to=room_id)
    
    # Calculate geometric values
    time.sleep(1.5)  # Let the animation run
    result_dice = [random.choice(COLORS) for _ in range(3)]
    
    # Synchronize balance registers from database storage rules
    conn = get_db_connection()
    balances = {}
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT username, coins FROM users WHERE room_id = %s", (room_id,))
            for r in cursor.fetchall():
                balances[r['username']] = r['coins']
            cursor.close()
            conn.close()
        except Exception:
            pass

    socketio.emit('roll_result', {"dice": result_dice, "balances": balances}, to=room_id)

@socketio.on('admin_forced_pull')
def handle_admin_forced_pull():
    """Allows an administrator to forcefully invoke a spin sequence for all users connected to their room."""
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: return
    
    room_id = admin_session['room_id']
    
    # Trigger rolling animation layout sequence for everyone in this server group
    socketio.emit('roll_start', {}, to=room_id)
    
    # Execute the roll calculation engine
    result_dice = [random.choice(COLORS) for _ in range(3)]
    
    conn = get_db_connection()
    balances = {}
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT username, coins FROM users WHERE room_id = %s", (room_id,))
            for r in cursor.fetchall():
                balances[r['username']] = r['coins']
            cursor.close()
            conn.close()
        except Exception:
            pass

    socketio.emit('roll_result', {"dice": result_dice, "balances": balances}, to=room_id)

@socketio.on('request_withdraw')
def handle_request_withdraw(data):
    user_session = online_users.get(request.sid)
    if not user_session or user_session['is_admin']: return
    
    username = user_session['username']
    room_id = user_session['room_id']
    amount = int(data.get('amount', 0))

    if amount <= 0:
        return emit('withdraw_status', {"message": "Invalid cashout amount."})

    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        
        if not user or user['coins'] < amount:
            cursor.close()
            conn.close()
            return emit('withdraw_status', {"message": "❌ Action Blocked: Insufficient ledger capital balance."})

        # Deduct balance immediately from user wallet during verification lifecycle
        new_balance = user['coins'] - amount
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
        conn.commit()
        cursor.close()
        conn.close()

        w_id = str(uuid.uuid4())[:8]
        pending_withdraws[w_id] = {"username": username, "room_id": room_id, "amount": amount}
        
        emit('withdraw_status', {"message": "Cashout logged successfully. Waiting for admin approval.", "balance": new_balance})
        update_admin_panels(room_id)
    except Exception as e:
        emit('withdraw_status', {"message": f"Cashout processing error: {e}"})

@socketio.on('approve_withdraw')
def handle_approve_withdraw(data):
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: return
    
    room_id = admin_session['room_id']
    w_id = data.get('withdraw_id')

    if w_id in pending_withdraws:
        target_cashout = pending_withdraws[w_id]
        if target_cashout['room_id'] == room_id:
            # Drop the ticket out of processing queue
            del pending_withdraws[w_id]
            
            # Send notification update packet out to the target user if they are currently online
            for sid, u_data in online_users.items():
                if u_data['username'] == target_cashout['username']:
                    socketio.emit('withdraw_status', {"message": f"🎉 Your cashout request for {target_cashout['amount']} coins was approved!"}, to=sid)
                    break
            
            update_admin_panels(room_id)

@socketio.on('create_player')
def handle_create_player(data):
    admin_session = online_users.get(request.sid)
    if not admin_session or not admin_session['is_admin']: 
        return emit('admin_error', {"message": "Unauthorized access profile configuration denied."})
        
    room_id = admin_session['room_id']
    username = data.get('username', '').strip()
    coins = int(data.get('coins', 1000))
    generated_password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    if not username:
        return emit('admin_error', {"message": "Username parameter configuration invalid."})

    conn = get_db_connection()
    if not conn:
        return emit('admin_error', {"message": "Database engine dropped offline dynamically."})

    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, password, coins, is_admin, room_id) VALUES (%s, %s, %s, 0, %s)",
            (username, generated_password, coins, room_id)
        )
        conn.commit()
        cursor.close()
        conn.close()

        emit('player_created', {"username": username, "password": generated_password})
        update_admin_panels(room_id)
    except Exception as e:
        emit('admin_error', {"message": f"Enrollment write conflict. Profile might exist: {e}"})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_panels(room_id)

threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
