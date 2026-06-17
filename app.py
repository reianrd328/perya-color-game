import os
import random
import string
import time
import pymysql
import threading
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

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Multi-Tenant Structural Engines
table_bets = {}         
online_users = {}       
pending_withdraws = []  
pull_requests = {}      

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
        print("❌ Cannot initialize DB: Connection offline")
        return
    try:
        cursor = connection.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                password VARCHAR(255) NOT NULL,
                coins INT DEFAULT 1000,
                is_admin TINYINT DEFAULT 0
            )
        """)
        
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN room_id VARCHAR(50) DEFAULT 'Server_1'")
            print("✅ Successfully injected missing 'room_id' column into users schema.")
        except Exception as col_err:
            if "Duplicate column name" in str(col_err) or "1060" in str(col_err):
                pass
            else:
                print(f"Schema update notice: {col_err}")

        connection.commit()
    except Exception as e:
        print(f"❌ Database Initialization Error: {e}")
    finally:
        if 'cursor' in locals() and cursor:
            cursor.close()
        if 'connection' in locals() and connection:
            connection.close()

def update_admin_panels(room_id):
    try:
        conn = get_db_connection()
        if not conn: return
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s", (room_id,))
        res = cursor.fetchone()
        
        total_circulation = int(res['total']) if res and res['total'] else 0
        
        cursor.execute("SELECT username, coins, password, is_admin FROM users WHERE room_id = %s", (room_id,))
        all_users = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"🚨 Admin room state sync error: {e}")
        return

    system_users = [{**u, "online": any(x['username'] == u['username'] for x in online_users.values()), "is_admin": bool(u['is_admin'])} for u in all_users]
    
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
    room_id = data.get('room_id', 'Server_1').strip() 

    if not username or not password:
        return emit('login_failed', {"message": "Credentials missing."})

    if username == ADMIN_USERNAME:
        if password == ADMIN_PASSWORD:
            online_users[request.sid] = {"username": username, "room_id": room_id}
            join_room(room_id)
            
            emit('user_status', {
                "username": username, "coins": 0,
                "is_admin": True, "room_id": room_id
            })
            update_admin_panels(room_id)
            return
        else:
            return emit('login_failed', {"message": "Invalid Administrator Password."})

    conn = get_db_connection()
    if not conn:
        return emit('login_failed', {"message": "Database offline."})

    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as e:
        return emit('login_failed', {"message": f"Database Lookup Error: {e}"})

    if not user:
        return emit('login_failed', {"message": "Invalid Username or Password."})

    if user['room_id'] != room_id:
        return emit('login_failed', {"message": f"❌ Access Denied: Registered to {user['room_id'].replace('_', ' ')} only."})

    online_users[request.sid] = {"username": username, "room_id": room_id}
    join_room(room_id)

    if room_id not in table_bets:
        table_bets[room_id] = {}
    if username not in table_bets[room_id]:
        table_bets[room_id][username] = {c: 0 for c in COLORS}

    emit('user_status', {
        "username": username, "coins": user['coins'],
        "is_admin": False, "room_id": room_id
    })
    update_admin_panels(room_id)

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

@socketio.on('create_player')
def handle_create_player(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: 
        return emit('admin_error', {"message": "Session expired. Log back in."})
        
    room_id = sid_data['room_id']
    username = data.get('username', '').strip()
    coins = int(data.get('coins', 1000))
    generated_password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    if not username:
        return emit('admin_error', {"message": "Username cannot be empty."})

    conn = get_db_connection()
    if not conn:
        return emit('admin_error', {"message": "Database offline."})

    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (username, password, coins, is_admin, room_id) VALUES (%s, %s, %s, 0, %s)",
            (username, generated_password, coins, room_id)
        )
        conn.commit()
        cursor.close()
        conn.close()

        emit('player_created', {"username": username, "password": generated_password, "coins": coins})
        update_admin_panels(room_id)
    except Exception as e:
        emit('admin_error', {"message": f"Database write blocked: {e}"})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_panels(room_id)

# Database generation thread
threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
