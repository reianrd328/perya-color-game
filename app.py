

from gevent import monkey
monkey.patch_all()  # Crucial: This MUST be the absolute first thing in the file!

import os
import pymysql
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

# Connect to Aiven MySQL using PyMySQL
# Change this inside your function if it recreates a connection:
db = pymysql.connect(
    host="mysql-1dbccf57-dropfarm341-f09c.e.aivencloud.com",
    user="avnadmin",
    password="AVNS_-suhnyilR-ApxD6Df54",  # Paste your real Aiven password here
    database="defaultdb",
    port=27671,
    cursorclass=pymysql.cursors.DictCursor,
    ssl={'ssl': {}}  # <-- ADD THIS CRUCIAL LINE HERE!
)
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
# Crucial: Ensure this line exists right below your imports/db config!

# ... rest of your code down to line 47 where app = Flask(__name__) runs

def _load_dotenv(path=".env"):
    """Load KEY=VALUE lines from a local .env file into the environment so
    secrets stay out of source control. Real env vars take precedence."""
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
# Engine uses eventlet for asynchronous background execution tasks
socketio = SocketIO(app, cors_allowed_origins="*")

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

# Admin login — set these in your .env / host environment, never in source.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# Database connection — credentials come from the environment / .env file.
def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "perya_color_game"),
    )

# Ensure the voucher-codes table exists and the users table has the
# password / active columns needed for admin-controlled player logins.
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS codes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(20) UNIQUE NOT NULL,
            amount INT NOT NULL,
            redeemed TINYINT(1) NOT NULL DEFAULT 0,
            redeemed_by VARCHAR(50) DEFAULT NULL
        )
    """)

    # Create the users table if it doesn't exist yet (fresh databases, e.g.
    # a new cloud host, won't have it). On an existing install this is a no-op
    # and the ALTERs below backfill any missing columns.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            coins INT NOT NULL DEFAULT 0,
            password VARCHAR(50) DEFAULT NULL,
            active TINYINT(1) NOT NULL DEFAULT 1,
            is_admin TINYINT(1) NOT NULL DEFAULT 0,
            total_earned INT NOT NULL DEFAULT 0,
            total_withdrawn INT NOT NULL DEFAULT 0
        )
    """)

    # Backfill any columns missing from a pre-existing users table.
    cursor.execute("SHOW COLUMNS FROM users")
    existing = {row[0] for row in cursor.fetchall()}
    if 'coins' not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN coins INT NOT NULL DEFAULT 0")
    if 'password' not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN password VARCHAR(50) DEFAULT NULL")
    if 'active' not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN active TINYINT(1) NOT NULL DEFAULT 1")
    if 'is_admin' not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN is_admin TINYINT(1) NOT NULL DEFAULT 0")
    if 'total_earned' not in existing:
        # Lifetime coins won from rolls (the winnings, on top of returned stake)
        cursor.execute("ALTER TABLE users ADD COLUMN total_earned INT NOT NULL DEFAULT 0")
    if 'total_withdrawn' not in existing:
        # Lifetime coins the player has cashed out via approved withdrawals
        cursor.execute("ALTER TABLE users ADD COLUMN total_withdrawn INT NOT NULL DEFAULT 0")

    conn.commit()
    cursor.close()
    conn.close()

# Track active live player bets in-memory per round
# Format: { username: { "red": 10, "blue": 0... } }
active_bets = {}

# Live session tracking so the admin can see and kick connected players.
online_players = {}  # username -> socket session id (sid)
admin_sids = set()   # sids that have authenticated as the admin

# Usernames of players who have asked the admin for permission to roll
pending_roll_requests = set()

# Pending cash-out requests awaiting admin approval: username -> amount
pending_withdrawals = {}

# True only while a roll is being processed, so players can't clear/refund
# bets that the roll is about to pay out (which would double-credit them).
roll_in_progress = False

def is_admin_request():
    """Admin powers are tied to an authenticated socket session, not a
    username string, so they can't be spoofed by a normal player."""
    return request.sid in admin_sids

def broadcast_player_list():
    """Push the current roster of logged-in players, with their live coin
    balances, to every admin."""
    players = [u for u in online_players if u != ADMIN_USERNAME]

    balances = {}
    if players:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Build a "username IN (%s, %s, ...)" lookup for the online players
        placeholders = ", ".join(["%s"] * len(players))
        cursor.execute(
            f"SELECT username, coins FROM users WHERE username IN ({placeholders})",
            tuple(players),
        )
        balances = {row['username']: row['coins'] for row in cursor.fetchall()}
        cursor.close()
        conn.close()

    # Each player's true holdings = wallet balance + coins staked this round.
    roster = []
    for u in players:
        wallet = balances.get(u, 0)
        staked = sum(active_bets.get(u, {}).values())
        roster.append({
            'username': u,
            'coins': wallet,
            'staked': staked,
            'total': wallet + staked,
        })
    for sid in admin_sids:
        socketio.emit('player_list', {'players': roster}, to=sid)

def broadcast_user_list():
    """Push the full list of created user accounts (online or not) to every
    admin, so they can manage logins and see who exists."""
    if not admin_sids:
        return
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT username, password, coins, active, is_admin, total_earned, total_withdrawn FROM users ORDER BY is_admin DESC, username"
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    online = set(online_players)
    users = [{
        'username': r['username'],
        'password': r['password'],
        'coins': r['coins'],
        'active': bool(r['active']),
        'is_admin': bool(r['is_admin']),
        'total_earned': r['total_earned'],
        'total_withdrawn': r['total_withdrawn'],
        'online': r['username'] in online,
    } for r in rows]

    # Total coins in circulation = every wallet balance plus whatever is
    # currently staked on the table (staked coins are deducted from wallets
    # but not yet paid out, so they'd otherwise be uncounted).
    staked_total = sum(sum(b.values()) for b in active_bets.values())
    total_coins = sum(r['coins'] for r in rows) + staked_total

    for sid in admin_sids:
        socketio.emit('user_list', {'users': users, 'total_coins': total_coins}, to=sid)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join(data):
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username:
        emit('login_failed', {'message': "Enter a username."})
        return

    # The bootstrap super-admin authenticates against the hardcoded creds.
    # This account always works so you can never lock yourself out.
    if username == ADMIN_USERNAME:
        if password != ADMIN_PASSWORD:
            emit('login_failed', {'message': "Wrong admin password."})
            return
        admin_sids.add(request.sid)
        online_players[username] = request.sid
        emit('user_status', {'username': username, 'coins': 0, 'is_admin': True})
        broadcast_player_list()
        broadcast_user_list()
        return

    # Everyone else must use an account the admin created for them.
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user or not user['password']:
        emit('login_failed', {'message': "No such account. Ask the admin for a login."})
        return
    if not user['active']:
        emit('login_failed', {'message': "This account has been terminated by the admin."})
        return
    if password != user['password']:
        emit('login_failed', {'message': "Wrong password."})
        return

    online_players[username] = request.sid

    # Accounts flagged is_admin get the admin console; everyone else plays.
    if user.get('is_admin'):
        admin_sids.add(request.sid)
        emit('user_status', {'username': username, 'coins': user['coins'], 'is_admin': True})
        broadcast_player_list()
        broadcast_user_list()
        return

    active_bets[username] = {color: 0 for color in COLORS}
    emit('user_status', {
        'username': username,
        'coins': user['coins'],
        'is_admin': False,
        'total_earned': user.get('total_earned', 0),
        'total_withdrawn': user.get('total_withdrawn', 0),
    })
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('disconnect')
def handle_disconnect():
    # Drop the session from our live rosters and refresh the admin view.
    sid = request.sid
    admin_sids.discard(sid)
    gone = [u for u, s in online_players.items() if s == sid]
    for u in gone:
        online_players.pop(u, None)
        pending_withdrawals.pop(u, None)
    if gone:
        broadcast_player_list()
        broadcast_user_list()

@socketio.on('place_bet')
def handle_bet(data):
    username = data.get('username')
    color = data.get('color')
    amount = int(data.get('amount', 0))

    # Reject obviously invalid stakes before touching the database
    if not username or color not in COLORS or amount <= 0:
        emit('bet_rejected', {'message': "Invalid bet."})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        emit('bet_rejected', {'message': "Account not found."})
        return

    if user['coins'] < amount:
        cursor.close()
        conn.close()
        emit('bet_rejected', {
            'message': "Not enough coins for that bet.",
            'coins': user['coins'],
        })
        return

    # Deduct temporary betting stake out of safe database wallet balance
    new_balance = user['coins'] - amount
    cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
    conn.commit()
    cursor.close()
    conn.close()

    # A live session may not have a bet tracker yet (e.g. reconnects), so
    # make sure one exists before recording the stake.
    table = active_bets.setdefault(username, {c: 0 for c in COLORS})
    table[color] += amount

    # Send the authoritative per-color total back so the board renders the
    # server's truth instead of an optimistic client guess.
    emit('bet_placed', {
        'username': username,
        'color': color,
        'color_total': table[color],
        'coins': new_balance,
    })
    # Broadcast total system table-bets to make it feel alive
    emit('table_update', {'message': f"{username} placed {amount} on {color.upper()}"}, broadcast=True)
    # Reflect the player's new balance on the admin roster
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('clear_bets')
def handle_clear_bets(data):
    """Take back all of a player's bets for the current round and refund the
    staked coins to their wallet."""
    username = data.get('username')
    if not username:
        emit('bet_rejected', {'message': "Log in before clearing bets."})
        return
    # Don't allow a refund once a roll is paying out, or we'd double-credit.
    if roll_in_progress:
        emit('bet_rejected', {'message': "Can't clear bets while the dice are rolling."})
        return

    table = active_bets.get(username)
    refund = sum(table.values()) if table else 0
    if refund <= 0:
        # Nothing staked — still tell the client so the board resets cleanly.
        emit('bets_cleared', {'username': username, 'refunded': 0})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (refund, username))
    conn.commit()
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    new_balance = cursor.fetchone()['coins']
    cursor.close()
    conn.close()

    active_bets[username] = {color: 0 for color in COLORS}

    emit('bets_cleared', {
        'username': username,
        'refunded': refund,
        'coins': new_balance,
    })
    emit('table_update',
         {'message': f"{username} cleared their bets (+{refund} refunded)"},
         broadcast=True)
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('create_player')
def handle_create_player(data):
    # Admin generates a username + password login for a new player (or, when
    # make_admin is set, another admin account).
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can create logins."})
        return

    username = (data.get('username') or '').strip()
    coins = int(data.get('coins', 1000))
    make_admin = bool(data.get('is_admin'))
    if not username:
        emit('admin_error', {'message': "Enter a username for the new account."})
        return
    if username == ADMIN_USERNAME:
        emit('admin_error', {'message': "That username is reserved."})
        return

    # Auto-generate a short password for the player
    chars = string.ascii_uppercase + string.digits
    password = "".join(random.choice(chars) for _ in range(6))

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
        if cursor.fetchone():
            cursor.close()
            conn.close()
            emit('admin_error', {'message': f"Username '{username}' already exists."})
            return

        cursor.execute(
            "INSERT INTO users (username, password, coins, active, is_admin) VALUES (%s, %s, %s, 1, %s)",
            (username, password, coins, 1 if make_admin else 0)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        # Surface the real DB error to the admin instead of failing silently.
        print(f"create_player DB error: {e}")
        emit('admin_error', {'message': f"Database error creating player: {e}"})
        return

    emit('player_created', {
        'username': username,
        'password': password,
        'coins': coins,
        'is_admin': make_admin,
    })
    # Refresh the persistent "all users" list shown to every admin
    broadcast_user_list()

@socketio.on('terminate_player')
def handle_terminate_player(data):
    # Admin kicks a player offline AND bans the account from logging back in.
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can terminate players."})
        return

    target = (data.get('username') or '').strip()
    if not target or target == ADMIN_USERNAME:
        emit('admin_error', {'message': "Invalid player to terminate."})
        return

    # Ban: disable the account so the credentials no longer work
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET active = 0 WHERE username = %s", (target,))
    conn.commit()
    cursor.close()
    conn.close()

    # Kick: force the user's live session offline if they're connected
    target_sid = online_players.pop(target, None)
    active_bets.pop(target, None)
    pending_withdrawals.pop(target, None)
    if target_sid:
        admin_sids.discard(target_sid)  # in case the target was an admin
        socketio.emit('force_logout',
                      {'message': "You have been terminated by the admin."},
                      to=target_sid)
        socketio.sleep(0.5)  # let the message flush before closing the socket
        socketio.server.disconnect(target_sid)

    emit('admin_error', {'message': f"Account '{target}' terminated."})
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('generate_code')
def handle_generate_code(data):
    # Only the admin may mint coin voucher codes
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can generate codes."})
        return

    amount = int(data.get('amount', 0))
    if amount <= 0:
        emit('admin_error', {'message': "Invalid code amount."})
        return

    # Generate a short human-readable single-use code, e.g. ABCD-1234
    chars = string.ascii_uppercase + string.digits
    code = "".join(random.choice(chars) for _ in range(4)) + "-" + \
           "".join(random.choice(chars) for _ in range(4))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO codes (code, amount) VALUES (%s, %s)", (code, amount)
    )
    conn.commit()
    cursor.close()
    conn.close()

    emit('code_generated', {'code': code, 'amount': amount})

@socketio.on('redeem_code')
def handle_redeem_code(data):
    username = data.get('username')
    code = (data.get('code') or '').strip().upper()

    if not username or not code:
        emit('redeem_result', {'success': False, 'message': "Enter a code first."})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM codes WHERE code = %s", (code,))
    voucher = cursor.fetchone()

    if not voucher:
        emit('redeem_result', {'success': False, 'message': "Invalid code."})
    elif voucher['redeemed']:
        emit('redeem_result', {'success': False, 'message': "This code was already used."})
    else:
        # Credit the player's wallet and burn the single-use voucher
        cursor.execute(
            "UPDATE users SET coins = coins + %s WHERE username = %s",
            (voucher['amount'], username)
        )
        cursor.execute(
            "UPDATE codes SET redeemed = 1, redeemed_by = %s WHERE code = %s",
            (username, code)
        )
        conn.commit()

        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        new_balance = cursor.fetchone()['coins']
        emit('user_status', {
            'username': username,
            'coins': new_balance,
            'is_admin': username == ADMIN_USERNAME
        })
        emit('redeem_result', {
            'success': True,
            'message': f"Redeemed {voucher['amount']} coins!"
        })
        # Reflect the player's new balance on the admin roster
        broadcast_player_list()
        broadcast_user_list()

    cursor.close()
    conn.close()

@socketio.on('trigger_roll')
def handle_roll(data=None):
    # Only the authenticated admin is allowed to pull the rope and roll
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can roll the dice."})
        return
    run_roll()


@socketio.on('request_roll')
def handle_request_roll(data):
    """A player asks the admin for permission to roll. The request is
    forwarded to every admin, who can approve or deny it."""
    username = (data.get('username') or '').strip()
    if not username or username == ADMIN_USERNAME:
        return
    if request.sid in admin_sids:
        return  # admins roll directly, they don't request

    pending_roll_requests.add(username)
    for sid in admin_sids:
        socketio.emit('roll_request', {'username': username}, to=sid)
    emit('roll_request_sent', {'message': "Roll request sent to the admin..."})


@socketio.on('approve_roll')
def handle_approve_roll(data):
    # Admin approves a pending request, which runs the table roll for everyone
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can approve a roll."})
        return
    requester = (data.get('username') or '').strip()
    pending_roll_requests.discard(requester)
    run_roll()


@socketio.on('deny_roll')
def handle_deny_roll(data):
    # Admin rejects a pending roll request and tells the player
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can deny a roll."})
        return
    requester = (data.get('username') or '').strip()
    pending_roll_requests.discard(requester)
    target_sid = online_players.get(requester)
    if target_sid:
        socketio.emit('roll_denied',
                      {'message': "The admin denied your roll request."},
                      to=target_sid)


@socketio.on('request_withdraw')
def handle_request_withdraw(data):
    """A player asks the admin to cash out some of their coins. The request
    is queued and forwarded to every admin to approve or deny."""
    username = (data.get('username') or '').strip()
    if not username or username == ADMIN_USERNAME or request.sid in admin_sids:
        return
    try:
        amount = int(data.get('amount', 0))
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        emit('withdraw_status', {'message': "Enter a valid withdraw amount."})
        return

    # Informational funds check now; the balance is re-checked at approval.
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if not row:
        return
    if row['coins'] < amount:
        emit('withdraw_status', {'message': "Not enough coins to withdraw that much."})
        return

    pending_withdrawals[username] = amount
    for sid in admin_sids:
        socketio.emit('withdraw_request', {'username': username, 'amount': amount}, to=sid)
    emit('withdraw_status', {'message': f"⏳ Withdraw request for {amount} sent to the admin..."})


@socketio.on('approve_withdraw')
def handle_approve_withdraw(data):
    # Admin approves a pending cash-out: deduct coins and bank total_withdrawn.
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can approve withdrawals."})
        return
    username = (data.get('username') or '').strip()
    amount = pending_withdrawals.pop(username, None)
    if not amount:
        emit('admin_error', {'message': "No pending withdrawal for that player."})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    row = cursor.fetchone()
    # Re-check funds — the player may have spent coins since requesting.
    if not row or row['coins'] < amount:
        cursor.close()
        conn.close()
        emit('admin_error', {'message': f"{username} no longer has {amount} coins."})
        target_sid = online_players.get(username)
        if target_sid:
            socketio.emit('withdraw_status',
                          {'message': "Your withdrawal failed: not enough coins now."},
                          to=target_sid)
        return

    new_balance = row['coins'] - amount
    cursor.execute(
        "UPDATE users SET coins = %s, total_withdrawn = total_withdrawn + %s WHERE username = %s",
        (new_balance, amount, username)
    )
    conn.commit()
    cursor.execute("SELECT total_withdrawn FROM users WHERE username = %s", (username,))
    total_withdrawn = cursor.fetchone()['total_withdrawn']
    cursor.close()
    conn.close()

    target_sid = online_players.get(username)
    if target_sid:
        socketio.emit('withdraw_status', {
            'message': f"✅ Withdrawal of {amount} approved!",
            'coins': new_balance,
            'total_withdrawn': total_withdrawn,
        }, to=target_sid)
    emit('admin_error', {'message': f"Approved {username}'s withdrawal of {amount}."})
    broadcast_player_list()
    broadcast_user_list()


@socketio.on('deny_withdraw')
def handle_deny_withdraw(data):
    # Admin rejects a pending cash-out request and tells the player.
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can deny withdrawals."})
        return
    username = (data.get('username') or '').strip()
    pending_withdrawals.pop(username, None)
    target_sid = online_players.get(username)
    if target_sid:
        socketio.emit('withdraw_status',
                      {'message': "🚫 The admin denied your withdrawal request."},
                      to=target_sid)


def run_roll():
    """Perform a single dice roll for the whole table and pay out winners."""
    global roll_in_progress
    # Lock out bet-clearing for the duration so refunds can't race the payout.
    roll_in_progress = True
    try:
        # 1. Broadcast chaotic spinning/rolling status trigger to all connections
        socketio.emit('dice_rolling')
        socketio.sleep(2)  # Artificial spin delay logic

        # 2. Draw 3 random color results
        dice_results = [random.choice(COLORS) for _ in range(3)]

        # 3. Process calculations against database profiles
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        payout_reports = {}

        for username, bets in active_bets.items():
            total_won = 0
            total_returned = 0

            for color, bet_amount in bets.items():
                if bet_amount > 0:
                    match_count = dice_results.count(color)
                    if match_count > 0:
                        # Win returns original stake + (match multiplier * stake)
                        total_won += bet_amount * match_count
                        total_returned += bet_amount

            net_payout = total_won + total_returned
            if net_payout > 0:
                # Credit the payout, and bank total_won as lifetime winnings
                # (the returned stake isn't "earned", it was already theirs).
                cursor.execute(
                    "UPDATE users SET coins = coins + %s, total_earned = total_earned + %s WHERE username = %s",
                    (net_payout, total_won, username)
                )
                payout_reports[username] = net_payout

            # Reset internal tracker for subsequent cycles
            active_bets[username] = {color: 0 for color in COLORS}

        conn.commit()
        cursor.close()
        conn.close()

        # 4. Push exact final landed colors out to all screens globally
        socketio.emit('roll_results', {
            'dice': dice_results,
            'payouts': payout_reports
        })
        # Winners' balances changed, so refresh the admin roster
        broadcast_player_list()
        broadcast_user_list()
    finally:
        roll_in_progress = False

# Create the schema on startup. This runs under gunicorn too (where the
# __main__ block below is skipped). Wrapped so an unreachable DB during a
# build step doesn't crash the import.
try:
    init_db()
except Exception as e:
    print(f"init_db skipped at startup: {e}")

if __name__ == '__main__':
    # Local development entry point. In production the host runs gunicorn
    # against the `app` object instead (see Procfile).
    socketio.run(app, debug=True)

import os
import random
import string
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import mysql.connector


def _load_dotenv(path=".env"):
    """Load KEY=VALUE lines from a local .env file into the environment so
    secrets stay out of source control. Real env vars take precedence."""
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
# Engine uses eventlet for asynchronous background execution tasks
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")

COLORS = ["red", "blue", "green", "yellow", "white", "pink"]

# Admin login — set these in your .env / host environment, never in source.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")

# Database connection — credentials come from the environment / .env file.
def get_db_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "perya_color_game"),
    )

# Ensure the voucher-codes table exists and the users table has the
# password / active columns needed for admin-controlled player logins.
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS codes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(20) UNIQUE NOT NULL,
            amount INT NOT NULL,
            redeemed TINYINT(1) NOT NULL DEFAULT 0,
            redeemed_by VARCHAR(50) DEFAULT NULL
        )
    """)

    # Add new columns to the existing users table only if they're missing
    cursor.execute("SHOW COLUMNS FROM users")
    existing = {row[0] for row in cursor.fetchall()}
    if 'password' not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN password VARCHAR(50) DEFAULT NULL")
    if 'active' not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN active TINYINT(1) NOT NULL DEFAULT 1")
    if 'is_admin' not in existing:
        cursor.execute("ALTER TABLE users ADD COLUMN is_admin TINYINT(1) NOT NULL DEFAULT 0")
    if 'total_earned' not in existing:
        # Lifetime coins won from rolls (the winnings, on top of returned stake)
        cursor.execute("ALTER TABLE users ADD COLUMN total_earned INT NOT NULL DEFAULT 0")
    if 'total_withdrawn' not in existing:
        # Lifetime coins the player has cashed out via approved withdrawals
        cursor.execute("ALTER TABLE users ADD COLUMN total_withdrawn INT NOT NULL DEFAULT 0")

    conn.commit()
    cursor.close()
    conn.close()

# Track active live player bets in-memory per round
# Format: { username: { "red": 10, "blue": 0... } }
active_bets = {}

# Live session tracking so the admin can see and kick connected players.
online_players = {}  # username -> socket session id (sid)
admin_sids = set()   # sids that have authenticated as the admin

# Usernames of players who have asked the admin for permission to roll
pending_roll_requests = set()

# Pending cash-out requests awaiting admin approval: username -> amount
pending_withdrawals = {}

# True only while a roll is being processed, so players can't clear/refund
# bets that the roll is about to pay out (which would double-credit them).
roll_in_progress = False

def is_admin_request():
    """Admin powers are tied to an authenticated socket session, not a
    username string, so they can't be spoofed by a normal player."""
    return request.sid in admin_sids

def broadcast_player_list():
    """Push the current roster of logged-in players, with their live coin
    balances, to every admin."""
    players = [u for u in online_players if u != ADMIN_USERNAME]

    balances = {}
    if players:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Build a "username IN (%s, %s, ...)" lookup for the online players
        placeholders = ", ".join(["%s"] * len(players))
        cursor.execute(
            f"SELECT username, coins FROM users WHERE username IN ({placeholders})",
            tuple(players),
        )
        balances = {row['username']: row['coins'] for row in cursor.fetchall()}
        cursor.close()
        conn.close()

    # Each player's true holdings = wallet balance + coins staked this round.
    roster = []
    for u in players:
        wallet = balances.get(u, 0)
        staked = sum(active_bets.get(u, {}).values())
        roster.append({
            'username': u,
            'coins': wallet,
            'staked': staked,
            'total': wallet + staked,
        })
    for sid in admin_sids:
        socketio.emit('player_list', {'players': roster}, to=sid)

def broadcast_user_list():
    """Push the full list of created user accounts (online or not) to every
    admin, so they can manage logins and see who exists."""
    if not admin_sids:
        return
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT username, password, coins, active, is_admin, total_earned, total_withdrawn FROM users ORDER BY is_admin DESC, username"
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    online = set(online_players)
    users = [{
        'username': r['username'],
        'password': r['password'],
        'coins': r['coins'],
        'active': bool(r['active']),
        'is_admin': bool(r['is_admin']),
        'total_earned': r['total_earned'],
        'total_withdrawn': r['total_withdrawn'],
        'online': r['username'] in online,
    } for r in rows]

    # Total coins in circulation = every wallet balance plus whatever is
    # currently staked on the table (staked coins are deducted from wallets
    # but not yet paid out, so they'd otherwise be uncounted).
    staked_total = sum(sum(b.values()) for b in active_bets.values())
    total_coins = sum(r['coins'] for r in rows) + staked_total

    for sid in admin_sids:
        socketio.emit('user_list', {'users': users, 'total_coins': total_coins}, to=sid)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('join_game')
def handle_join(data):
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    if not username:
        emit('login_failed', {'message': "Enter a username."})
        return

    # The bootstrap super-admin authenticates against the hardcoded creds.
    # This account always works so you can never lock yourself out.
    if username == ADMIN_USERNAME:
        if password != ADMIN_PASSWORD:
            emit('login_failed', {'message': "Wrong admin password."})
            return
        admin_sids.add(request.sid)
        online_players[username] = request.sid
        emit('user_status', {'username': username, 'coins': 0, 'is_admin': True})
        broadcast_player_list()
        broadcast_user_list()
        return

    # Everyone else must use an account the admin created for them.
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()
    cursor.close()
    conn.close()

    if not user or not user['password']:
        emit('login_failed', {'message': "No such account. Ask the admin for a login."})
        return
    if not user['active']:
        emit('login_failed', {'message': "This account has been terminated by the admin."})
        return
    if password != user['password']:
        emit('login_failed', {'message': "Wrong password."})
        return

    online_players[username] = request.sid

    # Accounts flagged is_admin get the admin console; everyone else plays.
    if user.get('is_admin'):
        admin_sids.add(request.sid)
        emit('user_status', {'username': username, 'coins': user['coins'], 'is_admin': True})
        broadcast_player_list()
        broadcast_user_list()
        return

    active_bets[username] = {color: 0 for color in COLORS}
    emit('user_status', {
        'username': username,
        'coins': user['coins'],
        'is_admin': False,
        'total_earned': user.get('total_earned', 0),
        'total_withdrawn': user.get('total_withdrawn', 0),
    })
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('disconnect')
def handle_disconnect():
    # Drop the session from our live rosters and refresh the admin view.
    sid = request.sid
    admin_sids.discard(sid)
    gone = [u for u, s in online_players.items() if s == sid]
    for u in gone:
        online_players.pop(u, None)
        pending_withdrawals.pop(u, None)
    if gone:
        broadcast_player_list()
        broadcast_user_list()

@socketio.on('place_bet')
def handle_bet(data):
    username = data.get('username')
    color = data.get('color')
    amount = int(data.get('amount', 0))

    # Reject obviously invalid stakes before touching the database
    if not username or color not in COLORS or amount <= 0:
        emit('bet_rejected', {'message': "Invalid bet."})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    user = cursor.fetchone()

    if not user:
        cursor.close()
        conn.close()
        emit('bet_rejected', {'message': "Account not found."})
        return

    if user['coins'] < amount:
        cursor.close()
        conn.close()
        emit('bet_rejected', {
            'message': "Not enough coins for that bet.",
            'coins': user['coins'],
        })
        return

    # Deduct temporary betting stake out of safe database wallet balance
    new_balance = user['coins'] - amount
    cursor.execute("UPDATE users SET coins = %s WHERE username = %s", (new_balance, username))
    conn.commit()
    cursor.close()
    conn.close()

    # A live session may not have a bet tracker yet (e.g. reconnects), so
    # make sure one exists before recording the stake.
    table = active_bets.setdefault(username, {c: 0 for c in COLORS})
    table[color] += amount

    # Send the authoritative per-color total back so the board renders the
    # server's truth instead of an optimistic client guess.
    emit('bet_placed', {
        'username': username,
        'color': color,
        'color_total': table[color],
        'coins': new_balance,
    })
    # Broadcast total system table-bets to make it feel alive
    emit('table_update', {'message': f"{username} placed {amount} on {color.upper()}"}, broadcast=True)
    # Reflect the player's new balance on the admin roster
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('clear_bets')
def handle_clear_bets(data):
    """Take back all of a player's bets for the current round and refund the
    staked coins to their wallet."""
    username = data.get('username')
    if not username:
        emit('bet_rejected', {'message': "Log in before clearing bets."})
        return
    # Don't allow a refund once a roll is paying out, or we'd double-credit.
    if roll_in_progress:
        emit('bet_rejected', {'message': "Can't clear bets while the dice are rolling."})
        return

    table = active_bets.get(username)
    refund = sum(table.values()) if table else 0
    if refund <= 0:
        # Nothing staked — still tell the client so the board resets cleanly.
        emit('bets_cleared', {'username': username, 'refunded': 0})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("UPDATE users SET coins = coins + %s WHERE username = %s", (refund, username))
    conn.commit()
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    new_balance = cursor.fetchone()['coins']
    cursor.close()
    conn.close()

    active_bets[username] = {color: 0 for color in COLORS}

    emit('bets_cleared', {
        'username': username,
        'refunded': refund,
        'coins': new_balance,
    })
    emit('table_update',
         {'message': f"{username} cleared their bets (+{refund} refunded)"},
         broadcast=True)
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('create_player')
def handle_create_player(data):
    # Admin generates a username + password login for a new player (or, when
    # make_admin is set, another admin account).
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can create logins."})
        return

    username = (data.get('username') or '').strip()
    coins = int(data.get('coins', 1000))
    make_admin = bool(data.get('is_admin'))
    if not username:
        emit('admin_error', {'message': "Enter a username for the new account."})
        return
    if username == ADMIN_USERNAME:
        emit('admin_error', {'message': "That username is reserved."})
        return

    # Auto-generate a short password for the player
    chars = string.ascii_uppercase + string.digits
    password = "".join(random.choice(chars) for _ in range(6))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT username FROM users WHERE username = %s", (username,))
    if cursor.fetchone():
        cursor.close()
        conn.close()
        emit('admin_error', {'message': f"Username '{username}' already exists."})
        return

    cursor.execute(
        "INSERT INTO users (username, password, coins, active, is_admin) VALUES (%s, %s, %s, 1, %s)",
        (username, password, coins, 1 if make_admin else 0)
    )
    conn.commit()
    cursor.close()
    conn.close()

    emit('player_created', {
        'username': username,
        'password': password,
        'coins': coins,
        'is_admin': make_admin,
    })
    # Refresh the persistent "all users" list shown to every admin
    broadcast_user_list()

@socketio.on('terminate_player')
def handle_terminate_player(data):
    # Admin kicks a player offline AND bans the account from logging back in.
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can terminate players."})
        return

    target = (data.get('username') or '').strip()
    if not target or target == ADMIN_USERNAME:
        emit('admin_error', {'message': "Invalid player to terminate."})
        return

    # Ban: disable the account so the credentials no longer work
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET active = 0 WHERE username = %s", (target,))
    conn.commit()
    cursor.close()
    conn.close()

    # Kick: force the user's live session offline if they're connected
    target_sid = online_players.pop(target, None)
    active_bets.pop(target, None)
    pending_withdrawals.pop(target, None)
    if target_sid:
        admin_sids.discard(target_sid)  # in case the target was an admin
        socketio.emit('force_logout',
                      {'message': "You have been terminated by the admin."},
                      to=target_sid)
        socketio.sleep(0.5)  # let the message flush before closing the socket
        socketio.server.disconnect(target_sid)

    emit('admin_error', {'message': f"Account '{target}' terminated."})
    broadcast_player_list()
    broadcast_user_list()

@socketio.on('generate_code')
def handle_generate_code(data):
    # Only the admin may mint coin voucher codes
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can generate codes."})
        return

    amount = int(data.get('amount', 0))
    if amount <= 0:
        emit('admin_error', {'message': "Invalid code amount."})
        return

    # Generate a short human-readable single-use code, e.g. ABCD-1234
    chars = string.ascii_uppercase + string.digits
    code = "".join(random.choice(chars) for _ in range(4)) + "-" + \
           "".join(random.choice(chars) for _ in range(4))

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO codes (code, amount) VALUES (%s, %s)", (code, amount)
    )
    conn.commit()
    cursor.close()
    conn.close()

    emit('code_generated', {'code': code, 'amount': amount})

@socketio.on('redeem_code')
def handle_redeem_code(data):
    username = data.get('username')
    code = (data.get('code') or '').strip().upper()

    if not username or not code:
        emit('redeem_result', {'success': False, 'message': "Enter a code first."})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM codes WHERE code = %s", (code,))
    voucher = cursor.fetchone()

    if not voucher:
        emit('redeem_result', {'success': False, 'message': "Invalid code."})
    elif voucher['redeemed']:
        emit('redeem_result', {'success': False, 'message': "This code was already used."})
    else:
        # Credit the player's wallet and burn the single-use voucher
        cursor.execute(
            "UPDATE users SET coins = coins + %s WHERE username = %s",
            (voucher['amount'], username)
        )
        cursor.execute(
            "UPDATE codes SET redeemed = 1, redeemed_by = %s WHERE code = %s",
            (username, code)
        )
        conn.commit()

        cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
        new_balance = cursor.fetchone()['coins']
        emit('user_status', {
            'username': username,
            'coins': new_balance,
            'is_admin': username == ADMIN_USERNAME
        })
        emit('redeem_result', {
            'success': True,
            'message': f"Redeemed {voucher['amount']} coins!"
        })
        # Reflect the player's new balance on the admin roster
        broadcast_player_list()
        broadcast_user_list()

    cursor.close()
    conn.close()

@socketio.on('trigger_roll')
def handle_roll(data=None):
    # Only the authenticated admin is allowed to pull the rope and roll
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can roll the dice."})
        return
    run_roll()


@socketio.on('request_roll')
def handle_request_roll(data):
    """A player asks the admin for permission to roll. The request is
    forwarded to every admin, who can approve or deny it."""
    username = (data.get('username') or '').strip()
    if not username or username == ADMIN_USERNAME:
        return
    if request.sid in admin_sids:
        return  # admins roll directly, they don't request

    pending_roll_requests.add(username)
    for sid in admin_sids:
        socketio.emit('roll_request', {'username': username}, to=sid)
    emit('roll_request_sent', {'message': "Roll request sent to the admin..."})


@socketio.on('approve_roll')
def handle_approve_roll(data):
    # Admin approves a pending request, which runs the table roll for everyone
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can approve a roll."})
        return
    requester = (data.get('username') or '').strip()
    pending_roll_requests.discard(requester)
    run_roll()


@socketio.on('deny_roll')
def handle_deny_roll(data):
    # Admin rejects a pending roll request and tells the player
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can deny a roll."})
        return
    requester = (data.get('username') or '').strip()
    pending_roll_requests.discard(requester)
    target_sid = online_players.get(requester)
    if target_sid:
        socketio.emit('roll_denied',
                      {'message': "The admin denied your roll request."},
                      to=target_sid)


@socketio.on('request_withdraw')
def handle_request_withdraw(data):
    """A player asks the admin to cash out some of their coins. The request
    is queued and forwarded to every admin to approve or deny."""
    username = (data.get('username') or '').strip()
    if not username or username == ADMIN_USERNAME or request.sid in admin_sids:
        return
    try:
        amount = int(data.get('amount', 0))
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        emit('withdraw_status', {'message': "Enter a valid withdraw amount."})
        return

    # Informational funds check now; the balance is re-checked at approval.
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if not row:
        return
    if row['coins'] < amount:
        emit('withdraw_status', {'message': "Not enough coins to withdraw that much."})
        return

    pending_withdrawals[username] = amount
    for sid in admin_sids:
        socketio.emit('withdraw_request', {'username': username, 'amount': amount}, to=sid)
    emit('withdraw_status', {'message': f"⏳ Withdraw request for {amount} sent to the admin..."})


@socketio.on('approve_withdraw')
def handle_approve_withdraw(data):
    # Admin approves a pending cash-out: deduct coins and bank total_withdrawn.
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can approve withdrawals."})
        return
    username = (data.get('username') or '').strip()
    amount = pending_withdrawals.pop(username, None)
    if not amount:
        emit('admin_error', {'message': "No pending withdrawal for that player."})
        return

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT coins FROM users WHERE username = %s", (username,))
    row = cursor.fetchone()
    # Re-check funds — the player may have spent coins since requesting.
    if not row or row['coins'] < amount:
        cursor.close()
        conn.close()
        emit('admin_error', {'message': f"{username} no longer has {amount} coins."})
        target_sid = online_players.get(username)
        if target_sid:
            socketio.emit('withdraw_status',
                          {'message': "Your withdrawal failed: not enough coins now."},
                          to=target_sid)
        return

    new_balance = row['coins'] - amount
    cursor.execute(
        "UPDATE users SET coins = %s, total_withdrawn = total_withdrawn + %s WHERE username = %s",
        (new_balance, amount, username)
    )
    conn.commit()
    cursor.execute("SELECT total_withdrawn FROM users WHERE username = %s", (username,))
    total_withdrawn = cursor.fetchone()['total_withdrawn']
    cursor.close()
    conn.close()

    target_sid = online_players.get(username)
    if target_sid:
        socketio.emit('withdraw_status', {
            'message': f"✅ Withdrawal of {amount} approved!",
            'coins': new_balance,
            'total_withdrawn': total_withdrawn,
        }, to=target_sid)
    emit('admin_error', {'message': f"Approved {username}'s withdrawal of {amount}."})
    broadcast_player_list()
    broadcast_user_list()


@socketio.on('deny_withdraw')
def handle_deny_withdraw(data):
    # Admin rejects a pending cash-out request and tells the player.
    if not is_admin_request():
        emit('admin_error', {'message': "Only the admin can deny withdrawals."})
        return
    username = (data.get('username') or '').strip()
    pending_withdrawals.pop(username, None)
    target_sid = online_players.get(username)
    if target_sid:
        socketio.emit('withdraw_status',
                      {'message': "🚫 The admin denied your withdrawal request."},
                      to=target_sid)


def run_roll():
    """Perform a single dice roll for the whole table and pay out winners."""
    global roll_in_progress
    # Lock out bet-clearing for the duration so refunds can't race the payout.
    roll_in_progress = True
    try:
        # 1. Broadcast chaotic spinning/rolling status trigger to all connections
        socketio.emit('dice_rolling')
        socketio.sleep(2)  # Artificial spin delay logic

        # 2. Draw 3 random color results
        dice_results = [random.choice(COLORS) for _ in range(3)]

        # 3. Process calculations against database profiles
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        payout_reports = {}

        for username, bets in active_bets.items():
            total_won = 0
            total_returned = 0

            for color, bet_amount in bets.items():
                if bet_amount > 0:
                    match_count = dice_results.count(color)
                    if match_count > 0:
                        # Win returns original stake + (match multiplier * stake)
                        total_won += bet_amount * match_count
                        total_returned += bet_amount

            net_payout = total_won + total_returned
            if net_payout > 0:
                # Credit the payout, and bank total_won as lifetime winnings
                # (the returned stake isn't "earned", it was already theirs).
                cursor.execute(
                    "UPDATE users SET coins = coins + %s, total_earned = total_earned + %s WHERE username = %s",
                    (net_payout, total_won, username)
                )
                payout_reports[username] = net_payout

            # Reset internal tracker for subsequent cycles
            active_bets[username] = {color: 0 for color in COLORS}

        conn.commit()
        cursor.close()
        conn.close()

        # 4. Push exact final landed colors out to all screens globally
        socketio.emit('roll_results', {
            'dice': dice_results,
            'payouts': payout_reports
        })
        # Winners' balances changed, so refresh the admin roster
        broadcast_player_list()
        broadcast_user_list()
    finally:
        roll_in_progress = False

# Create the schema on startup. This runs under gunicorn too (where the
# __main__ block below is skipped). Wrapped so an unreachable DB during a
# build step doesn't crash the import.
try:
    init_db()
except Exception as e:
    print(f"init_db skipped at startup: {e}")

if __name__ == '__main__':
    # Local development entry point. In production the host runs gunicorn
    # against the `app` object instead (see Procfile).
    socketio.run(app, debug=True)

