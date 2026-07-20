# Як запустити проєкт (українською)

Торговий бот для **USDT-M perpetual futures** (Binance / Bybit через CCXT).  
Стратегія: long-only mean-reversion + DCA, 5x isolated, без класичного SL.

---

## 1. Перше встановлення (Windows)

Відкрийте PowerShell або CMD у папці проєкту `bot-1`:

```bat
scripts\setup.bat
```

Або вручну:

```powershell
cd d:\Yura\ТЗ\bot-1
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

**Потрібно:** Python 3.10+ і інтернет.

---

## 2. Налаштування

### Файл `.env` (для paper/live з біржею)

```env
EXCHANGE_API_KEY=ваш_ключ
EXCHANGE_API_SECRET=ваш_секрет
```

Для **paper** ключі не обов’язкові (публічні дані з біржі).  
Для **live/testnet** — обов’язкові.

### Файл `config.yaml`

| Параметр | Значення за замовчуванням | Опис |
|----------|---------------------------|------|
| `exchange.id` | `binanceusdm` | Біржа (`bybit` — теж можна) |
| `exchange.testnet` | `true` | **Залишайте true** до повної перевірки |
| `symbols` | BTC, ETH | Пари для торгівлі |
| `strategy.leverage` | 5 | Плече |
| `strategy.max_dca_levels` | 4 | Макс. рівні DCA |

---

## 3. Порядок запуску (рекомендований)

### 🖥️ Найпростіший спосіб — Dashboard (GUI)

**Подвійний клік** на файл у корені проєкту:

```
Запуск Dashboard.bat
```

Або в терміналі:

```powershell
python main.py dashboard
```

Відкриється браузер на **http://127.0.0.1:8080** з сучасним інтерфейсом:

| Розділ | Що можна робити |
|--------|-----------------|
| **Огляд** | Equity, PnL, статус бота, графік |
| **Позиції** | Відкриті позиції |
| **Угоди** | Аудит + логи |
| **Бектест** | Запуск однією кнопкою |
| **Налаштування** | TP %, RSI, DCA, ризик, плече — зберегти в config.yaml |

Кнопки зверху: **▶ Paper**, **▶ Live**, **■ Стоп**, **1 тік**.

Працює на **десктопі та мобільному** (адаптивний дизайн).

> Закрийте вікно терміналу або натисніть Ctrl+C — dashboard зупиниться.

### Крок 1 — Тести

```powershell
.\.venv\Scripts\activate
pytest -v
```

або `scripts\run_tests.bat`

### Крок 2 — Бектест

```powershell
python main.py backtest --monte-carlo
```

Короткий тест (швидше):

```powershell
python main.py backtest --bars 25000 --monte-carlo
```

або `scripts\run_backtest.bat`

**Реальні 5 років 1m даних** (довго, години):

```powershell
python -m backtest.download_history --since 2021-01-01T00:00:00+00:00 --out data/historical/BTCUSDT_1m.parquet
python main.py backtest --monte-carlo
```

### Крок 3 — Walk-forward оптимізація

```powershell
python main.py walk-forward --train-days 180 --test-days 60
```

### Крок 4 — Paper trading (симуляція)

Один цикл (перевірка):

```powershell
python main.py paper --once
```

Безперервно (до Ctrl+C):

```powershell
python main.py paper
```

або `scripts\run_paper.bat`

- Ринкові дані — з біржі (CCXT)  
- Угоди — симульовані, з комісіями  
- Стан зберігається в `data/bot_state.sqlite`  
- Логи: `logs/bot.log`, аудит: `logs/audit.jsonl`

### Крок 5 — Live (тільки testnet спочатку!)

1. Створіть API ключі на **Binance Futures Testnet**  
2. Заповніть `.env`  
3. Переконайтесь: `exchange.testnet: true` в `config.yaml`

```powershell
python main.py live --once    # один цикл
python main.py live           # цикл до зупинки
```

Mainnet (небезпечно):

```powershell
python main.py live --i-understand-mainnet-risk
```

---

## 4. Усі команди CLI

| Команда | Що робить |
|---------|-----------|
| `python main.py backtest` | Бектест |
| `python main.py backtest --monte-carlo` | + Monte Carlo |
| `python main.py backtest --bars 15000` | Обмежити кількість свічок |
| `python main.py walk-forward` | Walk-forward оптимізація |
| `python main.py paper` | Paper trading |
| `python main.py paper --once` | Один тік |
| `python main.py live` | Реальні угоди (testnet) |
| `python main.py --config інший.yaml backtest` | Інший конфіг |

---

## 5. Структура проєкту

```
bot-1/
├── main.py              ← точка входу
├── config.yaml          ← параметри стратегії
├── .env                 ← API ключі (не комітити!)
├── src/
│   ├── strategy.py      ← логіка входу/DCA/TP
│   ├── risk_manager.py  ← ризик, розмір позиції
│   └── bot.py           ← головний цикл
├── backtest/
│   └── backtest.py      ← бектестер
├── tests/               ← pytest
├── data/
│   ├── bot_state.sqlite ← збережені позиції
│   └── historical/      ← сюди класти parquet/csv
└── logs/
```

---

## 6. Що вже зроблено / що ще вручну

| Готово | Потрібно з вашого боку |
|--------|------------------------|
| Стратегія, DCA, TP, trailing | Завантажити реальні 5y дані для серйозного бектесту |
| Ризик-менеджмент, circuit breaker | Налаштувати параметри під свій ризик |
| SQLite + audit trail | Регулярно оновлювати `data/economic_calendar.json` |
| Paper + live (CCXT testnet) | Спочатку paper → testnet → лише потім mainnet |
| 21 unit-тест | |
| Monte Carlo + walk-forward | |
| Календар новин (JSON) | За бажанням — підключити зовнішній API календаря |

**Не реалізовано навмисно / обмеження:**

- Немає live-скрейпера Investing.com (тільки JSON-кеш + шаблонні події)
- Немає vectorbt/backtrader — свій бектестер
- Синтетичний бектест ≠ гарантія прибутку на реальному ринку

---

## 7. Типові проблеми

| Проблема | Рішення |
|----------|---------|
| `No module named 'ccxt'` | `pip install -r requirements.txt` |
| Live: немає ключів | Заповніть `.env` |
| Paper падає з мережею | Перевірте інтернет / VPN |
| Бектест без файлів | Використовує синтетичні дані — завантажте parquet |
| Кодування в консолі | `chcp 65001` або PowerShell 7 |

---

## 8. Безпека

- Ніколи не комітьте `.env`  
- Тримайте `testnet: true` до повної перевірки  
- Починайте з мінімального капіталу  
- Торгівля криптою = високий ризик втрат

---

## 9. Запуск 24/7 (без вкладки браузера)

**Vercel не підходить** для постійної торгівлі: процес «засинає», файли в `/tmp` стираються.

Для роботи **цілодобово** (входи, DCA, TP, оновлення цін):

1. VPS (Oracle Cloud Free) або свій ПК
2. `python main.py paper` через **systemd** (не через кнопку в dashboard)
3. Стан уже в **SQLite**: `data/bot_state.sqlite` + `logs/audit.jsonl`

Покрокова інструкція: **[deploy/DEPLOY_24_7_UA.md](deploy/DEPLOY_24_7_UA.md)**  
Деплой на **AWS EC2**: **[deploy/DEPLOY_AWS_UA.md](deploy/DEPLOY_AWS_UA.md)**

Повна документація англійською: [README.md](README.md)
