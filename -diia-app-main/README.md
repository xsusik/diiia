# Дія-бот — Cloudflare Workers + Netlify

## Архітектура

```
Telegram ──webhook──▶ Cloudflare Worker (bot logic + sign API)
                              │
                         Cloudflare KV
                       USERS_KV / KEYS_KV / SIGNS_KV
                              │
User browser ◀── Netlify ────┘
   index.html   (Diia document, reads #profile= hash)
   sign.html    (canvas for signature → POSTs to Worker)
```

---

## 1. Cloudflare — підготовка

### 1.1 Встанови Wrangler
```bash
cd worker
npm install
```

### 1.2 Авторизуйся
```bash
npx wrangler login
```

### 1.3 Створи три KV namespace
```bash
npm run kv:create:users   # → скопіюй id у wrangler.toml → USERS_KV
npm run kv:create:keys    # → скопіюй id → KEYS_KV
npm run kv:create:signs   # → скопіюй id → SIGNS_KV
```

Встав скопійовані `id` у [`worker/wrangler.toml`](worker/wrangler.toml).

### 1.4 Додай secrets
```bash
npm run secret:token    # вставиш токен бота (від @BotFather)
npm run secret:admin    # вставиш свій Telegram ID
npm run secret:hook     # придумай будь-який рядок, напр. "my_secret_42"
```

### 1.5 Задеплой воркер
```bash
npm run deploy
```

Скопіюй URL воркера (вигляд: `https://diia-bot-worker.<твій-субдомен>.workers.dev`).

### 1.6 Зареєструй webhook
Відкрий у браузері:
```
https://diia-bot-worker.<субдомен>.workers.dev/setup?secret=<WEBHOOK_SECRET>
```
Має повернути `{"ok":true,"result":true,...}`.

---

## 2. Netlify — деплой фронтенду

### 2.1 Вкажи Worker URL у sign.html
Відкрий [`sign.html`](sign.html), знайди рядок:
```html
<meta name="worker-url" content="/api">
```
*(або додай у `<head>` якщо ще немає)*  
Заміни `/api` на URL воркера:
```html
<meta name="worker-url" content="https://diia-bot-worker.<субдомен>.workers.dev/api">
```

### 2.2 Вкажи Netlify origin у Worker
У Cloudflare Dashboard → Workers → diia-bot-worker → Settings → Variables:
```
NETLIFY_ORIGIN = https://YOUR-SITE.netlify.app
```

### 2.3 Задеплой на Netlify
- Зайди на [netlify.com](https://netlify.com) → **Add new site → Import an existing project**
- Підключи GitHub репо (або перетягни папку через **Deploy manually**)
- **Publish directory**: `.` (корінь)
- Деплой

Netlify видасть URL вигляду `https://ваш-сайт.netlify.app`.  
Вкажи його як `NETLIFY_ORIGIN` у Workers.

---

## 3. Завантаж профілі у KV

В Telegram напиши боту `/admin`, потім:
1. Пересли повідомлення з JSON-масивом профілів (вміст `profiles.json`)
2. Reply на нього командою `/admin seed`

Або через Wrangler CLI:
```bash
npx wrangler kv:key put --binding=USERS_KV "__profiles__" "$(cat profiles.json)"
```

---

## 4. Генерація ключів для продажу

```
/admin genkey 30 standard 10
```
→ генерує 10 ключів на 30 днів тарифу `standard`.

Список ключів:
```
/admin keys
```

Відкликати доступ:
```
/admin revoke <user_id>
```

---

## 5. Як це працює для покупця

1. Купує ключ → отримує рядок типу `A1B2C3D4E5F6G7H8I9J0`
2. Пише боту `/start` → вводить ключ → доступ активовано
3. Обирає **📋 Готові профілі** → вибирає → профіль встановлено
4. **✍️ Намалювати підпис** → посилання на `sign.html` → малює → повертається → тисне *Підпис готовий*
5. **📄 Показати документ** → посилання на `index.html#profile=...` → Diia-документ з фото + підписом

---

## Файлова структура

```
.
├── index.html          ← Diia document (Netlify)
├── sign.html           ← Signature canvas (Netlify)
├── profiles.json       ← Seed data for KV
├── sw.js               ← Service worker
├── netlify.toml        ← Netlify config
└── worker/
    ├── wrangler.toml   ← Cloudflare config (fill in KV ids)
    ├── package.json
    └── src/
        └── index.js    ← Worker: bot logic + sign API
```
