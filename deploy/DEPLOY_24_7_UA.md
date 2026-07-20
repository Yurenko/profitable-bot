# Запуск бота 24/7 (без Vercel)

## Чому Vercel не підходить

| Потреба | Vercel | VPS (Oracle / свій ПК) |
|---------|--------|-------------------------|
| Бот працює без відкритої вкладки | ❌ Ні | ✅ Так |
| Оновлення цін кожні 2–5 сек | ❌ Serverless «засинає» | ✅ Постійний процес |
| SQLite / логи зберігаються назавжди | ❌ Тільки `/tmp`, стирається | ✅ Файли на диску |
| WebSocket до Binance | ❌ Нестабільно | ✅ Так |

**Vercel** — для сайтів і коротких API. **Торговий бот** потрібно запускати на **завжди увімкненому сервері**.

---

## Що вже є в проєкті (база даних)

Бот **вже зберігає стан у SQLite** (`data/bot_state.sqlite`):

| Таблиця | Що зберігається |
|---------|-----------------|
| `positions` | Відкриті позиції: символ, qty, avg_entry, DCA level, grid meta |
| `orders` | Idempotent ордери (без дублікатів після рестарту) |
| `equity_snapshots` | Історія equity |
| `kv` | last_equity, bot_active тощо |

Аудит у `logs/audit.jsonl`: входи, DCA, TP, margin top-up, помилки.

Після перезапуску сервера бот **підхоплює позиції з SQLite** і продовжує.

---

## Рекомендована схема

```
┌─────────────────┐     ┌──────────────────┐
│  systemd        │     │  Binance API     │
│  python main.py │────▶│  (REST + WS)     │
│  paper / live   │     └──────────────────┘
└────────┬────────┘
         │ пише
         ▼
┌─────────────────┐     ┌──────────────────┐ (опційно)
│ bot_state.sqlite│◀────│ Dashboard        │
│ audit.jsonl     │     │ python main.py   │
└─────────────────┘     │ dashboard        │
                        └──────────────────┘
```

1. **Бот** — окремий процес 24/7 (`main.py paper` або `live`).
2. **Dashboard** — опційно, тільки для перегляду (не обов’язковий для торгівлі).

---

## Крок 1 — VPS (Oracle Cloud Free або свій ПК)

Oracle Always Free: Ubuntu 22.04, ~2 OCPU / 12 GB RAM.

Підключення:

```bash
ssh ubuntu@ВАШ_IP
```

---

## Крок 2 — Встановлення

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

git clone https://github.com/Yurenko/profitable-bot.git
cd profitable-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # EXCHANGE_API_KEY, EXCHANGE_API_SECRET
```

Перевірка одного циклу:

```bash
python main.py paper --once
```

Безперервний запуск (тест у SSH):

```bash
python main.py paper
# Ctrl+C — зупинка
```

---

## Крок 3 — systemd (автостарт після ребуту)

Скопіюй unit-файл (зміни шлях і користувача):

```bash
sudo cp deploy/systemd/trading-bot-paper.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-bot-paper.service
# WorkingDirectory=/home/ubuntu/profitable-bot
# User=ubuntu
# ExecStart=.../python main.py paper

sudo systemctl daemon-reload
sudo systemctl enable trading-bot-paper
sudo systemctl start trading-bot-paper
sudo systemctl status trading-bot-paper
```

Логи:

```bash
journalctl -u trading-bot-paper -f
tail -f logs/bot.log
tail -f logs/audit.jsonl
```

---

## Крок 4 — Dashboard (опційно, з іншого комп’ютера)

На VPS:

```bash
sudo cp deploy/systemd/trading-dashboard.service /etc/systemd/system/
# налаштуй шляхи
sudo systemctl enable --now trading-dashboard
```

Відкрий `http://IP:8080`. Бот уже крутиться в systemd — dashboard лише показує стан з SQLite.

**Не запускай бота кнопкою Paper на dashboard**, якщо вже працює systemd — буде два процеси.

---

## Як зрозуміти стан позиції

### Через SQLite

```bash
sqlite3 data/bot_state.sqlite "SELECT symbol, updated_at FROM positions;"
sqlite3 data/bot_state.sqlite "SELECT value FROM kv WHERE key='last_equity';"
```

### Через audit

```bash
tail -20 logs/audit.jsonl
```

Події: `bot_start`, `grid_planned`, `grid_fill`, `margin_topup`, TP/close тощо.

### У dashboard

Вкладки **Позиції** і **Угоди** читають ті самі файли.

---

## Live (testnet)

1. `exchange.testnet: true` у `config.yaml`
2. Ключі testnet у `.env`
3. Спочатку тиждень paper на VPS
4. Потім:

```bash
sudo systemctl stop trading-bot-paper
sudo cp deploy/systemd/trading-bot-live.service /etc/systemd/system/
# налаштуй і:
sudo systemctl enable --now trading-bot-live
```

---

## Windows 24/7 (домашній ПК)

1. `scripts\run_paper.bat` — не закривай вікно, або
2. Task Scheduler: запуск при старті Windows
3. Або NSSM — служба Windows для `python main.py paper`

---

## Підсумок

| Задача | Рішення |
|--------|---------|
| 24/7 без вкладки | VPS + `python main.py paper` + systemd |
| Дані не губляться | SQLite на диску VPS (не Vercel) |
| Входи / DCA / TP | Цикл `bot.py` кожні `poll_interval_sec` |
| Моніторинг | Dashboard або `audit.jsonl` |

**Vercel залиш для демо-UI або прибери.** Для реальної роботи — тільки VPS.
