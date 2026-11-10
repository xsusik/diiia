import os
import json
import uuid
import base64
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from dotenv import load_dotenv

load_dotenv()

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
PROFILE_FILE  = os.path.join(BASE_DIR, '..', 'profiles.json')
KEYS_FILE     = os.path.join(BASE_DIR, 'keys.json')
USERS_FILE    = os.path.join(BASE_DIR, 'users.json')
SIGN_DIR      = os.path.join(BASE_DIR, 'signatures')
os.makedirs(SIGN_DIR, exist_ok=True)

# ─── Conversation states ──────────────────────────────────────────────────────
(
    ST_KEY,
    ST_MENU,
    ST_PROFILE_PICK,
    ST_MANUAL_NAME,
    ST_MANUAL_SURNAME,
    ST_MANUAL_MIDDLE,
    ST_MANUAL_BIRTH,
    ST_MANUAL_INN,
    ST_MANUAL_PASSPORT,
    ST_SIGN_WAIT,
) = range(10)

SIGN_PORT = int(os.getenv('SIGN_PORT', '8765'))

# ─── JSON helpers ─────────────────────────────────────────────────────────────

def _read(path, default=None):
    if default is None:
        default = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def profiles_all():
    return _read(PROFILE_FILE, [])


def users_all():
    return _read(USERS_FILE, {})


def users_save(data):
    _write(USERS_FILE, data)


def keys_all():
    return _read(KEYS_FILE, {})


def keys_save(data):
    _write(KEYS_FILE, data)

# ─── Access control ───────────────────────────────────────────────────────────

def user_has_access(user_id: int) -> bool:
    users = users_all()
    u = users.get(str(user_id), {})
    if not u.get('active'):
        return False
    exp = u.get('expires')
    if exp and datetime.fromisoformat(exp) < datetime.now():
        u['active'] = False
        users[str(user_id)] = u
        users_save(users)
        return False
    return True


def activate_user(user_id: int, key_meta: dict):
    users = users_all()
    days = key_meta.get('days', 30)
    users[str(user_id)] = {
        'active': True,
        'activated_at': datetime.now().isoformat(),
        'expires': (datetime.now() + timedelta(days=days)).isoformat(),
        'plan': key_meta.get('plan', 'standard'),
    }
    users_save(users)


def consume_key(key: str, user_id: int):
    """Returns key meta dict or None if invalid/already used."""
    keys = keys_all()
    meta = keys.get(key)
    if not meta:
        return None
    if meta.get('used'):
        return None
    meta['used'] = True
    meta['used_by'] = user_id
    meta['used_at'] = datetime.now().isoformat()
    keys[key] = meta
    keys_save(keys)
    return meta

# ─── Signature HTTP server ────────────────────────────────────────────────────
# token -> user_id  (pending sessions)
_sign_pending: dict = {}
# token -> base64 PNG data URL
_sign_results: dict = {}

SIGN_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Підпис — Дія</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#abc8e2;font-family:sans-serif;display:flex;flex-direction:column;
       align-items:center;justify-content:center;min-height:100vh;gap:16px;padding:20px}
  h2{color:#1a3f5c;font-size:1.3rem}
  #canvas{background:#fff;border:2px solid #1a3f5c;border-radius:12px;
          cursor:crosshair;touch-action:none;display:block}
  .row{display:flex;gap:12px}
  button{padding:10px 24px;border:none;border-radius:8px;font-size:1rem;cursor:pointer}
  #btnClear{background:#e0e0e0;color:#333}
  #btnSave{background:#1a3f5c;color:#fff}
  #msg{font-size:.9rem;color:#1a3f5c;min-height:1.4em;text-align:center}
</style>
</head>
<body>
<h2>Намалюйте підпис</h2>
<canvas id="canvas" width="340" height="160"></canvas>
<div class="row">
  <button id="btnClear">Очистити</button>
  <button id="btnSave">Зберегти ✓</button>
</div>
<p id="msg"></p>
<script>
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
ctx.strokeStyle = '#1a3f5c';
ctx.lineWidth = 2.5;
ctx.lineCap = 'round';
ctx.lineJoin = 'round';
let drawing = false;

function pt(e) {
  const r = canvas.getBoundingClientRect();
  const scaleX = canvas.width / r.width;
  const scaleY = canvas.height / r.height;
  if (e.touches) {
    return { x: (e.touches[0].clientX - r.left) * scaleX,
             y: (e.touches[0].clientY - r.top) * scaleY };
  }
  return { x: (e.clientX - r.left) * scaleX, y: (e.clientY - r.top) * scaleY };
}

canvas.addEventListener('mousedown', e => {
  drawing = true; ctx.beginPath(); const p = pt(e); ctx.moveTo(p.x, p.y);
});
canvas.addEventListener('mousemove', e => {
  if (!drawing) return; const p = pt(e); ctx.lineTo(p.x, p.y); ctx.stroke();
});
canvas.addEventListener('mouseup', () => drawing = false);
canvas.addEventListener('mouseleave', () => drawing = false);
canvas.addEventListener('touchstart', e => {
  e.preventDefault(); drawing = true; ctx.beginPath();
  const p = pt(e); ctx.moveTo(p.x, p.y);
}, { passive: false });
canvas.addEventListener('touchmove', e => {
  e.preventDefault(); if (!drawing) return;
  const p = pt(e); ctx.lineTo(p.x, p.y); ctx.stroke();
}, { passive: false });
canvas.addEventListener('touchend', () => drawing = false);

document.getElementById('btnClear').onclick = () =>
  ctx.clearRect(0, 0, canvas.width, canvas.height);

document.getElementById('btnSave').onclick = async () => {
  const data = canvas.toDataURL('image/png');
  const token = new URLSearchParams(location.search).get('t');
  try {
    const res = await fetch('/sign?t=' + token, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ img: data }),
    });
    if (res.ok) {
      document.getElementById('msg').textContent =
        '✅ Підпис збережено! Поверніться в Telegram і натисніть "Підпис готовий".';
    } else {
      document.getElementById('msg').textContent = '❌ Помилка. Спробуйте ще раз.';
    }
  } catch {
    document.getElementById('msg').textContent = '❌ Немає з\'єднання з сервером.';
  }
};
</script>
</body>
</html>
"""


class SignHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence access logs

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/sign':
            token = parse_qs(parsed.query).get('t', [None])[0]
            if token and token in _sign_pending:
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(SIGN_HTML.encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'Invalid or expired token.')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == '/sign':
            token = parse_qs(parsed.query).get('t', [None])[0]
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
                img = payload.get('img', '')
                if token and token in _sign_pending and img.startswith('data:image'):
                    _sign_results[token] = img
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'ok')
                    return
            except Exception:
                pass
            self.send_response(400)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


def _start_sign_server():
    server = HTTPServer(('0.0.0.0', SIGN_PORT), SignHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[sign-server] listening on :{SIGN_PORT}")

# ─── UI helpers ───────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            ['📋 Готові профілі', '✍️ Намалювати підпис'],
            ['👤 Мій профіль',    '📄 Показати документ'],
            ['ℹ️ Статус ключа'],
        ],
        resize_keyboard=True,
    )


def _user_profile(user_id: int):
    return users_all().get(str(user_id), {}).get('profile')


def _save_user_profile(user_id: int, profile: dict):
    users = users_all()
    u = users.setdefault(str(user_id), {})
    u['profile'] = profile
    users_save(users)


def profile_text(p: dict) -> str:
    has_sig = '✅ є' if p.get('signature') else '⬜ не встановлено'
    return (
        f"👤 *{p.get('lastName','')} {p.get('firstName','')} {p.get('middleName','')}*\n"
        f"📅 Дата народження: `{p.get('birth','—')}`\n"
        f"🔢 ІПН: `{p.get('rnokpp','—')}`\n"
        f"🪪 Паспорт: `{p.get('passport','—')}`\n"
        f"✍️ Підпис: {has_sig}"
    )

# ─── /start ───────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_has_access(uid):
        await update.message.reply_text(
            "🇺🇦 *Дія-бот*\nОберіть дію:",
            parse_mode='Markdown',
            reply_markup=main_menu_keyboard(),
        )
        return ST_MENU
    await update.message.reply_text(
        "🔑 *Введіть ліцензійний ключ* для активації доступу:\n_(Ключ видається при купівлі)_",
        parse_mode='Markdown',
    )
    return ST_KEY

# ─── Key activation ───────────────────────────────────────────────────────────

async def handle_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    key = update.message.text.strip().upper()
    meta = consume_key(key, uid)
    if not meta:
        await update.message.reply_text("❌ Ключ недійсний або вже використаний. Спробуйте інший.")
        return ST_KEY
    activate_user(uid, meta)
    await update.message.reply_text(
        f"✅ *Доступ активовано!*\n"
        f"Тариф: `{meta.get('plan','standard')}`\n"
        f"Термін: `{meta.get('days',30)}` днів\n\nОберіть дію:",
        parse_mode='Markdown',
        reply_markup=main_menu_keyboard(),
    )
    return ST_MENU

# ─── Menu router ──────────────────────────────────────────────────────────────

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not user_has_access(uid):
        await update.message.reply_text("🔑 Доступ закінчився. Введіть /start і активуйте новий ключ.")
        return ConversationHandler.END

    text = update.message.text

    if text == '📋 Готові профілі':
        return await _show_ready_profiles(update, context)

    if text == '✍️ Намалювати підпис':
        return await _start_signature(update, context)

    if text == '👤 Мій профіль':
        p = _user_profile(uid)
        msg = profile_text(p) if p else "У вас ще немає профілю. Оберіть *Готові профілі*."
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=main_menu_keyboard())
        return ST_MENU

    if text == '📄 Показати документ':
        return await _show_document_link(update, context)

    if text == 'ℹ️ Статус ключа':
        u = users_all().get(str(uid), {})
        await update.message.reply_text(
            f"📋 *Статус*\nТариф: `{u.get('plan','—')}`\nДійсний до: `{u.get('expires','—')}`",
            parse_mode='Markdown',
            reply_markup=main_menu_keyboard(),
        )
        return ST_MENU

    return ST_MENU

# ─── Ready profiles ───────────────────────────────────────────────────────────

async def _show_ready_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profiles = profiles_all()
    if not profiles:
        await update.message.reply_text("Готових профілів немає.", reply_markup=main_menu_keyboard())
        return ST_MENU
    context.user_data['ready_profiles'] = profiles
    context.user_data['profile_page'] = 0
    await _send_profile_page(update, context)
    return ST_PROFILE_PICK


async def _send_profile_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    profiles = context.user_data.get('ready_profiles', profiles_all())
    page = context.user_data.get('profile_page', 0)
    page_size = 5
    start = page * page_size
    chunk = profiles[start: start + page_size]

    buttons = []
    for i, p in enumerate(chunk):
        label = f"{p.get('lastName','')} {p.get('firstName','')} | {p.get('birth','')}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"pick_{start + i}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Назад", callback_data="page_prev"))
    if start + page_size < len(profiles):
        nav.append(InlineKeyboardButton("➡️ Далі", callback_data="page_next"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("✏️ Ввести вручну", callback_data="manual_enter")])

    markup = InlineKeyboardMarkup(buttons)
    text = f"Оберіть профіль ({len(profiles)} шт.) або введіть вручну:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def callback_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = update.effective_user.id

    if not user_has_access(uid):
        await q.edit_message_text("🔑 Доступ закінчився. /start")
        return ConversationHandler.END

    if data == "page_next":
        context.user_data['profile_page'] += 1
        await _send_profile_page(update, context)
        return ST_PROFILE_PICK

    if data == "page_prev":
        context.user_data['profile_page'] = max(0, context.user_data['profile_page'] - 1)
        await _send_profile_page(update, context)
        return ST_PROFILE_PICK

    if data == "manual_enter":
        await q.edit_message_text("Введіть *ім'я* (по-українськи):", parse_mode='Markdown')
        context.user_data['manual'] = {}
        return ST_MANUAL_NAME

    if data.startswith("pick_"):
        idx = int(data.split("_")[1])
        profiles = context.user_data.get('ready_profiles', profiles_all())
        if idx >= len(profiles):
            await q.edit_message_text("Профіль не знайдено.")
            return ST_MENU
        chosen = dict(profiles[idx])
        # Preserve existing signature
        existing = _user_profile(uid) or {}
        if existing.get('signature'):
            chosen['signature'] = existing['signature']
        _save_user_profile(uid, chosen)
        await q.edit_message_text(
            f"✅ Профіль встановлено!\n\n{profile_text(chosen)}",
            parse_mode='Markdown',
        )
        await context.bot.send_message(uid, "Оберіть дію:", reply_markup=main_menu_keyboard())
        return ST_MENU

    return ST_PROFILE_PICK

# ─── Manual profile entry ─────────────────────────────────────────────────────

async def manual_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['manual']['firstName'] = update.message.text.strip()
    await update.message.reply_text("Введіть *прізвище*:", parse_mode='Markdown')
    return ST_MANUAL_SURNAME


async def manual_surname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['manual']['lastName'] = update.message.text.strip()
    await update.message.reply_text("Введіть *по батькові*:", parse_mode='Markdown')
    return ST_MANUAL_MIDDLE


async def manual_middle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['manual']['middleName'] = update.message.text.strip()
    await update.message.reply_text("Дата народження (DD.MM.YYYY):", parse_mode='Markdown')
    return ST_MANUAL_BIRTH


async def manual_birth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    parts = val.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        await update.message.reply_text("Невірний формат. Введіть DD.MM.YYYY:")
        return ST_MANUAL_BIRTH
    context.user_data['manual']['birth'] = val
    await update.message.reply_text("Введіть *ІПН* (10 цифр):", parse_mode='Markdown')
    return ST_MANUAL_INN


async def manual_inn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    if not val.isdigit() or len(val) != 10:
        await update.message.reply_text("ІПН — рівно 10 цифр. Спробуйте ще:")
        return ST_MANUAL_INN
    context.user_data['manual']['rnokpp'] = val
    await update.message.reply_text("Введіть *номер паспорту*:", parse_mode='Markdown')
    return ST_MANUAL_PASSPORT


async def manual_passport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data['manual']['passport'] = update.message.text.strip()
    profile = dict(context.user_data['manual'])
    profile['id'] = f"p_{uuid.uuid4().hex[:12]}"
    existing = _user_profile(uid) or {}
    if existing.get('signature'):
        profile['signature'] = existing['signature']
    _save_user_profile(uid, profile)
    await update.message.reply_text(
        f"✅ Профіль збережено!\n\n{profile_text(profile)}",
        parse_mode='Markdown',
        reply_markup=main_menu_keyboard(),
    )
    return ST_MENU

# ─── Signature flow ───────────────────────────────────────────────────────────

async def _start_signature(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    token = uuid.uuid4().hex
    _sign_pending[token] = uid
    context.user_data['sign_token'] = token

    host = os.getenv('SIGN_HOST', '127.0.0.1')
    url = f"http://{host}:{SIGN_PORT}/sign?t={token}"

    await update.message.reply_text(
        f"✍️ *Намалюйте підпис* за посиланням:\n\n{url}\n\n"
        "Після збереження натисніть кнопку нижче.",
        parse_mode='Markdown',
        reply_markup=ReplyKeyboardMarkup(
            [['✅ Підпис готовий'], ['❌ Скасувати']],
            resize_keyboard=True,
        ),
    )
    return ST_SIGN_WAIT


async def handle_sign_wait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if text == '❌ Скасувати':
        token = context.user_data.pop('sign_token', None)
        if token:
            _sign_pending.pop(token, None)
        await update.message.reply_text("Скасовано.", reply_markup=main_menu_keyboard())
        return ST_MENU

    token = context.user_data.get('sign_token')
    img = _sign_results.get(token)
    if not img:
        await update.message.reply_text(
            "⏳ Підпис ще не отримано. Перейдіть за посиланням, намалюйте і натисніть Зберегти."
        )
        return ST_SIGN_WAIT

    # Inject signature into user profile
    profile = _user_profile(uid) or {}
    profile['signature'] = img
    _save_user_profile(uid, profile)

    # Also patch profiles.json if the profile originated there
    pid = profile.get('id')
    if pid:
        all_p = profiles_all()
        for p in all_p:
            if p.get('id') == pid:
                p['signature'] = img
                break
        _write(PROFILE_FILE, all_p)

    # Cleanup token
    _sign_pending.pop(token, None)
    _sign_results.pop(token, None)
    context.user_data.pop('sign_token', None)

    await update.message.reply_text(
        "✅ Підпис збережено у профіль і в документ Дії!",
        reply_markup=main_menu_keyboard(),
    )
    return ST_MENU

# ─── Document link ────────────────────────────────────────────────────────────

async def _show_document_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    p = _user_profile(uid)
    if not p:
        await update.message.reply_text(
            "Спочатку оберіть профіль (📋 Готові профілі).",
            reply_markup=main_menu_keyboard(),
        )
        return ST_MENU
    payload = json.dumps(p, ensure_ascii=False)
    encoded = base64.urlsafe_b64encode(payload.encode()).decode()
    host = os.getenv('DIIA_HOST', '127.0.0.1:5500')
    url = f"http://{host}/index.html#profile={encoded}"
    await update.message.reply_text(
        f"📄 *Відкрийте документ Дії:*\n\n{url}",
        parse_mode='Markdown',
        reply_markup=main_menu_keyboard(),
    )
    return ST_MENU

# ─── Cancel ───────────────────────────────────────────────────────────────────

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано.", reply_markup=main_menu_keyboard())
    return ST_MENU

# ─── Admin commands ───────────────────────────────────────────────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = int(os.getenv('ADMIN_ID', '0'))
    if update.effective_user.id != admin_id:
        return
    args = context.args or []
    sub = args[0] if args else ''

    # genkey [days] [plan] [count]
    if sub == 'genkey':
        days  = int(args[1]) if len(args) > 1 else 30
        plan  = args[2]      if len(args) > 2 else 'standard'
        count = int(args[3]) if len(args) > 3 else 1
        keys  = keys_all()
        new_keys = []
        for _ in range(count):
            k = uuid.uuid4().hex[:20].upper()
            keys[k] = {'days': days, 'plan': plan, 'used': False}
            new_keys.append(k)
        keys_save(keys)
        key_list = '\n'.join(f'`{k}`' for k in new_keys)
        await update.message.reply_text(
            f"✅ Згенеровано {count} ключ(ів) [{plan}, {days}д]:\n\n{key_list}",
            parse_mode='Markdown',
        )
        return

    # keys — list all
    if sub == 'keys':
        keys = keys_all()
        lines = []
        for k, m in list(keys.items())[:50]:
            status = '✅ використаний' if m.get('used') else '🔑 активний'
            lines.append(f"`{k}` — {m.get('plan','?')}/{m.get('days','?')}д — {status}")
        await update.message.reply_text('\n'.join(lines) or 'Ключів немає.', parse_mode='Markdown')
        return

    # users — list active users
    if sub == 'users':
        users = users_all()
        lines = [
            f"ID:{uid} plan={u.get('plan','?')} exp={u.get('expires','?')[:10]} active={u.get('active')}"
            for uid, u in list(users.items())[:50]
        ]
        await update.message.reply_text('\n'.join(lines) or 'Користувачів немає.')
        return

    # revoke <user_id>
    if sub == 'revoke' and len(args) > 1:
        target = args[1]
        users = users_all()
        if target in users:
            users[target]['active'] = False
            users_save(users)
            await update.message.reply_text(f"✅ Доступ для {target} відкликано.")
        else:
            await update.message.reply_text("Користувача не знайдено.")
        return

    await update.message.reply_text(
        "Команди адміна:\n"
        "`/admin genkey [days] [plan] [count]`\n"
        "`/admin keys`\n"
        "`/admin users`\n"
        "`/admin revoke <user_id>`",
        parse_mode='Markdown',
    )

# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    _start_sign_server()

    token = os.getenv('BOT_TOKEN')
    if not token:
        raise RuntimeError('BOT_TOKEN not set in .env')

    app = ApplicationBuilder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler('start', cmd_start)],
        states={
            ST_KEY:             [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_key)],
            ST_MENU:            [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
            ST_PROFILE_PICK:    [
                CallbackQueryHandler(callback_profile),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu),
            ],
            ST_MANUAL_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_name)],
            ST_MANUAL_SURNAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_surname)],
            ST_MANUAL_MIDDLE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_middle)],
            ST_MANUAL_BIRTH:    [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_birth)],
            ST_MANUAL_INN:      [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_inn)],
            ST_MANUAL_PASSPORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_passport)],
            ST_SIGN_WAIT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_sign_wait)],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler('admin', cmd_admin))

    print("🤖 Bot polling...")
    await app.run_polling()


if __name__ == '__main__':
    asyncio.run(main())
