import os
import random
import string
import pymysql
import threading
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "perya_master_key_secure!")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

# Universal Master Override Configurations
GLOBAL_ADMIN_USER = "superadmin"
GLOBAL_ADMIN_PASS = "superadmin123"

# Memory Architecture Hooks
online_connections = {}  
active_room_bets = {}    
room_pull_requests = {f"Server_{i}": [] for i in range(1, 11)}

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
        print(f"❌ DB Drop Connection Refused: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                coins INT DEFAULT 1000,
                is_admin TINYINT DEFAULT 0,
                room_id VARCHAR(50) DEFAULT 'Server_1'
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vouchers (
                id INT AUTO_INCREMENT PRIMARY KEY,
                code VARCHAR(50) UNIQUE NOT NULL,
                value INT NOT NULL,
                claimed TINYINT DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(255) NOT NULL,
                room_id VARCHAR(50) NOT NULL,
                amount INT NOT NULL,
                type VARCHAR(50) NOT NULL,
                status VARCHAR(50) DEFAULT 'PENDING'
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"❌ Migration Error: {e}")
    finally:
        conn.close()

def sync_admin_station(room_id):
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, username, amount FROM transactions WHERE room_id = %s AND type = 'WITHDRAW' AND status = 'PENDING'", (room_id,))
        withdraws = cursor.fetchall()
        socketio.emit('admin_dashboard_update', {
            "pull_requests": room_pull_requests.get(room_id, []),
            "withdraws": withdraws
        }, to=room_id)
    finally:
        conn.close()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join_game(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')
    room_id = data.get('room_id', 'Server_1').strip()

    if username == GLOBAL_ADMIN_USER and password == GLOBAL_ADMIN_PASS:
        online_connections[request.sid] = {"username": username, "room_id": room_id, "is_admin": True}
        join_room(room_id)
        emit('user_status', {"username": username, "is_admin": True, "room_id": room_id})
        sync_admin_station(room_id)
        emit('admin_log_msg', {"text": f"👑 Global Super Admin attached directly to {room_id}"})
        return

    conn = get_db_connection()
    if not conn: return emit('login_failed', {"message": "Database Engine Offline."})
    
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()
        
        if not user:
            return emit('login_failed', {"message": "Invalid credentials provided."})
            
        if user['is_admin'] == 0 and user['room_id'] != room_id:
            return emit('login_failed', {"message": f"Locked Out! Account registered to {user['room_id']} explicitly."})
            
        online_connections[request.sid] = {"username": username, "room_id": room_id, "is_admin": bool(user['is_admin'])}
        join_room(room_id)
        
        emit('user_status', {"username": username, "is_admin": bool(user['is_admin']), "room_id": room_id, "coins": user['coins']})
        
        if user['is_admin']:
            sync_admin_station(room_id)
            emit('admin_log_msg', {"text": f"🔐 Room Admin logged into {room_id}"})
        else:
            emit('log_msg', {"text": f"Connected to {room_id} successfully."})
    finally:
        conn.close()

@socketio.on('place_bet')
def handle_place_bet(data):
    user_meta = online_connections.get(request.sid)
    if not user_meta: return
    if user_meta['room_id'] not in active_room_bets:
        active_room_bets[user_meta['room_id']] = {}
    active_room_bets[user_meta['room_id']][user_meta['username']] = data.get('bets', {})

@socketio.on('request_pull')
def handle_request_pull():
    meta = online_connections.get(request.sid)
    if not meta or meta['is_admin']: return
    room = meta['room_id']
    user = meta['username']
    
    if user not in room_pull_requests[room]:
        room_pull_requests[room].append(user)
    
    emit('log_msg', {"text": "Pull authorization requested. Awaiting administrator approval."})
    socketio.emit('admin_log_msg', {"text": f"🙋‍♂️ {user} requested rope control."}, to=room)
    sync_admin_station(room)

@socketio.on('handle_pull_request')
def handle_pull_req(data):
    meta = online_connections.get(request.sid)
    if not meta or not meta['is_admin']: return
    room = meta['room_id']
    target = data.get('username')
    
    if target in room_pull_requests[room]:
        room_pull_requests[room].remove(target)
        
    if data.get('action') == 'approve':
        for sid, u_data in online_connections.items():
            if u_data['username'] == target and u_data['room_id'] == room:
                socketio.emit('pull_allowed', {}, to=sid)
                socketio.emit('admin_log_msg', {"text": f"✅ Approved {target}'s pull sequence."}, to=room)
                break
    else:
        socketio.emit('admin_log_msg', {"text": f"❌ Denied {target}'s pull request."}, to=room)
    sync_admin_station(room)

@socketio.on('execute_pull')
def handle_execute_pull():
    meta = online_connections.get(request.sid)
    if not meta: return
    run_dice_engine(meta['room_id'], meta['username'])

@socketio.on('admin_force_pull')
def handle_admin_force():
    meta = online_connections.get(request.sid)
    if not meta or not meta['is_admin']: return
    run_dice_engine(meta['room_id'], "ROOM ADMINISTRATOR")

def run_dice_engine(room_id, triggered_by):
    socketio.emit('dice_rolling_start', {}, to=room_id)
    
    # Run computing engine physics results
    res_dice = [random.choice(COLORS) for _ in range(3)]
    
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        room_wagers = active_room_bets.get(room_id, {})
        
        for player, bets in list(room_wagers.items()):
            cursor.execute("SELECT coins FROM users WHERE username = %s", (player,))
            user_row = cursor.fetchone()
            if not user_row: continue
            
            curr_balance = user_row['coins']
            net_gain_loss = 0
            
            for color, amount in bets.items():
                if amount <= 0: continue
                match_count = res_dice.count(color)
                if match_count > 0:
                    net_gain_loss += (amount * match_count)
                else:
                    net_gain_loss -= amount
            
            new_bal = curr_balance + net_gain_loss
            cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_bal, player))
            
            # Identify individual connection instances
            for sid, u_meta in online_connections.items():
                if u_meta['username'] == player:
                    socketio.emit('balance_update', {"coins": new_bal}, to=sid)
                    socketio.emit('log_msg', {"text": f"🎲 Landed: {res_dice}. Return outcome: {'💰 +' if net_gain_loss>=0 else '📉 '}{net_gain_loss} coins."}, to=sid)
        
        conn.commit()
    finally:
        conn.close()
        
    socketio.emit('roll_result', {"dice": res_dice}, to=room_id)
    socketio.emit('admin_log_msg', {"text": f"🎲 Game loop computed by {triggered_by}. Result: {res_dice}"}, to=room_id)
    if room_id in active_room_bets:
        active_room_bets[room_id] = {}

@socketio.on('create_voucher')
def handle_create_voucher(data):
    meta = online_connections.get(request.sid)
    if not meta or not meta['is_admin']: return
    val = data.get('value', 0)
    if val <= 0: return
    
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO vouchers (code, value) VALUES (%s, %s)", (code, val))
        conn.commit()
        emit('admin_log_msg', {"text": f"🎟️ Voucher Minted: Code [{code}] worth 💰{val} Coins."})
    finally:
        conn.close()

@socketio.on('redeem_voucher')
def handle_redeem_voucher(data):
    meta = online_connections.get(request.sid)
    if not meta: return
    code = data.get('code', '').strip()
    
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM vouchers WHERE code = %s AND claimed = 0", (code,))
        v = cursor.fetchone()
        if not v:
            return emit('log_msg', {"text": "❌ Voucher code invalid or already claimed."})
            
        cursor.execute("UPDATE vouchers SET claimed = 1 WHERE id = %s", (v['id'],))
        cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (v['value'], meta['username']))
        cursor.execute("SELECT coins FROM users WHERE username = %s", (meta['username'],))
        updated_coins = cursor.fetchone()['coins']
        conn.commit()
        
        emit('balance_update', {"coins": updated_coins})
        emit('log_msg', {"text": f"🎉 Successfully claimed code voucher worth 💰{v['value']} Coins!"})
    finally:
        conn.close()

@socketio.on('request_withdraw')
def handle_req_withdraw(data):
    meta = online_connections.get(request.sid)
    if not meta or meta['is_admin']: return
    amt = data.get('amount', 0)
    if amt <= 0: return
    
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT coins FROM users WHERE username = %s", (meta['username'],))
        bal = cursor.fetchone()['coins']
        if bal < amt:
            return emit('log_msg', {"text": "❌ Insufficient balance for withdrawal value."})
            
        cursor.execute("INSERT INTO transactions (username, room_id, amount, type) VALUES (%s, %s, %s, 'WITHDRAW')", (meta['username'], meta['room_id'], amt))
        conn.commit()
        emit('log_msg', {"text": f"⏳ Withdrawal request for {amt} coins logged for Admin approval."})
        socketio.emit('admin_log_msg', {"text": f"💰 {meta['username']} requested token withdrawal of {amt}."}, to=meta['room_id'])
        sync_admin_station(meta['room_id'])
    finally:
        conn.close()

@socketio.on('handle_withdraw_request')
def handle_with_req(data):
    meta = online_connections.get(request.sid)
    if not meta or not meta['is_admin']: return
    t_id = data.get('id')
    action = data.get('action')
    
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transactions WHERE id = %s", (t_id,))
        t = cursor.fetchone()
        if not t or t['status'] != 'PENDING': return
        
        if action == 'approve':
            cursor.execute("SELECT coins FROM users WHERE username = %s", (t['username'],))
            p_bal = cursor.fetchone()['coins']
            if p_bal >= t['amount']:
                cursor.execute("UPDATE users SET coins = coins - %s WHERE username = %s", (t['amount'], t['username']))
                cursor.execute("UPDATE transactions SET status = 'APPROVED' WHERE id = %s", (t_id,))
                socketio.emit('admin_log_msg', {"text": f"✅ Approved withdrawal of {t['amount']} to {t['username']}."}, to=meta['room_id'])
                
                # Push immediate live balance sync directly to target user if currently connected online
                for sid, u_meta in online_connections.items():
                    if u_meta['username'] == t['username']:
                        cursor.execute("SELECT coins FROM users WHERE username = %s", (t['username'],))
                        socketio.emit('balance_update', {"coins": cursor.fetchone()['coins']}, to=sid)
                        socketio.emit('log_msg', {"text": f"✅ Your withdrawal of {t['amount']} has been fully disbursed!"}, to=sid)
            else:
                cursor.execute("UPDATE transactions SET status = 'INSUFFICIENT_FUNDS' WHERE id = %s", (t_id,))
        else:
            cursor.execute("UPDATE transactions SET status = 'DENIED' WHERE id = %s", (t_id,))
            socketio.emit('admin_log_msg', {"text": f"❌ Rejected withdrawal of {t['amount']} to {t['username']}."}, to=meta['room_id'])
            for sid, u_meta in online_connections.items():
                if u_meta['username'] == t['username']:
                    socketio.emit('log_msg', {"text": f"❌ Your withdrawal request of {t['amount']} was rejected by the admin."}, to=sid)
                    
        conn.commit()
        sync_admin_station(meta['room_id'])
    finally:
        conn.close()

@socketio.on('create_player')
def handle_create_player(data):
    meta = online_connections.get(request.sid)
    if not meta or not meta['is_admin']: return
    user = data.get('username', '').strip()
    generated_pass = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    
    if not user: return
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (username, password, coins, is_admin, room_id) VALUES (%s, %s, 1000, 0, %s)", (user, generated_pass, meta['room_id']))
        conn.commit()
        emit('admin_log_msg', {"text": f"👤 Profile Built: User [{user}] | Password: [{generated_pass}] linked to {meta['room_id']}"})
    except Exception as err:
        emit('admin_log_msg', {"text": f"⚠️ Profile Creation Blocked: {err}"})
    finally:
        conn.close()

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_connections:
        room = online_connections[request.sid]['room_id']
        del online_connections[request.sid]
        sync_admin_station(room)

threading.Thread(target=init_db, daemon=True).start()

if __name__ == '__main__':
    socketio.run(app, debug=True)
