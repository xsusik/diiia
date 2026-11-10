// ─── Cloudflare Worker — Дія Telegram Bot ─────────────────────────────────
// KV bindings: USERS_KV, KEYS_KV, SIGNS_KV
// Secrets:     BOT_TOKEN, ADMIN_ID, WEBHOOK_SECRET
// Env vars:    NETLIFY_ORIGIN

const TG = (token, method, body) =>
  fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => r.json());

// ─── KV helpers ──────────────────────────────────────────────────────────────

async function kvGet(ns, key, def = null) {
  const v = await ns.get(key, { type: 'json' });
  return v ?? def;
}

async function kvPut(ns, key, val) {
  await ns.put(key, JSON.stringify(val));
}

// ─── Access control ───────────────────────────────────────────────────────────

async function userHasAccess(env, uid) {
  const u = await kvGet(env.USERS_KV, String(uid), {});
  if (!u.active) return false;
  if (u.expires && new Date(u.expires) < new Date()) {
    u.active = false;
    await kvPut(env.USERS_KV, String(uid), u);
    return false;
  }
  return true;
}

async function activateUser(env, uid, meta) {
  const days = meta.days ?? 30;
  const expires = new Date(Date.now() + days * 86400000).toISOString();
  const existing = await kvGet(env.USERS_KV, String(uid), {});
  await kvPut(env.USERS_KV, String(uid), {
    ...existing,
    active: true,
    activated_at: new Date().toISOString(),
    expires,
    plan: meta.plan ?? 'standard',
  });
}

async function consumeKey(env, key, uid) {
  const meta = await kvGet(env.KEYS_KV, key.toUpperCase());
  if (!meta || meta.used) return null;
  meta.used = true;
  meta.used_by = uid;
  meta.used_at = new Date().toISOString();
  await kvPut(env.KEYS_KV, key.toUpperCase(), meta);
  return meta;
}

// ─── User profile ─────────────────────────────────────────────────────────────

async function getUserProfile(env, uid) {
  const u = await kvGet(env.USERS_KV, String(uid), {});
  return u.profile ?? null;
}

async function saveUserProfile(env, uid, profile) {
  const u = await kvGet(env.USERS_KV, String(uid), {});
  u.profile = profile;
  await kvPut(env.USERS_KV, String(uid), u);
}

// ─── State machine (stored per user in KV) ────────────────────────────────────
// States:
const ST = {
  KEY:              'KEY',
  MENU:             'MENU',
  PROFILE_PICK:     'PROFILE_PICK',
  MANUAL_NAME:      'MANUAL_NAME',
  MANUAL_SURNAME:   'MANUAL_SURNAME',
  MANUAL_MIDDLE:    'MANUAL_MIDDLE',
  MANUAL_BIRTH:     'MANUAL_BIRTH',
  MANUAL_INN:       'MANUAL_INN',
  MANUAL_PASSPORT:  'MANUAL_PASSPORT',
  SIGN_WAIT:        'SIGN_WAIT',
};

async function getState(env, uid) {
  return await kvGet(env.USERS_KV, `state:${uid}`, { step: ST.KEY, data: {} });
}

async function setState(env, uid, step, data = {}) {
  await kvPut(env.USERS_KV, `state:${uid}`, { step, data });
}

// ─── Keyboard builders ────────────────────────────────────────────────────────

const MAIN_KB = {
  keyboard: [
    ['📋 Готові профілі', '✍️ Намалювати підпис'],
    ['👤 Мій профіль',    '📄 Показати документ'],
    ['ℹ️ Статус ключа'],
  ],
  resize_keyboard: true,
};

const SIGN_KB = {
  keyboard: [['✅ Підпис готовий'], ['❌ Скасувати']],
  resize_keyboard: true,
};

// ─── Profile text ─────────────────────────────────────────────────────────────

function profileText(p) {
  const sig = p.signature ? '✅ є' : '⬜ не встановлено';
  return (
    `👤 *${p.lastName ?? ''} ${p.firstName ?? ''} ${p.middleName ?? ''}*\n` +
    `📅 Дата народження: \`${p.birth ?? '—'}\`\n` +
    `🔢 ІПН: \`${p.rnokpp ?? '—'}\`\n` +
    `🪪 Паспорт: \`${p.passport ?? '—'}\`\n` +
    `✍️ Підпис: ${sig}`
  );
}

// ─── Profiles list (from KV, populated once from profiles.json via /admin seed) ─

async function getAllProfiles(env) {
  return await kvGet(env.USERS_KV, '__profiles__', []);
}

// ─── Inline keyboards for profile picking ─────────────────────────────────────

function profilePageKeyboard(profiles, page) {
  const pageSize = 5;
  const start = page * pageSize;
  const chunk = profiles.slice(start, start + pageSize);
  const rows = chunk.map((p, i) => [{
    text: `${p.lastName ?? ''} ${p.firstName ?? ''} | ${p.birth ?? ''}`,
    callback_data: `pick_${start + i}`,
  }]);
  const nav = [];
  if (page > 0)                           nav.push({ text: '⬅️ Назад', callback_data: 'page_prev' });
  if (start + pageSize < profiles.length) nav.push({ text: '➡️ Далі',  callback_data: 'page_next' });
  if (nav.length) rows.push(nav);
  rows.push([{ text: '✏️ Ввести вручну', callback_data: 'manual_enter' }]);
  return { inline_keyboard: rows };
}

// ─── Send helpers ─────────────────────────────────────────────────────────────

function send(env, chat_id, text, extra = {}) {
  return TG(env.BOT_TOKEN, 'sendMessage', {
    chat_id,
    text,
    parse_mode: 'Markdown',
    ...extra,
  });
}

function edit(env, chat_id, message_id, text, extra = {}) {
  return TG(env.BOT_TOKEN, 'editMessageText', {
    chat_id,
    message_id,
    text,
    parse_mode: 'Markdown',
    ...extra,
  });
}

// ─── Main message handler ─────────────────────────────────────────────────────

async function handleMessage(env, msg) {
  const uid  = msg.from.id;
  const chat = msg.chat.id;
  const text = (msg.text ?? '').trim();

  // /start always resets
  if (text === '/start') {
    if (await userHasAccess(env, uid)) {
      await setState(env, uid, ST.MENU);
      await send(env, chat, '🇺🇦 *Дія-бот*\nОберіть дію:', { reply_markup: MAIN_KB });
    } else {
      await setState(env, uid, ST.KEY);
      await send(env, chat, '🔑 *Введіть ліцензійний ключ* для активації доступу:\n_(Ключ видається при купівлі)_');
    }
    return;
  }

  // /cancel
  if (text === '/cancel') {
    await setState(env, uid, ST.MENU);
    await send(env, chat, 'Скасовано.', { reply_markup: MAIN_KB });
    return;
  }

  const { step, data } = await getState(env, uid);

  // ── KEY ──────────────────────────────────────────────────────────────────
  if (step === ST.KEY) {
    const meta = await consumeKey(env, text, uid);
    if (!meta) {
      await send(env, chat, '❌ Ключ недійсний або вже використаний. Спробуйте інший.');
      return;
    }
    await activateUser(env, uid, meta);
    await setState(env, uid, ST.MENU);
    await send(env, chat,
      `✅ *Доступ активовано!*\nТариф: \`${meta.plan ?? 'standard'}\`\nТермін: \`${meta.days ?? 30}\` днів\n\nОберіть дію:`,
      { reply_markup: MAIN_KB },
    );
    return;
  }

  // Guard: все решта вимагає доступу
  if (!(await userHasAccess(env, uid))) {
    await setState(env, uid, ST.KEY);
    await send(env, chat, '🔑 Доступ закінчився. Введіть /start і активуйте новий ключ.');
    return;
  }

  // ── MENU ─────────────────────────────────────────────────────────────────
  if (step === ST.MENU) {
    if (text === '📋 Готові профілі') {
      const profiles = await getAllProfiles(env);
      if (!profiles.length) {
        await send(env, chat, 'Готових профілів немає. Оберіть *Ввести вручну*.', { reply_markup: MAIN_KB });
        return;
      }
      await setState(env, uid, ST.PROFILE_PICK, { page: 0 });
      await send(env, chat, `Оберіть профіль (${profiles.length} шт.) або введіть вручну:`, {
        reply_markup: profilePageKeyboard(profiles, 0),
      });
      return;
    }

    if (text === '✍️ Намалювати підпис') {
      const token = crypto.randomUUID().replace(/-/g, '');
      await env.SIGNS_KV.put(`pending:${token}`, String(uid), { expirationTtl: 600 });
      await setState(env, uid, ST.SIGN_WAIT, { token });
      const origin = env.NETLIFY_ORIGIN ?? 'https://YOUR-SITE.netlify.app';
      const url = `${origin}/sign.html?t=${token}`;
      await send(env, chat,
        `✍️ *Намалюйте підпис* за посиланням:\n\n${url}\n\nПісля збереження натисніть кнопку нижче.`,
        { reply_markup: SIGN_KB },
      );
      return;
    }

    if (text === '👤 Мій профіль') {
      const p = await getUserProfile(env, uid);
      await send(env, chat, p ? profileText(p) : 'У вас ще немає профілю. Оберіть *Готові профілі*.', { reply_markup: MAIN_KB });
      return;
    }

    if (text === '📄 Показати документ') {
      const p = await getUserProfile(env, uid);
      if (!p) {
        await send(env, chat, 'Спочатку оберіть профіль (📋 Готові профілі).', { reply_markup: MAIN_KB });
        return;
      }
      const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(p))));
      const origin = env.NETLIFY_ORIGIN ?? 'https://YOUR-SITE.netlify.app';
      const url = `${origin}/index.html#profile=${encoded}`;
      await send(env, chat, `📄 *Відкрийте документ Дії:*\n\n${url}`, { reply_markup: MAIN_KB });
      return;
    }

    if (text === 'ℹ️ Статус ключа') {
      const u = await kvGet(env.USERS_KV, String(uid), {});
      await send(env, chat,
        `📋 *Статус*\nТариф: \`${u.plan ?? '—'}\`\nДійсний до: \`${(u.expires ?? '—').slice(0, 10)}\``,
        { reply_markup: MAIN_KB },
      );
      return;
    }
    return;
  }

  // ── SIGN_WAIT ─────────────────────────────────────────────────────────────
  if (step === ST.SIGN_WAIT) {
    if (text === '❌ Скасувати') {
      if (data.token) await env.SIGNS_KV.delete(`pending:${data.token}`);
      await setState(env, uid, ST.MENU);
      await send(env, chat, 'Скасовано.', { reply_markup: MAIN_KB });
      return;
    }
    // Check if signature arrived
    const img = await env.SIGNS_KV.get(`result:${data.token}`);
    if (!img) {
      await send(env, chat, '⏳ Підпис ще не отримано. Перейдіть за посиланням, намалюйте і збережіть.');
      return;
    }
    // Save into profile
    const profile = (await getUserProfile(env, uid)) ?? {};
    profile.signature = img;
    await saveUserProfile(env, uid, profile);
    await env.SIGNS_KV.delete(`pending:${data.token}`);
    await env.SIGNS_KV.delete(`result:${data.token}`);
    await setState(env, uid, ST.MENU);
    await send(env, chat, '✅ Підпис збережено у профіль і в документ Дії!', { reply_markup: MAIN_KB });
    return;
  }

  // ── PROFILE_PICK fallback ─────────────────────────────────────────────────
  if (step === ST.PROFILE_PICK) {
    // Handled by callback; text just bounces back to menu
    await setState(env, uid, ST.MENU);
    await send(env, chat, 'Оберіть дію:', { reply_markup: MAIN_KB });
    return;
  }

  // ── MANUAL entry states ───────────────────────────────────────────────────
  const manualSteps = {
    [ST.MANUAL_NAME]:     { field: 'firstName',  next: ST.MANUAL_SURNAME,  prompt: 'Введіть *прізвище*:' },
    [ST.MANUAL_SURNAME]:  { field: 'lastName',   next: ST.MANUAL_MIDDLE,   prompt: 'Введіть *по батькові*:' },
    [ST.MANUAL_MIDDLE]:   { field: 'middleName', next: ST.MANUAL_BIRTH,    prompt: 'Дата народження (DD.MM.YYYY):' },
    [ST.MANUAL_BIRTH]:    { field: 'birth',      next: ST.MANUAL_INN,      prompt: 'Введіть *ІПН* (10 цифр):',
      validate: v => /^\d{2}\.\d{2}\.\d{4}$/.test(v),
      errMsg: 'Невірний формат. Введіть DD.MM.YYYY:',
    },
    [ST.MANUAL_INN]:      { field: 'rnokpp',     next: ST.MANUAL_PASSPORT, prompt: 'Введіть *номер паспорту*:',
      validate: v => /^\d{10}$/.test(v),
      errMsg: 'ІПН — рівно 10 цифр. Спробуйте ще:',
    },
  };

  if (manualSteps[step]) {
    const cfg = manualSteps[step];
    if (cfg.validate && !cfg.validate(text)) {
      await send(env, chat, cfg.errMsg);
      return;
    }
    const updated = { ...data, [cfg.field]: text };
    await setState(env, uid, cfg.next, updated);
    await send(env, chat, cfg.prompt);
    return;
  }

  if (step === ST.MANUAL_PASSPORT) {
    const profile = {
      ...data,
      passport: text,
      id: `p_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`,
    };
    // Preserve existing signature
    const existing = await getUserProfile(env, uid);
    if (existing?.signature) profile.signature = existing.signature;
    await saveUserProfile(env, uid, profile);
    await setState(env, uid, ST.MENU);
    await send(env, chat, `✅ Профіль збережено!\n\n${profileText(profile)}`, { reply_markup: MAIN_KB });
    return;
  }
}

// ─── Callback query handler ───────────────────────────────────────────────────

async function handleCallback(env, cq) {
  const uid  = cq.from.id;
  const chat = cq.message.chat.id;
  const mid  = cq.message.message_id;
  const cbData = cq.data;

  await TG(env.BOT_TOKEN, 'answerCallbackQuery', { callback_query_id: cq.id });

  if (!(await userHasAccess(env, uid))) {
    await edit(env, chat, mid, '🔑 Доступ закінчився. /start');
    return;
  }

  const { data } = await getState(env, uid);

  if (cbData === 'page_next') {
    const page = (data.page ?? 0) + 1;
    await setState(env, uid, ST.PROFILE_PICK, { ...data, page });
    const profiles = await getAllProfiles(env);
    await edit(env, chat, mid, `Оберіть профіль (${profiles.length} шт.) або введіть вручну:`, {
      reply_markup: profilePageKeyboard(profiles, page),
    });
    return;
  }

  if (cbData === 'page_prev') {
    const page = Math.max(0, (data.page ?? 0) - 1);
    await setState(env, uid, ST.PROFILE_PICK, { ...data, page });
    const profiles = await getAllProfiles(env);
    await edit(env, chat, mid, `Оберіть профіль (${profiles.length} шт.) або введіть вручну:`, {
      reply_markup: profilePageKeyboard(profiles, page),
    });
    return;
  }

  if (cbData === 'manual_enter') {
    await setState(env, uid, ST.MANUAL_NAME, {});
    await edit(env, chat, mid, "Введіть *ім'я* (по-українськи):");
    return;
  }

  if (cbData.startsWith('pick_')) {
    const idx = parseInt(cbData.split('_')[1]);
    const profiles = await getAllProfiles(env);
    if (idx >= profiles.length) {
      await edit(env, chat, mid, 'Профіль не знайдено.');
      return;
    }
    const chosen = { ...profiles[idx] };
    const existing = await getUserProfile(env, uid);
    if (existing?.signature) chosen.signature = existing.signature;
    await saveUserProfile(env, uid, chosen);
    await setState(env, uid, ST.MENU);
    await edit(env, chat, mid, `✅ Профіль встановлено!\n\n${profileText(chosen)}`);
    await TG(env.BOT_TOKEN, 'sendMessage', {
      chat_id: chat,
      text: 'Оберіть дію:',
      reply_markup: MAIN_KB,
    });
    return;
  }
}

// ─── Admin commands ───────────────────────────────────────────────────────────

async function handleAdmin(env, msg) {
  const uid  = msg.from.id;
  const chat = msg.chat.id;
  if (String(uid) !== String(env.ADMIN_ID)) return;

  const parts = (msg.text ?? '').split(/\s+/);
  const sub   = parts[1] ?? '';

  // /admin genkey [days] [plan] [count]
  if (sub === 'genkey') {
    const days  = parseInt(parts[2]) || 30;
    const plan  = parts[3] || 'standard';
    const count = parseInt(parts[4]) || 1;
    const keys  = [];
    for (let i = 0; i < count; i++) {
      const k = crypto.randomUUID().replace(/-/g, '').slice(0, 20).toUpperCase();
      await kvPut(env.KEYS_KV, k, { days, plan, used: false });
      keys.push(k);
    }
    await send(env, chat,
      `✅ Згенеровано ${count} ключ(ів) [${plan}, ${days}д]:\n\n${keys.map(k => `\`${k}\``).join('\n')}`,
    );
    return;
  }

  // /admin revoke <user_id>
  if (sub === 'revoke' && parts[2]) {
    const target = parts[2];
    const u = await kvGet(env.USERS_KV, target, null);
    if (!u) { await send(env, chat, 'Користувача не знайдено.'); return; }
    u.active = false;
    await kvPut(env.USERS_KV, target, u);
    await send(env, chat, `✅ Доступ для ${target} відкликано.`);
    return;
  }

  // /admin seed — uploads profiles.json array into KV
  // Usage: send command, then reply with profiles JSON (or just call with inline JSON after)
  if (sub === 'seed' && msg.reply_to_message?.text) {
    try {
      const arr = JSON.parse(msg.reply_to_message.text);
      await kvPut(env.USERS_KV, '__profiles__', arr);
      await send(env, chat, `✅ Завантажено ${arr.length} профілів у KV.`);
    } catch {
      await send(env, chat, '❌ Невалідний JSON.');
    }
    return;
  }

  await send(env, chat,
    'Команди адміна:\n' +
    '`/admin genkey [days] [plan] [count]`\n' +
    '`/admin revoke <user_id>`\n' +
    '`/admin seed` _(reply to message with JSON array)_',
  );
}

// ─── Signature API endpoint ───────────────────────────────────────────────────
// POST /api/sign?t=<token>   body: { img: "data:image/png;base64,..." }

async function handleSignApi(env, request) {
  const url   = new URL(request.url);
  const token = url.searchParams.get('t');

  if (!token) return new Response('Bad request', { status: 400 });

  const uidStr = await env.SIGNS_KV.get(`pending:${token}`);
  if (!uidStr) return new Response('Invalid or expired token', { status: 404 });

  const body = await request.json().catch(() => null);
  const img  = body?.img ?? '';
  if (!img.startsWith('data:image')) return new Response('Bad image', { status: 400 });

  // Store result for the bot to pick up (TTL 10 min)
  await env.SIGNS_KV.put(`result:${token}`, img, { expirationTtl: 600 });

  return new Response('ok', {
    status: 200,
    headers: corsHeaders(env),
  });
}

// ─── CORS headers ─────────────────────────────────────────────────────────────

function corsHeaders(env) {
  const origin = env.NETLIFY_ORIGIN ?? '*';
  return {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
}

// ─── Webhook registration helper ──────────────────────────────────────────────
// GET /setup?secret=<WEBHOOK_SECRET>  — registers the webhook with Telegram

async function handleSetup(env, request) {
  const url    = new URL(request.url);
  const secret = url.searchParams.get('secret');
  if (secret !== env.WEBHOOK_SECRET) return new Response('Forbidden', { status: 403 });

  const workerUrl = `${url.origin}/webhook`;
  const result = await TG(env.BOT_TOKEN, 'setWebhook', {
    url: workerUrl,
    secret_token: env.WEBHOOK_SECRET,
    allowed_updates: ['message', 'callback_query'],
  });
  return new Response(JSON.stringify(result), {
    headers: { 'Content-Type': 'application/json' },
  });
}

// ─── Main fetch handler ───────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(env) });
    }

    // Webhook registration
    if (url.pathname === '/setup') {
      return handleSetup(env, request);
    }

    // Signature API
    if (url.pathname === '/api/sign' && request.method === 'POST') {
      return handleSignApi(env, request);
    }

    // Telegram webhook
    if (url.pathname === '/webhook' && request.method === 'POST') {
      // Verify secret
      const incoming = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
      if (env.WEBHOOK_SECRET && incoming !== env.WEBHOOK_SECRET) {
        return new Response('Forbidden', { status: 403 });
      }

      const update = await request.json().catch(() => null);
      if (!update) return new Response('Bad request', { status: 400 });

      try {
        if (update.message) {
          const text = update.message.text ?? '';
          if (text.startsWith('/admin')) {
            await handleAdmin(env, update.message);
          } else {
            await handleMessage(env, update.message);
          }
        } else if (update.callback_query) {
          await handleCallback(env, update.callback_query);
        }
      } catch (e) {
        console.error('Handler error:', e);
      }

      return new Response('ok');
    }

    return new Response('Not found', { status: 404 });
  },
};

