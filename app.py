import os
import random
import string
import pymysql
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "super_secure_perya_key_2026_x!!")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

# Multi-Tenant Memory Matrix
table_bets = {f"Server_{i}": {} for i in range(1, 11)}
table_bets["ALL"] = {} # Support Super Admin space
online_users = {}  # Map: sid -> {username, room_id, is_admin, is_super_admin}
pull_requests = {f"Server_{i}": [] for i in range(1, 11)}
pull_requests["ALL"] = []
withdraw_requests = {f"Server_{i}": [] for i in range(1, 11)}
withdraw_requests["ALL"] = []

def get_db_connection():
    try:
        return pymysql.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 3306)),
            user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASSWORD", ""),
            database=os.environ.get("DB_NAME", "perya_color_game"),
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            ssl={"ssl": {}} # 🌟 Required for Cloud Aiven Connection
        )
    except Exception as e:
        print(f"❌ DB Failure: {e}")
        return None

def write_log(room_id, username, action_type, details):
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO game_logs (room_id, username, action_type, details) VALUES (%s, %s, %s, %s)",
                    (room_id, username, action_type, details)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Log Engine Failure: {e}")

def update_admin_and_user_panels(room_id):
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            if room_id == 'ALL':
                cursor.execute("SELECT username, coins, password, is_admin, is_super_admin, room_id FROM users")
                room_users = cursor.fetchall()
                cursor.execute("SELECT * FROM vouchers WHERE is_used = 0")
                active_vouchers = cursor.fetchall()
                cursor.execute("SELECT * FROM game_logs ORDER BY id DESC LIMIT 15")
                logs = cursor.fetchall()
            else:
                cursor.execute("SELECT username, coins, password, is_admin, is_super_admin, room_id FROM users WHERE room_id = %s OR is_super_admin = 1", (room_id,))
                room_users = cursor.fetchall()
                cursor.execute("SELECT * FROM vouchers WHERE room_id = %s AND is_used = 0", (room_id,))
                active_vouchers = cursor.fetchall()
                cursor.execute("SELECT * FROM game_logs WHERE room_id = %s ORDER BY id DESC LIMIT 15", (room_id,))
                logs = cursor.fetchall()
                
        conn.close()
    except Exception as e:
        print(f"Sync Engine Exception: {e}")
        return

    # Map online status dynamically
    active_sids = list(online_users.values())
    for u in room_users:
        u['online'] = any(x['username'] == u['username'] for x in active_sids)

    # Process live tracking statistics for active table players
    active_players = []
    room_bets = table_bets.get(room_id, {})
    for sid, data in online_users.items():
        if (data['room_id'] == room_id or room_id == 'ALL') and not data['is_admin']:
            name = data['username']
            db_user = next((x for x in room_users if x['username'] == name), {"coins": 0})
            staked = sum(room_bets.get(name, {}).values())
            active_players.append({
                "username": name,
                "coins": db_user['coins'],
                "staked": staked,
                "total": db_user['coins'] + staked
            })

    socketio.emit('admin_dashboard_update', {
        "users": room_users,
        "vouchers": active_vouchers,
        "players": active_players,
        "pull_requests": pull_requests.get(room_id, []),
        "withdraw_requests": withdraw_requests.get(room_id, []),
        "logs": logs
    }, to=room_id)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join_game(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')
    requested_room = data.get('room_id', 'Server_1').strip()

    if not username or not password:
        return emit('login_failed', {"message": "Credentials cannot be empty."})

    conn = get_db_connection()
    if not conn: 
        return emit('login_failed', {"message": "Database Engine Offline. Check SSL/Credentials."})

    user = None
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
            user = cursor.fetchone()
        conn.close()
    except Exception as e:
        return emit('login_failed', {"message": f"Query Error: {e}"})

    if not user:
        return emit('login_failed', {"message": "Invalid Username or Password."})

    is_super = bool(user['is_super_admin'])
    assigned_room = requested_room if is_super else user['room_id']

    if not is_super and user['room_id'] != requested_room:
        return emit('login_failed', {"message": f"❌ Access Denied: Bound to {user['room_id']}."})

    online_users[request.sid] = {
        "username": username,
        "room_id": assigned_room,
        "is_admin": bool(user['is_admin']),
        "is_super_admin": is_super
    }
    
    join_room(assigned_room)
    if assigned_room not in table_bets:
        table_bets[assigned_room] = {}
    if username not in table_bets[assigned_room]:
        table_bets[assigned_room][username] = {c: 0 for c in COLORS}

    emit('user_status', {
        "username": username,
        "coins": user['coins'],
        "is_admin": bool(user['is_admin']),
        "is_super_admin": is_super,
        "room_id": assigned_room
    })

    write_log(assigned_room, username, "Login", f"User joined session room {assigned_room}")
    update_admin_and_user_panels(assigned_room)

@socketio.on('place_bet')
def handle_place_bet(data):
    user_ctx = online_users.get(request.sid)
    if not user_ctx or user_ctx['is_admin']: return

    color = data.get('color')
    amount = int(data.get('amount', 0))
    room_id = user_ctx['room_id']
    username = user_ctx['username']

    if color not in COLORS or amount <= 0: return

    conn = get_db_connection()
    if not conn: return
    
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
            res = cursor.fetchone()
            if res and res['coins'] >= amount:
                cursor.execute("UPDATE users SET coins = coins - %s WHERE username = %s", (amount, username))
                conn.commit()
                table_bets[room_id][username][color] += amount
                emit('bet_success', {"coins": res['coins'] - amount, "bets": table_bets[room_id][username]})
                write_log(room_id, username, "Bet Staked", f"Staked {amount} on {color}")
            else:
                emit('bet_failed', {"message": "Insufficient balance allocation."})
        conn.close()
    except Exception as e:
        print(f"Betting runtime exception: {e}")
    update_admin_and_user_panels(room_id)

@socketio.on('request_pull')
def handle_request_pull():
    user_ctx = online_users.get(request.sid)
    if not user_ctx: return
    room_id, username = user_ctx['room_id'], user_ctx['username']

    if username not in pull_requests[room_id]:
        pull_requests[room_id].append(username)
    write_log(room_id, username, "Rope Request", "Player requested permission to pull rope.")
    update_admin_and_user_panels(room_id)

@socketio.on('admin_rope_action')
def handle_admin_rope_action(data):
    admin_ctx = online_users.get(request.sid)
    if not admin_ctx or not admin_ctx['is_admin']: return
    
    room_id = admin_ctx['room_id']
    target_user = data.get('username')
    approved = data.get('approved', False)

    if target_user in pull_requests[room_id]:
        pull_requests[room_id].remove(target_user)
        
    if approved:
        final_dice = [random.choice(COLORS) for _ in range(3)]
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cursor:
                    for player, bets in list(table_bets[room_id].items()):
                        p_payout = 0
                        p_return_stake = 0
                        for color, amt in bets.items():
                            if amt > 0:
                                matches = final_dice.count(color)
                                if matches > 0:
                                    p_payout += (amt * matches)
                                    p_return_stake += amt
                        
                        total_refund = p_payout + p_return_stake
                        if total_refund > 0:
                            cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (total_refund, player))
                        
                        table_bets[room_id][player] = {c: 0 for c in COLORS}
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"Payout Engine Crash: {e}")

        socketio.emit('rope_spin_broadcast', {"dice": final_dice}, to=room_id)
        write_log(room_id, admin_ctx['username'], "Rope Pull Execution", f"Rope rolled by authorization. System landing: {final_dice}")
    else:
        write_log(room_id, admin_ctx['username'], "Rope Disapproved", f"Rope execution requested by {target_user} rejected.")
        
    update_admin_and_user_panels(room_id)

@socketio.on('request_withdraw')
def handle_request_withdraw(data):
    user_ctx = online_users.get(request.sid)
    if not user_ctx: return
    amount = int(data.get('amount', 0))
    room_id, username = user_ctx['room_id'], user_ctx['username']

    if amount <= 0: return
    
    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
            res = cursor.fetchone()
            if res and res['coins'] >= amount:
                cursor.execute("UPDATE users SET coins = coins - %s WHERE username = %s", (amount, username))
                conn.commit()
                withdraw_requests[room_id].append({"username": username, "amount": amount})
                write_log(room_id, username, "Withdraw Pending", f"Requested cashout of {amount} coins")
            else:
                emit('withdraw_failed', {"message": "Insufficient coins to back withdrawal allocation request."})
        conn.close()
    except Exception as e:
        print(f"Withdrawal runtime exception: {e}")
    update_admin_and_user_panels(room_id)

@socketio.on('admin_withdraw_action')
def handle_admin_withdraw_action(data):
    admin_ctx = online_users.get(request.sid)
    if not admin_ctx or not admin_ctx['is_admin']: return
    room_id = admin_ctx['room_id']
    target_user = data.get('username')
    amount = int(data.get('amount', 0))
    approved = data.get('approved', False)

    match = next((x for x in withdraw_requests[room_id] if x['username'] == target_user and x['amount'] == amount), None)
    if match:
        withdraw_requests[room_id].remove(match)
        if not approved:
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cursor:
                    cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (amount, target_user))
                conn.commit()
                conn.close()
            write_log(room_id, target_user, "Withdraw Rejected", f"Disapproved cashout request for {amount} coins. Reverted balance.")
        else:
            write_log(room_id, target_user, "Withdraw Approved", f"Approved settlement out for {amount} coins.")
    update_admin_and_user_panels(room_id)

@socketio.on('create_voucher')
def handle_create_voucher(data):
    admin_ctx = online_users.get(request.sid)
    if not admin_ctx or not admin_ctx['is_admin']: return
    room_id = admin_ctx['room_id']
    amount = int(data.get('amount', 0))
    if amount <= 0: return

    code = "PERYA-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))
    conn = get_db_connection()
    if conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO vouchers (code, amount, room_id) VALUES (%s, %s, %s)", (code, amount, room_id))
        conn.commit()
        conn.close()
        write_log(room_id, admin_ctx['username'], "Voucher Created", f"Generated voucher code {code} for {amount} coins")
    update_admin_and_user_panels(room_id)

@socketio.on('redeem_voucher')
def handle_redeem_voucher(data):
    user_ctx = online_users.get(request.sid)
    if not user_ctx: return
    code = data.get('code', '').strip()
    room_id, username = user_ctx['room_id'], user_ctx['username']

    conn = get_db_connection()
    if not conn: return
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM vouchers WHERE code = %s AND (room_id = %s OR room_id = 'ALL') AND is_used = 0", (code, room_id))
            v = cursor.fetchone()
            if v:
                cursor.execute("UPDATE vouchers SET is_used = 1, used_by = %s WHERE id = %s", (username, v['id']))
                cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (v['amount'], username))
                conn.commit()
                emit('voucher_redeemed', {"message": f"Successfully added {v['amount']} coins!", "coins": v['amount']})
                write_log(room_id, username, "Redeemed Voucher", f"Claimed voucher validation key {code} (+{v['amount']})")
            else:
                emit('voucher_failed', {"message": "Invalid code context or room mismatched deployment."})
        conn.close()
    except Exception as e:
        print(f"Voucher subsystem handling fault: {e}")
    update_admin_and_user_panels(room_id)

@socketio.on('admin_create_user')
def handle_admin_create_user(data):
    admin_ctx = online_users.get(request.sid)
    if not admin_ctx or not admin_ctx['is_admin']: return
    
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    make_admin = int(data.get('is_admin', 0))
    room_id = admin_ctx['room_id']

    if not username or not password: return

    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cursor:
                # 🌟 FIX: Explicitly pass '0' for is_super_admin to ensure proper login evaluation
                cursor.execute(
                    "INSERT INTO users (username, password, coins, is_admin, is_super_admin, room_id) VALUES (%s, %s, 0, %s, 0, %s)",
                    (username, password, make_admin, room_id)
                )
            conn.commit()
            conn.close()
            write_log(room_id, admin_ctx['username'], "User Setup", f"Enrolled profile account: {username}")
        except Exception as e:
            print(f"User creation failed: {e}")
            emit('admin_error', {"message": "Username is already registered globally."})
    update_admin_and_user_panels(room_id)

@socketio.on('admin_delete_user')
def handle_admin_delete_user(data):
    admin_ctx = online_users.get(request.sid)
    if not admin_ctx or not admin_ctx['is_admin']: return
    target = data.get('username')
    room_id = admin_ctx['room_id']

    conn = get_db_connection()
    if conn:
        with conn.cursor() as cursor:
            if room_id == 'ALL':
                cursor.execute("DELETE FROM users WHERE username = %s AND is_super_admin = 0", (target,))
            else:
                cursor.execute("DELETE FROM users WHERE username = %s AND room_id = %s AND is_super_admin = 0", (target, room_id))
        conn.commit()
        conn.close()
        write_log(room_id, admin_ctx['username'], "Account Pruned", f"Deleted account record: {target}")
    update_admin_and_user_panels(room_id)

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_and_user_panels(room_id)

@app.route('/force-admin-fix')
def force_admin_fix():
    conn = get_db_connection()
    if not conn:
        return "❌ Database connection failed."
    try:
        with conn.cursor() as cursor:
            # 1. 🛠️ Structurally inject the missing column into your database schema
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN is_super_admin TINYINT(1) DEFAULT 0")
                conn.commit()
            except Exception as schema_e:
                # If it already exists or fails silently, log it and keep going
                print(f"Schema notice: {schema_e}")

            # 2. 🔐 Set the admin profile password and permissions
            cursor.execute("""
                UPDATE users 
                SET password = 'SuperPerya2026!', is_super_admin = 1, is_admin = 1, room_id = 'ALL' 
                WHERE username = 'admin'
            """)
            conn.commit()
        conn.close()
        return "🎉 Success! The missing 'is_super_admin' column has been added to your Aiven Database, and the 'admin' account is now fully upgraded to Global Super Admin. Pass: SuperPerya2026!"
    except Exception as e:
        return f"❌ An error occurred during database modification: {e}"

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
