import os
import random
import string
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
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "perya_master_secret_key_8821!")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

# Global Super-Admin Credentials
GLOBAL_ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
GLOBAL_ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")

# Multi-Tenant Real-time Cache Indexes
table_bets = {}         # Track user bets per server table: {room_id: {username: {color: amt}}}
online_users = {}       # Track active session states: {sid: {username, room_id}}
pull_requests = {}      # Open rope pull authorization lists: {room_id: [usernames]}
withdraw_requests = {}  # Incremental auto-id container cache memory index tracking

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
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                password VARCHAR(255) NOT NULL,
                coins INT DEFAULT 1000,
                is_admin TINYINT DEFAULT 0,
                room_id VARCHAR(50) DEFAULT 'Server_1'
            )
        """)
        
        # Schema auto-update table for withdrawal requests architecture layout management
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                amount INT NOT NULL,
                room_id VARCHAR(50) NOT NULL,
                status VARCHAR(20) DEFAULT 'PENDING'
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"❌ Database Initialization Error: {e}")
    finally:
        cursor.close()
        conn.close()

# Automatically create tables execution structure engine
init_db()

def update_admin_panels(room_id):
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        # Sum total room balance circulation
        cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s AND is_admin = 0", (room_id,))
        res = cursor.fetchone()
        total_circulation = int(res['total']) if res and res['total'] else 0
        
        # Fetch current server metrics row parameters users
        cursor.execute("SELECT username, coins, password, is_admin FROM users WHERE room_id = %s", (room_id,))
        all_users = cursor.fetchall()
        
        # Read pending local withdrawal requests engine configuration
        cursor.execute("SELECT id, username, amount FROM withdrawals WHERE room_id = %s AND status = 'PENDING'", (room_id,))
        pending_w = cursor.fetchall()
    except Exception as e:
        print(f"🚨 Admin system layout syncing error occurred: {e}")
        return
    finally:
        cursor.close()
        conn.close()

    system_users = [{**u, "online": any(x['username'] == u['username'] for x in online_users.values())} for u in all_users]

    socketio.emit('admin_dashboard_update', {
        "users": system_users, 
        "total_coins": total_circulation,
        "pull_requests": pull_requests.get(room_id, []),
        "pending_withdraws": pending_w
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
        return emit('login_failed', {"message": "Credentials missing parameters."})

    # Rule 1: Master Default Admin Global Pass Access Check Rule configuration engine override hook
    if username == GLOBAL_ADMIN_USER and password == GLOBAL_ADMIN_PASS:
        online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": True}
        join_room(room_id)
        emit('user_status', {"username": username, "coins": 0, "is_admin": True, "room_id": room_id})
        update_admin_panels(room_id)
        return

    conn = get_db_connection()
    if not conn: return emit('login_failed', {"message": "Database error connection state offline."})
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
    except Exception as e:
        return emit('login_failed', {"message": f"Query execution failed: {e}"})
    finally:
        cursor.close()
        conn.close()

    if not user:
        return emit('login_failed', {"message": "Invalid Username or Access Key string credentials."})

    # Rule 2: Strict Room Boundary Isolation Validation Checks
    if user['is_admin'] == 0 and user['room_id'] != room_id:
        return emit('login_failed', {"message": f"❌ Access Blocked: Account profile locked inside {user['room_id'].replace('_', ' ')} structural boundary context rules."})

    online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": bool(user['is_admin'])}
    join_room(room_id)

    # Initializing memory data matrices maps safely tracking bets engine parameters structures layouts
    if room_id not in table_bets: table_bets[room_id] = {}
    if username not in table_bets[room_id]: table_bets[room_id][username] = {c: 0 for c in COLORS}

    emit('user_status', {"username": username, "coins": user['coins'], "is_admin": bool(user['is_admin']), "room_id": room_id})
    update_admin_panels(room_id)

@socketio.on('place_bets')
def handle_place_bets(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or sid_data.get('is_admin'): return
    
    room_id = sid_data['room_id']
    username = sid_data['username']
    client_bets = data.get('bets', {})

    total_staked = sum(max(0, int(v)) for v in client_bets.values())
    if total_staked <= 0: return

    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        
        if not user or user['coins'] < total_staked:
            return emit('admin_error', {"message": "Insufficient coin configurations to confirm this bet."})

        # Deduct balances safely locked array patterns parameters matrix configurations
        new_balance = user['coins'] - total_staked
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
        conn.commit()

        # Update cache tracking tables allocations map array loops elements values configurations 
        for color in COLORS:
            table_bets[room_id][username][color] += max(0, int(client_bets.get(color, 0)))

        emit('bets_updated', {"coins": new_balance})
        update_admin_panels(room_id)
    except Exception as e:
        print(f"Bet error: {e}")
    finally:
        cursor.close()
        conn.close()

@socketio.on('request_pull')
def handle_request_pull(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    room_id = sid_data['room_id']
    username = sid_data['username']

    if room_id not in pull_requests: pull_requests[room_id] = []
    if username not in pull_requests[room_id]:
        pull_requests[room_id].append(username)
    
    update_admin_panels(room_id)
    emit('pull_requested_status', {"message": "Rope pulling authorization pending admin validation loops."})

@socketio.on('grant_pull_permission')
def handle_grant_permission(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data.get('is_admin'): return
    room_id = sid_data['room_id']
    target_user = data.get('username')
    approve = data.get('approve', False)

    if room_id in pull_requests and target_user in pull_requests[room_id]:
        pull_requests[room_id].remove(target_user)
        if approve:
            for sid, u_data in online_users.items():
                if u_data['username'] == target_user and u_data['room_id'] == room_id:
                    socketio.emit('pull_permission_granted', {}, to=sid)
                    break
        update_admin_panels(room_id)

@socketio.on('execute_pull')
def handle_execute_pull(data):
    sid_data = online_users.get(request.sid)
    if not sid_data: return
    process_dice_roll_engine(sid_data['room_id'])

@socketio.on('admin_force_pull')
def handle_admin_force_pull(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data.get('is_admin'): return
    process_dice_roll_engine(sid_data['room_id'])

def process_dice_roll_engine(room_id):
    # Broadcast start of spinning across the isolated server room group channel container
    socketio.emit('dice_rolling_started', {}, to=room_id)
    
    # 3D Mechanical animation delay computation processing calculation execution loop context structures
    socketio.sleep(2)

    # Compute land results array calculation elements variables
    final_dice = [random.choice(COLORS) for _ in range(3)]
    
    conn = get_db_connection()
    if not conn: return

    try:
        cursor = conn.cursor()
        room_bets = table_bets.get(room_id, {})
        updated_balances = {}

        for player, bets in list(room_bets.items()):
            player_payout = 0
            total_refund = 0
            
            for color, stake in bets.items():
                if stake <= 0: continue
                matches = final_dice.count(color)
                if matches > 0:
                    player_payout += stake * (matches + 1)
                total_refund += stake

            if total_refund > 0:
                cursor.execute("SELECT coins FROM users WHERE username = %s", (player,))
                p_user = cursor.fetchone()
                current_bal = p_user['coins'] if p_user else 0
                final_bal = current_bal + player_payout
                
                cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (final_bal, player))
                updated_balances[player] = final_bal
            
            # Clear historical tracking maps cache matrix metrics values
            table_bets[room_id][player] = {c: 0 for c in COLORS}

        conn.commit()
        socketio.emit('roll_result', {"dice": final_dice, "balances": updated_balances}, to=room_id)
        update_admin_panels(room_id)
    except Exception as e:
        print(f"Engine payout structural loop failure crash: {e}")
    finally:
        cursor.close()
        conn.close()

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

        if not user or user['coins'] < amount:
            return emit('admin_error', {"message": "Insufficient cash balance parameters available."})

        # Safeguard deductions verification loop checks
        cursor.execute("UPDATE users SET coins = coins - %s WHERE username = %s", (amount, username))
        cursor.execute("INSERT INTO withdrawals (username, amount, room_id, status) VALUES (%s, %s, %s, 'PENDING')", (username, amount, room_id))
        conn.commit()

        emit('user_status', {"username": username, "coins": user['coins'] - amount, "is_admin": False, "room_id": room_id})
        update_admin_panels(room_id)
    except Exception as e:
        print(f"Withdraw submission fail: {e}")
    finally:
        cursor.close()
        conn.close()

@socketio.on('process_withdraw_decision')
def handle_process_withdraw(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data.get('is_admin'): return
    room_id = sid_data['room_id']
    w_id = data.get('id')
    approve = data.get('approve', False)

    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM withdrawals WHERE id = %s", (w_id,))
        record = cursor.fetchone()

        if record and record['status'] == 'PENDING':
            if approve:
                cursor.execute("UPDATE withdrawals SET status = 'APPROVED' WHERE id = %s", (w_id,))
            else:
                cursor.execute("UPDATE withdrawals SET status = 'DENIED' WHERE id = %s", (w_id,))
                # Return staked configuration components profiles balance back to user profile context logs
                cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (record['amount'], record['username']))
            conn.commit()
        update_admin_panels(room_id)
    except Exception as e:
        print(f"Approval management system transaction loop error context: {e}")
    finally:
        cursor.close()
        conn.close()

@socketio.on('create_player')
def handle_create_player(data):
    sid_data = online_users.get(request.sid)
    if not sid_data or not sid_data.get('is_admin'): return
    room_id = sid_data['room_id']
    username = data.get('username', '').strip()
    coins = int(data.get('coins', 1000))
    pwd = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (username, password, coins, is_admin, room_id) VALUES (%s, %s, %s, 0, %s)", (username, pwd, coins, room_id))
        conn.commit()
        update_admin_panels(room_id)
    except Exception as e:
        emit('admin_error', {"message": f"Unique Key Collision or insertion fail error context tracking parameters: {e}"})
    finally:
        cursor.close()
        conn.close()

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_panels(room_id)

if __name__ == '__main__':
    socketio.run(app, debug=True)
