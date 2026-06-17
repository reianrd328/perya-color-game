import os
import random
import string
import threading
import uuid
import pymysql
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

# Global Super-Administrator Parameter Handlers
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Multi-Tenant Memory Pool Registries
online_users = {}       # Layout: { request.sid: {"username": x, "room_id": y} }
pull_requests = {}      # Layout: { "Server_X": [username1, username2] }
pending_withdraws = {}   # Layout: { "Server_X": [ {"id": uuid, "username": u, "amount": a} ] }

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
        print("❌ DB Initialization skipped: Engine Connection Offline")
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
        
        # Schema verification safety layer check
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN room_id VARCHAR(50) DEFAULT 'Server_1'")
        except Exception:
            pass # Column structure already exist block handle
            
        connection.commit()
        print("📊 Core Database Matrix Initialization Complete.")
    except Exception as e:
        print(f"❌ Database Initialization Error: {e}")
    finally:
        cursor.close()
        connection.close()

def update_admin_panels(room_id):
    """Computes isolation context aggregates and pushes payload state to active room overseers."""
    conn = get_db_connection()
    if not conn: return
    
    try:
        cursor = conn.cursor()
        
        # Filter stats solely by tenant scope
        cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s AND is_admin = 0", (room_id,))
        res = cursor.fetchone()
        total_circulation = int(res['total']) if res and res['total'] else 0
        
        cursor.execute("SELECT username, coins, password, is_admin FROM users WHERE room_id = %s", (room_id,))
        all_users = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"🚨 Admin state synchronization fault: {e}")
        return

    # Track structural connection signals inside online users pool matching current loop scope
    system_users = [
        {
            **u, 
            "online": any(x['username'] == u['username'] for x in online_users.values()), 
            "is_admin": bool(u['is_admin'])
        } for u in all_users
    ]
    
    socketio.emit('admin_dashboard_update', {
        "users": system_users, 
        "total_coins": total_circulation,
        "pull_requests": pull_requests.get(room_id, []),
        "pending_withdraws": pending_withdraws.get(room_id, [])
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
        return emit('login_failed', {"message": "Credentials cannot be empty."})

    # Master Administration Rule override match engine handler
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
            return emit('login_failed', {"message": "Invalid Global Master Password."})

    conn = get_db_connection()
    if not conn:
        return emit('login_failed', {"message": "Database server unreachable."})

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
    except Exception as e:
        return emit('login_failed', {"message": f"Query processing exception: {e}"})

    if not user:
        return emit('login_failed', {"message": "Invalid Account Username or Password."})

    # 🔒 Multi-Tenant Server Isolation Enforcer Guard
    if user['room_id'] != room_id:
        return emit('login_failed', {
            "message": f"❌ Access Denied: This account is locked to {user['room_id'].replace('_', ' ')}. You cannot cross-play on other tables!"
        })

    online_users[request.sid] = {"username": username, "room_id": room_id}
    join_room(room_id)

    emit('user_status', {
        "username": username, "coins": user['coins'],
        "is_admin": False, "room_id": room_id
    })
    update_admin_panels(room_id)

@socketio.on('request_pull')
def handle_request_pull():
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    room_id = sid_data['room_id']
    username = sid_data['username']

    if room_id not in pull_requests:
        pull_requests[room_id] = []
        
    if username not in pull_requests[room_id]:
        pull_requests[room_id].append(username)
    
    update_admin_panels(room_id)

@socketio.on('grant_pull_permission')
def handle_grant_permission(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data['username'] == ADMIN_USERNAME: return
    
    room_id = sid_data['room_id']
    target_user = data.get('username')

    if room_id in pull_requests and target_user in pull_requests[room_id]:
        pull_requests[room_id].remove(target_user)
        
        # Inform the room that a spin approval is granted, activating the client-side CSS loops
        socketio.emit('pull_permission_granted', {}, to=room_id)
        
        # Compute calculations after a 2.5 second spinning sequence
        threading.Timer(2.5, execute_dice_roll_calculation, [room_id]).start()

def execute_dice_roll_calculation(room_id):
    """Calculates results after the 3D rotation duration completes."""
    rolled_dice = [random.choice(COLORS) for _ in range(3)]
    
    # In a custom perya configuration structure, this context maps wins/losses
    # For baseline multi-tenant testing, users maintain active coin parameters
    conn = get_db_connection()
    balances = {}
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT username, coins FROM users WHERE room_id = %s", (room_id,))
            rows = cursor.fetchall()
            balances = {r['username']: r['coins'] for r in rows}
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Error reading balances: {e}")

    socketio.emit('roll_result', {
        "dice": rolled_dice,
        "balances": balances
    }, to=room_id)
    
    update_admin_panels(room_id)

@socketio.on('request_withdraw')
def handle_request_withdraw(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    
    room_id = sid_data['room_id']
    username = sid_data['username']
    amount = int(data.get('amount', 0))

    if amount <= 0: return

    conn = get_db_connection()
    if not conn: return
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        
        if user and user['coins'] >= amount:
            if room_id not in pending_withdraws:
                pending_withdraws[room_id] = []
                
            # Place withdrawal request in an escrow structural hold state
            pending_withdraws[room_id].append({
                "id": str(uuid.uuid4())[:8],
                "username": username,
                "amount": amount
            })
            update_admin_panels(room_id)
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Withdraw request failure: {e}")

@socketio.on('approve_withdraw')
def handle_approve_withdraw(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data['username'] == ADMIN_USERNAME: return
    
    room_id = sid_data['room_id']
    tx_id = data.get('tx_id')

    tx_list = pending_withdraws.get(room_id, [])
    tx = next((item for item in tx_list if item['id'] == tx_id), None)
    
    if tx:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                # Atomically deduct value inside the database rows
                cursor.execute(
                    "UPDATE users SET coins = coins - %s WHERE username = %s AND coins >= %s", 
                    (tx['amount'], tx['username'], tx['amount'])
                )
                conn.commit()
                cursor.close()
                conn.close()
            except Exception as e:
                print(f"Database balance update crash: {e}")
                
        tx_list.remove(tx)
        
        # Broadcast refreshed balance changes down to all active room subscribers
        refresh_room_balances(room_id)
        update_admin_panels(room_id)

@socketio.on('reject_withdraw')
def handle_reject_withdraw(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data['username'] == ADMIN_USERNAME: return
    
    room_id = sid_data['room_id']
    tx_id = data.get('tx_id')

    tx_list = pending_withdraws.get(room_id, [])
    tx = next((item for item in tx_list if item['id'] == tx_id), None)
    if tx:
        tx_list.remove(tx)
        update_admin_panels(room_id)

def refresh_room_balances(room_id):
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT username, coins FROM users WHERE room_id = %s", (room_id,))
        users = cursor.fetchall()
        cursor.close()
        conn.close()
        
        for u in users:
            for sid, data in online_users.items():
                if data['username'] == u['username']:
                    socketio.emit('user_status', {
                        "username": u['username'], "coins": u['coins'],
                        "is_admin": False, "room_id": room_id
                    }, to=sid)
    except Exception as e:
        print(f"Failure updating active player node views: {e}")

@socketio.on('create_player')
def handle_create_player(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data['username'] == ADMIN_USERNAME: return
        
    room_id = sid_data['room_id']
    username = data.get('username', '').strip()
    coins = int(data.get('coins', 1000))
    generated_password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    if not username: return

    conn = get_db_connection()
    if not conn: return

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
        emit('admin_error', {"message": f"Username matching validation constraints already exists: {e}"})

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_panels(room_id)

# Initialize schema on clean container background execution thread
threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
