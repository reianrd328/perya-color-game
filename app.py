import os
import random
import string
import pymysql
import threading
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "perya_secret_core_9921!@")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

# Global Master Overrides
SUPERADMIN_USER = "superadmin"
SUPERADMIN_PASS = "superpassword123"

# Multi-Tenant Memory Matrix
active_bets = {}       # structure: { room_id: { username: { color: amount } } }
online_sessions = {}   # structure: { sid: { username, room_id, is_admin } }
pull_requests = {}     # structure: { room_id: [ usernames ] }

def get_db_connection():
    try:
        return pymysql.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 3306)),  
            user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASSWORD", ""),
            database=os.environ.get("DB_NAME", "perya_color_game"),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5
        )
    except Exception as e:
        print(f"❌ DB Wire Malfunction: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(255) NOT NULL UNIQUE,
                    password VARCHAR(255) NOT NULL,
                    coins INT DEFAULT 1000,
                    room_id VARCHAR(50) DEFAULT 'Server_1',
                    is_admin TINYINT DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vouchers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    code VARCHAR(50) NOT NULL UNIQUE,
                    amount INT NOT NULL,
                    room_id VARCHAR(50) NOT NULL,
                    is_redeemed TINYINT DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    username VARCHAR(255) NOT NULL,
                    amount INT NOT NULL,
                    room_id VARCHAR(50) NOT NULL,
                    status VARCHAR(20) DEFAULT 'PENDING'
                )
            """)
            # Inject native admin accounts for 10 isolated sub-servers
            for idx in range(1, 11):
                r_name = f"Server_{idx}"
                cursor.execute("SELECT id FROM users WHERE username = %s AND room_id = %s", ("admin", r_name))
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO users (username, password, coins, room_id, is_admin) VALUES (%s, %s, %s, %s, 1)",
                        ("admin", f"adminpass{idx}", 0, r_name)
                    )
            conn.commit()
            print("🚀 Data Schema Pipelines Initialized and Synced.")
    except Exception as e:
        print(f"🚨 Schema failure: {e}")
    finally:
        conn.close()

def sync_admin_dashboard(room_id):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s AND is_admin = 0", (room_id,))
            circ_res = cursor.fetchone()
            total_coins = circ_res['total'] if circ_res['total'] else 0

            cursor.execute("SELECT username, password, coins FROM users WHERE room_id = %s AND is_admin = 0", (room_id,))
            room_users = cursor.fetchall()

            cursor.execute("SELECT id, username, amount FROM withdrawals WHERE room_id = %s AND status = 'PENDING'", (room_id,))
            pending_w = cursor.fetchall()

        socketio.emit('admin_dashboard_update', {
            "total_coins": int(total_coins),
            "users": room_users,
            "pull_requests": pull_requests.get(room_id, []),
            "pending_withdraws": pending_w
        }, to=room_id)
    except Exception as e:
        print(f"Sync issue: {e}")
    finally:
        conn.close()

@app.route('/')
def index(): return render_template('index.html')

@socketio.on('join_game')
def on_join(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')
    room_id = data.get('room_id', 'Server_1')

    if username == SUPERADMIN_USER and password == SUPERADMIN_PASS:
        online_sessions[request.sid] = {"username": username, "room_id": room_id, "is_admin": True}
        join_room(room_id)
        emit('user_status', {"username": username, "is_admin": True, "room_id": room_id})
        sync_admin_dashboard(room_id)
        return

    conn = get_db_connection()
    if not conn: return emit('login_failed', {"message": "Database Engine Offline."})
    
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
            user = cursor.fetchone()
            
        if not user:
            return emit('login_failed', {"message": "Invalid credentials provided."})
        
        if user['is_admin'] == 0 and user['room_id'] != room_id:
            return emit('login_failed', {"message": f"Access Denied: Enrolled in {user['room_id']}."})
            
        if user['is_admin'] == 1 and user['room_id'] != room_id:
            return emit('login_failed', {"message": "Admin/Master credential mismatch for this sub-server."})

        online_sessions[request.sid] = {"username": username, "room_id": room_id, "is_admin": bool(user['is_admin'])}
        join_room(room_id)
        
        emit('user_status', {"username": username, "is_admin": bool(user['is_admin']), "room_id": room_id, "coins": user['coins']})
        
        if user['is_admin']:
            sync_admin_dashboard(room_id)
            emit('admin_log', {"msg": f"Master verified. Listening to channel: {room_id}"})
        else:
            if room_id not in active_bets: active_bets[room_id] = {}
            if username not in active_bets[room_id]: active_bets[room_id][username] = {c: 0 for c in COLORS}
    finally:
        conn.close()

@socketio.on('place_bets')
def on_bet(data):
    session = online_sessions.get(request.sid)
    if not session or session['is_admin']: return
    
    room_id = session['room_id']
    username = session['username']
    client_bets = data.get('bets', {})
    total_staked = sum(max(0, int(v)) for v in client_bets.values())

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()
            if not user or user['coins'] < total_staked:
                return emit('system_message', {"msg": "❌ Transaction blocked: Insufficient Coin balance."})
            
            # Deduct immediately to lock stakes safely
            cursor.execute("UPDATE users SET coins = coins - %s WHERE username = %s", (total_staked, username))
            conn.commit()
            
            for color in COLORS:
                active_bets[room_id][username][color] = max(0, int(client_bets.get(color, 0)))
                
        emit('system_message', {"msg": f"✅ Locked structural stake of {total_staked} coins.", "balance": user['coins'] - total_staked})
        sync_admin_dashboard(room_id)
    finally:
        conn.close()

@socketio.on('request_pull')
def on_req_pull():
    s = online_sessions.get(request.sid)
    if not s or s['is_admin']: return
    r, u = s['room_id'], s['username']
    
    if r not in pull_requests: pull_requests[r] = []
    if u not in pull_requests[r]: pull_requests[r].append(u)
    
    emit('pull_requested_status', {"message": "Rope-pull telemetry request transmitted. Awaiting Master sign-off."})
    sync_admin_dashboard(r)

@socketio.on('handle_pull_request')
def handle_pull(data):
    s = online_sessions.get(request.sid)
    if not s or not s['is_admin']: return
    r = s['room_id']
    target = data.get('username')
    
    if r in pull_requests and target in pull_requests[r]:
        pull_requests[r].remove(target)
        if data.get('action') == 'approve':
            for sid, sdata in online_sessions.items():
                if sdata['username'] == target and sdata['room_id'] == r:
                    socketio.emit('pull_permission_granted', {}, to=sid)
                    break
        sync_admin_dashboard(r)

def execute_engine_spin(room_id):
    """Executes the core business logic of the Perya Color Game roll."""
    socketio.emit('dice_spin_start', {}, to=room_id)
    import gevent
    gevent.sleep(2) # Synchronous visual execution delay simulation

    results = [random.choice(COLORS) for _ in range(3)]
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            room_wagers = active_bets.get(room_id, {})
            for player, wagers in list(room_wagers.items()):
                payout = 0
                total_bet = sum(wagers.values())
                if total_bet == 0: continue
                
                for color, amount in wagers.items():
                    if amount > 0:
                        matches = results.count(color)
                        if matches > 0:
                            payout += amount + (amount * matches)
                
                cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (payout, player))
                cursor.execute("SELECT coins FROM users WHERE username = %s", (player,))
                curr_bal = cursor.fetchone()['coins']
                
                # Notify individual player channels
                for psid, pdata in online_sessions.items():
                    if pdata['username'] == player:
                        socketio.emit('dice_spin_stop', {"results": results, "balance": curr_bal}, to=psid)
                        socketio.emit('system_message', {"msg": f"Outcome: Obtained {payout} from game loop calculation.", "balance": curr_bal}, to=psid)

                # Reset profile stakes memory array
                active_bets[room_id][player] = {c: 0 for c in COLORS}
        conn.commit()
        socketio.emit('admin_log', {"msg": f"Engine execution sweep finished: {results}"}, to=room_id)
        sync_admin_dashboard(room_id)
    finally:
        conn.close()

@socketio.on('trigger_roll')
def on_roll():
    s = online_sessions.get(request.sid)
    if not s or s['is_admin']: return
    execute_engine_spin(s['room_id'])

@socketio.on('admin_force_pull')
def on_force_pull():
    s = online_sessions.get(request.sid)
    if not s or not s['is_admin']: return
    execute_engine_spin(s['room_id'])

@socketio.on('request_withdrawal')
def on_withdraw(data):
    s = online_sessions.get(request.sid)
    if not s or s['is_admin']: return
    amt = int(data.get('amount', 0))
    if amt <= 0: return
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT coins FROM users WHERE username = %s", (s['username'],))
            ubal = cursor.fetchone()['coins']
            if ubal < amt:
                return emit('system_message', {"msg": "❌ Withdrawal denied: Balance exceeded."})
                
            cursor.execute("UPDATE users SET coins = coins - %s WHERE username = %s", (amt, s['username']))
            cursor.execute("INSERT INTO withdrawals (username, amount, room_id) VALUES (%s, %s, %s)", (s['username'], amt, s['room_id']))
            conn.commit()
            emit('system_message', {"msg": "⏳ Withdrawal request queued for validation.", "balance": ubal - amt})
        sync_admin_dashboard(s['room_id'])
    finally:
        conn.close()

@socketio.on('handle_withdrawal_request')
def handle_w(data):
    s = online_sessions.get(request.sid)
    if not s or not s['is_admin']: return
    w_id = data.get('id')
    action = data.get('action')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM withdrawals WHERE id = %s", (w_id,))
            wreq = cursor.fetchone()
            if not wreq: return
            
            if action == 'approve':
                cursor.execute("UPDATE withdrawals SET status = 'APPROVED' WHERE id = %s", (w_id,))
                msg = f"💸 Withdrawal profile transaction of {wreq['amount']} approved by room master."
            else:
                cursor.execute("UPDATE withdrawals SET status = 'DENIED' WHERE id = %s", (w_id,))
                cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (wreq['amount'], wreq['username']))
                msg = f"❌ Withdrawal of {wreq['amount']} rejected. Asset volume returned to pool."
                
        conn.commit()
        sync_admin_dashboard(s['room_id'])
        # Broadcast context update directly to user endpoints
        for psid, pdata in online_sessions.items():
            if pdata['username'] == wreq['username']:
                socketio.emit('system_message', {"msg": msg}, to=psid)
    finally:
        conn.close()

@socketio.on('generate_voucher')
def on_gen_vouch(data):
    s = online_sessions.get(request.sid)
    if not s or not s['is_admin']: return
    amt = int(data.get('amount', 0))
    code = "VCH-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO vouchers (code, amount, room_id) VALUES (%s, %s, %s)", (code, amt, s['room_id']))
        conn.commit()
        emit('voucher_created', {"code": code, "amount": amt})
    finally:
        conn.close()

@socketio.on('redeem_voucher')
def on_redeem(data):
    s = online_sessions.get(request.sid)
    if not s or s['is_admin']: return
    code = data.get('code', '')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM vouchers WHERE code = %s AND is_redeemed = 0", (code,))
            v = cursor.fetchone()
            if not v:
                return emit('system_message', {"msg": "❌ Invalid token authentication or already claimed."})
            if v['room_id'] != s['room_id']:
                return emit('system_message', {"msg": "❌ Region Conflict: This voucher belongs to a different table segment."})
                
            cursor.execute("UPDATE vouchers SET is_redeemed = 1 WHERE id = %s", (v['id'],))
            cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (v['amount'], s['username']))
            cursor.execute("SELECT coins FROM users WHERE username = %s", (s['username'],))
            ubal = cursor.fetchone()['coins']
        conn.commit()
        emit('system_message', {"msg": f"🎉 Voucher applied! Added {v['amount']} coins.", "balance": ubal})
        sync_admin_dashboard(s['room_id'])
    finally:
        conn.close()

@socketio.on('create_player')
def on_create(data):
    s = online_sessions.get(request.sid)
    if not s or not s['is_admin']: return
    uname = data.get('username', '').strip()
    gen_pass = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    
    if not uname: return
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO users (username, password, room_id) VALUES (%s, %s, %s)", (uname, gen_pass, s['room_id']))
        conn.commit()
        emit('admin_log', {"msg": f"Profile created: User '{uname}' password reference: '{gen_pass}'"})
        sync_admin_dashboard(s['room_id'])
    except Exception:
        emit('admin_log', {"msg": "❌ Creation failure. Username value could be duplicated inside system db."})
    finally:
        conn.close()

@socketio.on('delete_player')
def on_delete(data):
    s = online_sessions.get(request.sid)
    if not s or not s['is_admin']: return
    target = data.get('username')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE username = %s AND room_id = %s", (target, s['room_id']))
        conn.commit()
        emit('admin_log', {"msg": f"Purged player profile context: {target}"})
        sync_admin_dashboard(s['room_id'])
    finally:
        conn.close()

@socketio.on('disconnect')
def on_disconnect():
    if request.sid in online_sessions:
        r = online_sessions[request.sid]['room_id']
        del online_sessions[request.sid]
        sync_admin_dashboard(r)

threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
