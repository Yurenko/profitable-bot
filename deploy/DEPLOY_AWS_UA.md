# Деплой бота 24/7 на AWS EC2

## EC2 чи S3?

| Сервіс | Для чого | Потрібен вам? |
|--------|----------|---------------|
| **EC2** | Віртуальний сервер (Linux), постійний процес Python | **Так — обов’язково** |
| **S3** | Сховище файлів (backup, архіви) | Ні (на старті) |
| **Lambda** | Короткі serverless-функції | Ні (як Vercel — не підходить) |
| **RDS** | PostgreSQL/MySQL у хмарі | Ні (у вас уже SQLite на диску EC2) |

**Висновок:** створюєте **один EC2 інстанс** з Ubuntu. Бот, SQLite і логи живуть на його диску. S3 можна додати пізніше лише для резервних копій.

---

## Що буде в результаті

```
Ваш ПК (Windows) ──SSH──▶ EC2 (Ubuntu)
                              │
                              ├─ systemd → python main.py paper  (24/7)
                              ├─ data/bot_state.sqlite
                              ├─ logs/audit.jsonl
                              └─ (опційно) dashboard :8080
```

---

## Частина 1 — Реєстрація AWS (якщо акаунта немає)

### Крок 1.1 — Створити акаунт

1. Відкрийте [https://aws.amazon.com/](https://aws.amazon.com/)
2. **Create an AWS Account**
3. Вкажіть email, пароль, ім’я акаунта
4. Оберіть **Personal** (особистий)
5. Додайте **банківську картку** — AWS перевіряє її (~$1 hold, повертається)
6. Підтвердіть телефон (SMS)
7. Оберіть план **Basic Support (Free)**

> Без картки акаунт не активують. Free Tier не означає «без картки» — це ліміти, після яких може з’явитися плата.

### Крок 1.2 — Увімкнути billing alerts (обов’язково)

1. У консолі AWS: **Billing and Cost Management** → **Billing preferences**
2. Увімкніть **Receive Free Tier Usage Alerts**
3. **Budgets** → **Create budget** → **Zero spend budget** або ліміт **$5/міс**
4. Email для сповіщень

### Крок 1.3 — Увійти в консоль

- [https://console.aws.amazon.com/](https://console.aws.amazon.com/)
- Регіон (правий верхній кут): **Europe (Frankfurt) `eu-central-1`** або **Stockholm `eu-north-1`** — ближче до України, менша затримка до Binance.

> **Критично:** не ставте EC2 у **США** (`us-east-1`, `us-west-2` тощо). Binance повертає **451 Restricted location** і API (баланс, ордери) не працюватиме. Потрібен регіон EU / Asia (Frankfurt, Ireland, Singapore, Tokyo).

---

## Частина 2 — Створити EC2

### Крок 2.1 — Launch instance

1. Консоль → **EC2** → **Launch instance**
2. Параметри:

| Поле | Значення |
|------|----------|
| Name | `trading-bot` |
| AMI | **Ubuntu Server 22.04 LTS** (Free tier eligible) |
| Instance type | **t2.micro** або **t3.micro** (Free tier) |
| Key pair | **Create new key pair** → ім’я `trading-bot-key`, тип `.pem`, зберегти файл |
| Network | Default VPC |
| Storage | **20–30 GB** gp3 (Free tier: до 30 GB) |

3. **Security group** — створити нову:

| Type | Port | Source | Навіщо |
|------|------|--------|--------|
| SSH | 22 | **My IP** | Підключення з вашого ПК |
| Custom TCP | 8080 | **0.0.0.0/0** | Dashboard з будь-якої IP (з паролем) |

> Порт **8080** можна відкрити для всіх (`0.0.0.0/0`), якщо в `.env` задано **`DASHBOARD_PASSWORD`**.
> Без пароля доступ до dashboard не захищений — не робіть так у публічній мережі.

4. **Launch instance**
5. Запишіть **Public IPv4 address** (наприклад `3.75.123.45`)

### Крок 2.2 — Elastic IP (рекомендовано)

Без Elastic IP публічний IP зміниться після stop/start.

1. EC2 → **Elastic IPs** → **Allocate**
2. **Associate** → ваш інстанс `trading-bot`

> Перший Elastic IP безкоштовний, поки прив’язаний до **запущеного** інстанса.

---

## Частина 3 — Підключення з Windows

### Крок 3.1 — Підготувати `.pem` ключ

1. Файл `trading-bot-key.pem` покласти, наприклад, у `C:\Users\ВашІмʼя\.ssh\`
2. PowerShell (один раз — обмежити доступ до ключа):

```powershell
icacls "$env:USERPROFILE\.ssh\trading-bot-key.pem" /inheritance:r
icacls "$env:USERPROFILE\.ssh\trading-bot-key.pem" /grant:r "$($env:USERNAME):(R)"
```

### Крок 3.2 — SSH

```powershell
ssh -i "$env:USERPROFILE\.ssh\trading-bot-key.pem" ubuntu@ВАШ_PUBLIC_IP
```

При першому підключенні: `yes` → Enter.

> Якщо `Permission denied`: перевірте, що Security Group дозволяє SSH з вашої IP, і що ключ `.pem` правильний.

---

## Частина 4 — Завантажити проєкт на сервер

Є два способи. **Рекомендований — git clone** (якщо репо на GitHub).

### Спосіб A — Git clone (рекомендовано)

На EC2 (в SSH-сесії):

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git sqlite3

git clone https://github.com/Yurenko/profitable-bot.git
cd profitable-bot

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Спосіб B — Завантажити з вашого ПК (якщо репо приватне / локальні зміни)

**Варіант B1 — SCP з Windows:**

```powershell
scp -i "$env:USERPROFILE\.ssh\trading-bot-key.pem" -r "D:\Yura\ТЗ\bot-1" ubuntu@ВАШ_IP:~/bot-1
```

На сервері:

```bash
cd ~/bot-1
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Варіант B2 — GitHub (приватний репо):**

1. На EC2 згенеруйте SSH-ключ: `ssh-keygen -t ed25519 -C "ec2-bot"`
2. Додайте `~/.ssh/id_ed25519.pub` у GitHub → Settings → SSH keys
3. `git clone git@github.com:Yurenko/profitable-bot.git`

---

## Частина 5 — Налаштування бота

### Крок 5.1 — `.env`

```bash
cd ~/profitable-bot   # або ~/bot-1
cp .env.example .env
nano .env
```

Заповніть (для paper можна лишити порожнім, якщо тільки публічні дані):

```
EXCHANGE_API_KEY=ваш_ключ
EXCHANGE_API_SECRET=ваш_секрет
DASHBOARD_PASSWORD=ваш_складний_пароль
```

`DASHBOARD_PASSWORD` — пароль для входу на `http://IP:8080`. Сесія тримається **24 години**.

Зберегти: `Ctrl+O`, Enter, `Ctrl+X`.

### Крок 5.2 — Перевірка

```bash
source .venv/bin/activate
python main.py paper --once
```

Без помилок → один торговий цикл пройшов.

### Крок 5.3 — Тестовий запуск (2–3 хв)

```bash
python main.py paper
```

`Ctrl+C` — зупинка. Перевірте:

```bash
ls -la data/bot_state.sqlite
tail logs/audit.jsonl
```

---

## Частина 6 — Автозапуск 24/7 (systemd)

### Крок 6.1 — Встановити unit-файл

```bash
sudo cp deploy/systemd/trading-bot-paper.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-bot-paper.service
```

Змініть шляхи під ваш каталог (якщо не `profitable-bot`):

```ini
WorkingDirectory=/home/ubuntu/profitable-bot
EnvironmentFile=-/home/ubuntu/profitable-bot/.env
ExecStart=/home/ubuntu/profitable-bot/.venv/bin/python main.py paper
StandardOutput=append:/home/ubuntu/profitable-bot/logs/systemd-paper.log
StandardError=append:/home/ubuntu/profitable-bot/logs/systemd-paper.log
```

Створіть папку логів:

```bash
mkdir -p logs
```

### Крок 6.2 — Запуск

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-bot-paper
sudo systemctl start trading-bot-paper
sudo systemctl status trading-bot-paper
```

Має бути **active (running)**.

### Крок 6.3 — Логи

```bash
journalctl -u trading-bot-paper -f
tail -f logs/bot.log
tail -f logs/audit.jsonl
```

---

## Частина 7 — Dashboard (опційно)

```bash
sudo cp deploy/systemd/trading-dashboard.service /etc/systemd/system/
sudo nano /etc/systemd/system/trading-dashboard.service
# ті самі шляхи що в paper service

sudo systemctl enable --now trading-dashboard
```

У браузері: `http://ВАШ_IP:8080`

**Важливо:** не натискайте «Start Paper» у dashboard, якщо бот уже працює через systemd — буде два процеси.

---

## Частина 8 — Оновлення коду після змін

```bash
cd ~/profitable-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart trading-bot-paper
```

---

## Free Tier — скільки це коштує

| Ресурс | Free Tier (новий акаунт) | Після 12 міс |
|--------|--------------------------|--------------|
| t2.micro / t3.micro | 750 год/міс (~1 інстанс 24/7) | ~$8–10/міс |
| EBS 30 GB | включено | ~$2–3/міс |
| Elastic IP (прив’язаний) | безкоштовно | безкоштовно |
| Трафік | ~15 GB/міс outbound | платно понад ліміт |

**Щоб не платити зайве:**

- Не створюйте зайві інстанси
- **Stop** (не Terminate) інстанс, якщо бот тимчасово не потрібен — але IP без Elastic IP зміниться
- Тримайте billing alerts увімкненими

---

## Часті проблеми

| Проблема | Рішення |
|----------|---------|
| SSH timeout | Security Group: порт 22, Source = My IP |
| `Permission denied (publickey)` | Перевірте `.pem` і користувача `ubuntu` |
| Бот падає після ребуту | `sudo systemctl enable trading-bot-paper` |
| Немає модулів Python | `source .venv/bin/activate && pip install -r requirements.txt` |
| Dashboard не відкривається | Security Group порт 8080, `systemctl status trading-dashboard` |
| Дані зникли | Переконайтесь, що не на Vercel — на EC2 шлях `data/bot_state.sqlite` |

---

## Чеклист

- [ ] AWS акаунт + billing alerts
- [ ] EC2 Ubuntu 22.04 t2.micro/t3.micro
- [ ] Key pair `.pem` збережено
- [ ] Security Group: SSH 22, (опційно) 8080
- [ ] Elastic IP (опційно)
- [ ] SSH з Windows працює
- [ ] `git clone` + `pip install`
- [ ] `.env` налаштовано
- [ ] `python main.py paper --once` OK
- [ ] systemd `trading-bot-paper` active
- [ ] `data/bot_state.sqlite` існує після роботи

---

## Підсумок

| Питання | Відповідь |
|---------|-----------|
| EC2 чи S3? | **EC2** |
| Як завантажити код? | **`git clone`** або **SCP** з Windows |
| Немає AWS акаунта? | Реєстрація на aws.com + картка + Free Tier |
| Як 24/7? | **systemd** + `python main.py paper` |
| Де дані? | `data/bot_state.sqlite` на диску EC2 |

Загальна інструкція (Oracle / Windows): [DEPLOY_24_7_UA.md](DEPLOY_24_7_UA.md)
