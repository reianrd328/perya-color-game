import os
import random
import string
import uuid
import pymysql
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config['SECRET_KEY'] = "perya_super_secret_991223"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

# Root Master Admin Credentials (Access to all 10 servers)
GLOBAL_ADMIN_USER = "superadmin"
GLOBAL_ADMIN_PASS = "superpassword123"

# Memory Matrices (Multi-Server Tenants)
table_bets = {}         # Structure: { room_id: { username: { color: amount } } }
online_users = {}       # Structure: { sid: { username: str, room_id: str, is_admin: bool } }
pull_requests = {}      # Structure: { room_id: [ usernames ] }
pending_withdraws = {}  # Structure: { room_id: [ { id, username, amount } ] }
vouchers = {}           # Structure: { room_id: { token_code: value_int } }

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
        print(f"❌ Database Connectivity Failure: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        return
    try:
        cursor = conn.cursor()
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
        conn.commit()

        # Seed structural default sub-server admin profiles (1 to 10)
        for i in range(1, 11):
            s_name = f"admin" if i == 1 else f"admin{i}"
            s_room = f"Server_{i}"
            s_pass = f"adminpass{i}"
            try:
                cursor.execute(
                    "INSERT INTO users (username, password, coins, is_admin, room_id) VALUES (%s, %s, 0, 1, %s)",
                    (s_name, s_pass, s_room)
                )
                conn.commit()
            except Exception:
                pass # Already seeded
    except Exception as e:
        print(f"Database setup notice: {e}")
    finally:
        cursor.close()
        conn.close()

def update_admin_panels(room_id):
    conn = get_db_connection()
    if not conn: return
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(coins) as total FROM users WHERE room_id = %s AND is_admin = 0", (room_id,))
        res = cursor.fetchone()
        total_coins = int(res['total']) if res and res['total'] else 0

        cursor.execute("SELECT username, password, coins, is_admin FROM users WHERE room_id = %s", (room_id,))
        users_list = cursor.fetchall()

        socketio.emit('admin_dashboard_update', {
            "total_coins": total_coins,
            "users": users_list,
            "pull_requests": pull_requests.get(room_id, []),
            "pending_withdraws": pending_withdraws.get(room_id, [])
        }, to=room_id)
    except Exception as e:
        print(f"Sync error: {e}")
    finally:
        cursor.close()
        conn.close()

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join_game(data):
    username = data.get('username', '').strip()
    password = data.get('password', '')
    room_id = data.get('room_id', 'Server_1').strip()

    if not username or not password:
        return emit('login_failed', {"message": "Empty values loaded."})

    # Rule 1: Validate global master backdoor configuration overrides
    if username == GLOBAL_ADMIN_USER and password == GLOBAL_ADMIN_PASS:
        online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": True}
        join_room(room_id)
        emit('user_status', {"username": username, "is_admin": True, "room_id": room_id, "coins": 0})
        update_admin_panels(room_id)
        return

    conn = get_db_connection()
    if not conn: return emit('login_failed', {"message": "Database disconnected."})

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = %s AND password = %s", (username, password))
        user = cursor.fetchone()

        if not user:
            return emit('login_failed', {"message": "Invalid username or password configuration."})

        # Rule 2: Strict cross-room verification checks
        if user['is_admin'] == 0 and user['room_id'] != room_id:
            return emit('login_failed', {"message": f"Access Denied: Enrolled in {user['room_id'].replace('_',' ')} only."})

        # If a Sub-Server Admin logs in, tie them strictly to their assigned server room
        if user['is_admin'] == 1:
            room_id = user['room_id']

        online_users[request.sid] = {"username": username, "room_id": room_id, "is_admin": bool(user['is_admin'])}
        join_room(room_id)

        emit('user_status', {
            "username": username, 
            "is_admin": bool(user['is_admin']), 
            "room_id": room_id, 
            "coins": user['coins']
        })

        if user['is_admin'] == 1:
            update_admin_panels(room_id)
        else:
            if room_id not in table_bets: table_bets[room_id] = {}
            if username not in table_bets[room_id]: table_bets[room_id][username] = {c: 0 for c in COLORS}
            update_admin_panels(room_id)

    except Exception as e:
        emit('login_failed', {"message": f"Internal Error: {e}"})
    finally:
        conn.close()

@socketio.on('place_bets')
def handle_bets(data):
    user_meta = online_users.get(request.sid)
    if not user_meta or user_meta['is_admin']: return

    room_id = user_meta['room_id']
    username = user_meta['username']
    client_bets = data.get('bets', {})

    total_stake = sum(max(0, int(v)) for v in client_bets.values())

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    user_db = cursor.fetchone()

    if not user_db or user_db['coins'] < total_stake:
        cursor.close()
        conn.close()
        return emit('notification', {"message": "Insufficient coin balance."})

    table_bets[room_id][username] = {c: max(0, int(client_bets.get(c, 0))) for c in COLORS}
    cursor.close()
    conn.close()
    emit('player_log', {"msg": f"Locked structural bets total: {total_stake} coins."})

@socketio.on('request_pull')
def handle_req_pull():
    user_meta = online_users.get(request.sid)
    if not user_meta or user_meta['is_admin']: return

    room_id = user_meta['room_id']
    username = user_meta['username']

    if room_id not in pull_requests: pull_requests[room_id] = []
    if username not in pull_requests[room_id]:
        pull_requests[room_id].append(username)

    update_admin_panels(room_id)
    emit('player_log', {"msg": "Rope pull permission pending admin authorization."})

@socketio.on('resolve_pull_request')
def resolve_pull(data):
    admin_meta = online_users.get(request.sid)
    if not admin_meta or not admin_meta['is_admin']: return

    room_id = admin_meta['room_id']
    target = data.get('target_user')
    action = data.get('action')

    if room_id in pull_requests and target in pull_requests[room_id]:
        pull_requests[room_id].remove(target)
        if action == 'approve':
            socketio.emit('log_update', {"msg": f"🟢 Pull request approved for user: {target}."}, to=room_id)
            execute_dice_roll_sequence(room_id)
        else:
            socketio.emit('log_update', {"msg": f"❌ Pull request rejected for user: {target}."}, to=room_id)
        update_admin_panels(room_id)

@socketio.on('admin_pull_rope')
def admin_pull_rope():
    admin_meta = online_users.get(request.sid)
    if not admin_meta or not admin_meta['is_admin']: return
    execute_dice_roll_sequence(admin_meta['room_id'])

def execute_dice_roll_sequence(room_id):
    # Step 1: Broadcast asynchronous animation trigger signals down to the room channel
    socketio.emit('dice_spin_start', {}, to=room_id)

    # Precise calculation delay loop block
    socketio.sleep(3.0)

    # Step 2: Roll the dice
    res1, res2, res3 = random.choice(COLORS), random.choice(COLORS), random.choice(COLORS)
    rolled_results = [res1, res2, res3]

    conn = get_db_connection()
    cursor = conn.cursor()

    socketio.emit('log_update', {"msg": f"🎲 Winning color metrics generated: {res1} | {res2} | {res3}."}, to=room_id)

    # Step 3: Assess balances across active bet maps
    room_bet_matrix = table_bets.get(room_id, {})
    for player, bets in list(room_bet_matrix.items()):
        total_bet = sum(bets.values())
        if total_bet == 0: continue

        cursor.execute("SELECT coins FROM users WHERE username = %s", (player,))
        p_data = cursor.fetchone()
        if not p_data: continue

        current_balance = p_data['coins']
        winnings = 0

        for color, amount in bets.items():
            if amount <= 0: continue
            matches = rolled_results.count(color)
            if matches > 0:
                # Win: Keep base stake + get paid equivalent matching multiplier rates
                winnings += amount + (amount * matches)
            else:
                # Loss: Deduct staked capital pool from balance
                winnings -= amount

        new_balance = max(0, current_balance + winnings)
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, player))
        conn.commit()

        # Update specific player client frames directly
        for sid, meta in online_users.items():
            if meta['username'] == player:
                socketio.emit('update_balance', {"coins": new_balance}, to=sid)
                status_msg = f"Result: {'🎉 Win! +' if winnings >= 0 else '📉 Loss: '}{winnings} Coins."
                socketio.emit('player_log', {"msg": status_msg}, to=sid)

        # Clear bets after evaluation
        table_bets[room_id][player] = {c: 0 for c in COLORS}

    cursor.close()
    conn.close()

    # Step 4: Disengage rolling frames on client threads
    socketio.emit('dice_spin_stop', {"results": rolled_results}, to=room_id)
    update_admin_panels(room_id)

@socketio.on('request_withdraw')
def handle_withdraw(data):
    user_meta = online_users.get(request.sid)
    if not user_meta or user_meta['is_admin']: return

    room_id = user_meta['room_id']
    username = user_meta['username']
    amount = int(data.get('amount', 0))

    if amount <= 0: return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    ub = cursor.fetchone()
    cursor.close()
    conn.close()

    if not ub or ub['coins'] < amount:
        return emit('notification', {"message": "Withdraw balance exceeds wallet limits."})

    if room_id not in pending_withdraws: pending_withdraws[room_id] = []
    pending_withdraws[room_id].append({"id": str(uuid.uuid4())[:8], "username": username, "amount": amount})

    update_admin_panels(room_id)
    emit('player_log', {"msg": f"Cashout processing ticket registered for {amount} coins."})

@socketio.on('resolve_withdraw_request')
def resolve_withdraw(data):
    admin_meta = online_users.get(request.sid)
    if not admin_meta or not admin_meta['is_admin']: return

    room_id = admin_meta['room_id']
    tx_id = data.get('transfer_id')
    action = data.get('action')

    target_ticket = next((w for w in pending_withdraws.get(room_id, []) if w['id'] == tx_id), None)
    if not target_ticket: return

    pending_withdraws[room_id].remove(target_ticket)

    if action == 'approve':
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT coins FROM users WHERE username = %s", (target_ticket['username'],))
        ub = cursor.fetchone()

        if ub and ub['coins'] >= target_ticket['amount']:
            new_bal = ub['coins'] - target_ticket['amount']
            cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_bal, target_ticket['username']))
            conn.commit()

            socketio.emit('log_update', {"msg": f"✅ Cashout approved: {target_ticket['username']} ({target_ticket['amount']}c)."}, to=room_id)

            for sid, meta in online_users.items():
                if meta['username'] == target_ticket['username']:
                    socketio.emit('update_balance', {"coins": new_bal}, to=sid)
                    socketio.emit('player_log', {"msg": f"Cashout transaction verified: -{target_ticket['amount']}c."}, to=sid)
        cursor.close()
        conn.close()
    else:
         socketio.emit('log_update', {"msg": f"❌ Cashout denied for {target_ticket['username']}."}, to=room_id)

    update_admin_panels(room_id)

@socketio.on('generate_voucher')
def make_voucher(data):
    admin_meta = online_users.get(request.sid)
    if not admin_meta or not admin_meta['is_admin']: return

    room_id = admin_meta['room_id']
    amount = int(data.get('amount', 0))
    if amount <= 0: return

    token = "TOK-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    if room_id not in vouchers: vouchers[room_id] = {}

    vouchers[room_id][token] = amount
    emit('voucher_generated', {"token": token, "amount": amount})

@socketio.on('claim_voucher')
def claim_voucher(data):
    user_meta = online_users.get(request.sid)
    if not user_meta or user_meta['is_admin']: return

    room_id = user_meta['room_id']
    username = user_meta['username']
    token = data.get('token', '').strip()

    if room_id in vouchers and token in vouchers[room_id]:
        val = vouchers[room_id].pop(token)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        ub = cursor.fetchone()

        new_bal = ub['coins'] + val
        cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_bal, username))
        conn.commit()
        cursor.close()
        conn.close()

        emit('update_balance', {"coins": new_bal})
        emit('player_log', {"msg": f"🎟️ Claimed Voucher successfully (+{val} Coins)."})
        update_admin_panels(room_id)
    else:
        emit('notification', {"message": "Invalid voucher or assigned to another room zone mapping."})

@socketio.on('create_player')
def create_player(data):
    admin_meta = online_users.get(request.sid)
    if not admin_meta or not admin_meta['is_admin']: return

    room_id = admin_meta['room_id']
    username = data.get('username', '').strip()
    generated_password = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))

    if not username: return

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password, coins, is_admin, room_id) VALUES (%s, %s, 1000, 0, %s)",
            (username, generated_password, room_id)
        )
        conn.commit()
        socketio.emit('log_update', {"msg": f"👤 Enrolled user profile: {username} | Password: {generated_password}"}, to=room_id)
        update_admin_panels(room_id)
    except Exception:
        emit('notification', {"message": "Profile moniker already exists."})
    finally:
        cursor.close()
        conn.close()

@socketio.on('delete_user_profile')
def delete_user(data):
    admin_meta = online_users.get(request.sid)
    if not admin_meta or not admin_meta['is_admin']: return

    room_id = admin_meta['room_id']
    target = data.get('target_user')

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE username = %s AND room_id = %s AND is_admin = 0", (target, room_id))
    conn.commit()
    cursor.close()
    conn.close()

    socketio.emit('log_update', {"msg": f"🗑️ Purged user: {target} from this server."}, to=room_id)
    update_admin_panels(room_id)

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in online_users:
        room_id = online_users[request.sid]['room_id']
        del online_users[request.sid]
        update_admin_panels(room_id)

init_db()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)


Index.html

<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Multi-Server Perya Game</title>
    <script src="https://cdn.jsdelivr.net/npm/socket.io-client@4.7.5/dist/socket.io.min.js"></script>
    <style>
        :root {
            --bg-color: #1a1a1a;
            --panel-color: #222222;
            --text-color: #ffffff;
            --border-color: #444444;
        }
        .light-theme {
            --bg-color: #f5f6fa;
            --panel-color: #ffffff;
            --text-color: #2f3640;
            --border-color: #dcdde1;
        }
        body { 
            background-color: var(--bg-color); 
            color: var(--text-color); 
            font-family: sans-serif; 
            text-align: center; 
            padding: 20px;
            transition: background 0.3s, color 0.3s;
        }
        .panel { 
            background: var(--panel-color); 
            max-width: 800px; 
            margin: 20px auto; 
            padding: 25px; 
            border-radius: 10px; 
            box-shadow: 0 4px 15px rgba(0,0,0,0.3);
            border: 1px solid var(--border-color);
            text-align: left;
        }
        select, input, button { padding: 10px; margin: 8px 0; font-size: 15px; border-radius: 5px; border: 1px solid var(--border-color); width: 100%; box-sizing: border-box; background: var(--panel-color); color: var(--text-color); }
        button { background: #e1b12c; color: #000; font-weight: bold; cursor: pointer; border: none; transition: 0.2s; }
        button:hover { background: #fbc531; }
        .flex-grid { display: flex; gap: 15px; flex-wrap: wrap; }
        .col { flex: 1; min-width: 250px; }

        /* 3D Dice Layout */
        .dice-container { display: flex; justify-content: center; gap: 30px; margin: 30px auto; min-height: 100px; }
        .cube { width: 80px; height: 80px; position: relative; transform-style: preserve-3d; transition: transform 0.5s ease-out; }
        .face { position: absolute; width: 80px; height: 80px; border: 2px solid #000; border-radius: 12px; opacity: 0.95; }

        /* 3D Face Transforms */
        .f-red    { background: #ff2a2a; transform: rotateY(0deg) translateZ(40px); }
        .f-blue   { background: #2a75ff; transform: rotateY(90deg) translateZ(40px); }
        .f-green  { background: #2aff53; transform: rotateY(180deg) translateZ(40px); }
        .f-yellow { background: #ffeb2a; transform: rotateY(-90deg) translateZ(40px); }
        .f-white  { background: #ffffff; transform: rotateX(90deg) translateZ(40px); }
        .f-pink   { background: #ff2ae2; transform: rotateX(-90deg) translateZ(40px); }

        .spinning { animation: tumble 0.15s infinite linear; }
        @keyframes tumble {
            0% { transform: rotateX(0deg) rotateY(0deg); }
            100% { transform: rotateX(360deg) rotateY(360deg); }
        }

        /* Betting Table Styles */
        .betting-table { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin: 20px 0; }
        .bet-card { padding: 15px; border-radius: 8px; text-align: center; color: #000; font-weight: bold; cursor: pointer; border: 3px solid transparent; }
  
