# TradingBot (Quantara v0.1)

An automated trading **research and practice** program for **MetaTrader 5** and an **Exness demo account**.
It watches live prices, decides whether a trade looks worthwhile, and (in practice modes) places or simulates trades
while a web dashboard shows you everything it is doing.

> ## Read this first
> * **Practice money only.** This version refuses to place real-money orders. You use a **demo** account (fake money, real prices).
> * **It is not a money machine.** The built-in strategy is a *hypothesis*, not a proven winner. The program includes a
>   "research lab" that tests the strategy and **blocks it when it finds no edge**. Seeing few or no trades is normal.
> * **Not financial advice.** You are responsible for anything you do with this software.
> * **Never share your `.env` file.** It holds your passwords. It is excluded from Git on purpose (see [Keeping secrets safe](#keeping-secrets-safe)).

---

## Contents
1. [How the pieces fit together](#1-how-the-pieces-fit-together)
2. [Words you will see (glossary)](#2-words-you-will-see-glossary)
3. [What you need](#3-what-you-need)
4. [Setup, step by step](#4-setup-step-by-step)
5. [Run it](#5-run-it)
6. [Using the dashboard](#6-using-the-dashboard)
7. [Safety modes: DRY_RUN → DEMO](#7-safety-modes-dry_run--demo)
8. [Daily cheat sheet](#8-daily-cheat-sheet)
9. [Troubleshooting](#9-troubleshooting)
10. [Keeping secrets safe](#keeping-secrets-safe)
11. [Versions and saving your work with Git](#11-versions-and-saving-your-work-with-git)
12. [Project layout and more documentation](#12-project-layout-and-more-documentation)

---

## 1. How the pieces fit together

```
  Exness (broker)  ──prices & orders──►  MetaTrader 5 app (must stay open and logged in)
                                                   │
                                                   │  "MetaTrader5" Python package
                                                   ▼
                                         TradingBot / Quantara  ──stores history──►  PostgreSQL database
                                                   │
                                                   ▼
                                    Dashboard in your browser (http://127.0.0.1:8787)
```

* **Exness** is the broker that provides the account and prices. You use a **demo** account.
* **MetaTrader 5 (MT5)** is the trading app installed on your PC. The bot talks to it in the background.
* **Python** runs the bot.
* **PostgreSQL** is a database where the bot keeps its records (signals, trades, risk state).
* The **dashboard** is a web page, served only on your own computer, where you watch and control the bot.

## 2. Words you will see (glossary)

| Word | Plain meaning |
|---|---|
| **Demo account** | A practice account with fake money but real market prices. |
| **Symbol** | A thing you can trade, e.g. `XAUUSDm` (gold in US dollars), `EURUSDm` (euro vs dollar). |
| **Bid / Ask** | The price you can sell at / buy at. Ask is always slightly higher. |
| **Spread** | The gap between Ask and Bid. It is a cost you pay on every trade. |
| **Lot** | The size of a trade. The smallest is usually `0.01`. |
| **Stop loss (SL)** | An order that closes a trade automatically if it loses a set amount. |
| **Take profit (TP)** | An order that closes a trade automatically when it reaches a target. |
| **Equity** | Your account's current value, including profit or loss on open trades. |
| **Drawdown** | How far equity has fallen from its highest point. |
| **Kill switch / emergency stop** | A safety lock. While engaged the bot opens **no new trades**. |
| **DRY_RUN** | The bot uses live prices but makes **paper trades** inside itself. Nothing is sent to MT5. |
| **DEMO** | The bot sends real orders to your **demo** account. |
| **Backtest** | Replaying the strategy on past prices to see how it would have done. |

## 3. What you need

| Item | Notes |
|---|---|
| A Windows 10/11 PC (64-bit) | The MetaTrader5 Python package only works on Windows. |
| An Exness account | Free at <https://my.exness.com>. You will create a **demo** trading account inside it. |
| MetaTrader 5 | The Exness version, from the Exness website. |
| Python 3.10 – 3.13 (64-bit) | From <https://www.python.org/downloads/>. Tick **"Add python.exe to PATH"** in the installer. |
| PostgreSQL 16 or 17 (+ pgAdmin 4) | From <https://www.postgresql.org/download/windows/>. Remember the `postgres` password you choose. |
| Git | From <https://git-scm.com/download/win> (to download and version this project). |
| Internet and a PC that stays awake while the bot runs | |

## 4. Setup, step by step

Do these in order. Each step says what you should see when it worked.

### Step 1 – Create your Exness demo account
1. Sign in at <https://my.exness.com> (create a free account if you do not have one).
2. In the Personal Area open **Trading accounts** and choose to open a new account → **Demo**.
3. Pick **MetaTrader 5** as the platform and a **Standard** account type. Choose a virtual balance (e.g. 500 or 10,000 USD).
4. Write down the three things Exness shows you: **Login** (a number), **Trading password**, and **Server**
   (e.g. `Exness-MT5Trial16`). Keep them private.

### Step 2 – Install and log in to MetaTrader 5
1. Download MT5 from Exness and install it (defaults are fine).
2. In MT5: **File → Login to Trade Account** → enter Login, Password, Server.
3. Turn on automatic trading: click the **Algo Trading** button in the toolbar (it must turn **green**), then
   **Tools → Options → Expert Advisors → Allow algorithmic trading**.
4. Open **View → Market Watch** (Ctrl+M) and make sure the symbol you want is listed
   (right-click → *Symbols* → search, e.g. `XAU`). Exness demo symbols often end in `m` (e.g. `XAUUSDm`).
5. Note the path of `terminal64.exe` – usually `C:\Program Files\MetaTrader 5\terminal64.exe`
   (right-click the MT5 shortcut → *Open file location*).

**It worked when:** you see live, moving prices and the Algo Trading button is green.

### Step 3 – Install Python and PostgreSQL
* **Python:** after installing, open PowerShell and run `python --version`. You should see `Python 3.x.x`.
* **PostgreSQL:** keep the default components and port **5432**. Choose and **write down** a password for the
  `postgres` user. At the end you should be able to open **pgAdmin 4** and see *Servers → PostgreSQL*.

### Step 4 – Download this project
Choose a folder **without spaces** in its path (for example `C:\Trading`), then in PowerShell:

```powershell
cd C:\Trading
git clone https://github.com/fawad1997/TradingBot.git
cd TradingBot
```

### Step 5 – Install the Python packages
Still inside the `TradingBot` folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

This creates a private Python environment (`.venv`) and downloads everything the bot needs. It can take several
minutes. If the download is interrupted, just run the last command again.
(Prefer double-clicking? `scripts\1_install.bat` does the same and also creates your `.env`.)

**It worked when:** the last command ends with `Successfully installed …`.

### Step 6 – Create the database
The SQL files are in the `database` folder (details in [database/README_DATABASE.md](database/README_DATABASE.md)).

1. Open `database\00a_create_role.sql` in Notepad. Replace `CHANGE_ME_BEFORE_RUNNING` with a password of your own
   choice. **Use only letters and numbers** – symbols like `@` or `#` break the connection string later. Save.
2. Open **pgAdmin 4** → *Servers → PostgreSQL → Databases → postgres* → right-click → **Query Tool**.
3. Click the folder icon (**Open File**), choose `00a_create_role.sql`, press **F5**.
4. Open `00b_create_database.sql` in the same window and press **F5**. You should see `CREATE DATABASE`.
5. Right-click *Databases* → **Refresh**. Right-click the new **quantara** database → **Query Tool**.
6. Open `01_schema.sql` and press **F5**. This creates the tables.

The bot also creates any missing tables itself when it starts.

### Step 7 – Create your `.env` file (your private settings)
```powershell
Copy-Item .env.example .env
notepad .env
```
Fill in:

| Line | What to put |
|---|---|
| `MT5_LOGIN` | Your demo Login number from Step 1. |
| `MT5_PASSWORD` | Your demo trading password. |
| `MT5_SERVER` | The exact server name, e.g. `Exness-MT5Trial16`. |
| `MT5_TERMINAL_PATH` | The path to `terminal64.exe` from Step 2. |
| `DATABASE_URL` | `postgresql://quantara_app:YOUR_DB_PASSWORD@localhost:5432/quantara` with the password from Step 6. |
| `DASHBOARD_TOKEN` | A long random text. Make one with: `.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"` |

Leave everything else empty for now. Save and close. **Never share or upload this file.**

### Step 8 – Let MT5 download price history (one time)
The bot refuses to start without enough past prices. In MT5, open a chart of your symbol (e.g. `XAUUSDm`) and switch it
to **M1**, **M15**, **H1** and **H4** in turn. For each, press **Home** a few times to scroll back into the past so MT5
downloads the history. If the bot later says `only 0 closed bars available`, this step is what it needs.

### Step 9 – Check everything
Keep MT5 open and logged in, then run:

```powershell
.\.venv\Scripts\python.exe -m app.main check
```

**It worked when** the last line says `ALL CHECKS PASSED` and you see your account number, `DEMO`, and your equity.

## 5. Run it

### First, try the simulator (no MT5, no risk)
```powershell
.\.venv\Scripts\python.exe -m app.main run --simulate
```
Open <http://127.0.0.1:8787> and explore the dashboard. Everything here is pretend data. Stop with **Ctrl+C**.

### Then run on live prices with paper trades (recommended first mode)
1. Open `config\config.json` and make sure it says `"environment": "DRY_RUN"`.
2. Start the bot:
   ```powershell
   .\.venv\Scripts\python.exe -m app.main run
   ```
   (or double-click `scripts\7_run_bot.bat`).
3. Watch the console. Startup goes through stages and trading may only begin at **`ACTIVE`**:
   `CONNECTING → MT5_CONNECTED → ACCOUNT_VALIDATION → SYMBOL_VALIDATION → DATA_HYDRATION → … → READY → ACTIVE`.
4. Open the dashboard: **<http://127.0.0.1:8787>**. It asks for your `DASHBOARD_TOKEN` when you use controls.
5. Stop the bot with **Ctrl+C**. Stop losses already placed at the broker stay in place.

Only run **one** bot at a time. Two bots share the same database and account and will confuse each other.

## 6. Using the dashboard

| Page | What it shows |
|---|---|
| **Overview** | Five-second health check: status lights, account cards, equity curve, risk state, open positions. |
| **Real-Time** | Live prices, spread, positions, and *"Why is (or isn't) the system trading?"* |
| **Logs & Audit** | Signals, rejected signals (with the reason), errors, trades, config history. |
| **Analysis** | Losing trades, blocked signals and research-lab results. |
| **Configuration** | Strategy switches, validated settings, kill switch, close-all, resume. |

The red **HALT NEW TRADES** button stops all new signals and orders immediately. Open positions keep their stop loss.

## 7. Safety modes: DRY_RUN → DEMO

Set the mode in `config\config.json` → `"environment"`, then restart the bot.

| Mode | What happens | Orders sent to MT5? |
|---|---|---|
| `DRY_RUN` | Live prices, **paper** trades inside the bot | No |
| `SHADOW` | Signals only, tracked as "what-if" | No |
| `DEMO` | **Real orders on your demo account** | Yes (demo only) |
| `REAL` | Disabled in this version | Never |

Recommended path: **simulator → DRY_RUN for about two weeks → DEMO**. The bot refuses to start in `DEMO` if MT5 is
logged in to a real account.

> **Tip:** `paper.starting_balance` in `config.json` (default 100) is the DRY_RUN paper balance. If it is very
> different from your demo balance, switching between `DRY_RUN` and `DEMO` can make the bot see a sudden fake
> "drawdown" and engage the emergency stop. Setting it equal to your demo balance avoids this.

## 8. Daily cheat sheet

```powershell
cd C:\Trading\TradingBot

.\.venv\Scripts\python.exe -m app.main check          # is everything connected?
.\.venv\Scripts\python.exe -m app.main status         # kill switch + risk state
.\.venv\Scripts\python.exe -m app.main run            # start bot + dashboard
.\.venv\Scripts\python.exe -m app.main run --simulate # practice on a fake market
.\.venv\Scripts\python.exe -m app.main why-no-trades  # explain why nothing is trading
```

Every day: open MT5 (logged in, Algo Trading green) → start the bot → check the **Overview** page.
The log file is `logs\quantara.log`.

## 9. Troubleshooting

| What you see | What to do |
|---|---|
| `MT5 initialize failed` | Check `MT5_TERMINAL_PATH`, login, password and server in `.env`; make sure MT5 is open. |
| `Algo Trading is disabled` | Click the **Algo Trading** button in MT5 until it is green. |
| Startup stuck at `DATA_HYDRATION`, log says `only 0 closed bars available` | Do **Step 8**: open the symbol's M1/M15/H1/H4 charts in MT5 and scroll back to download history. |
| `password authentication failed` (PostgreSQL) | The password in `DATABASE_URL` must match the one in `00a_create_role.sql`, with no special symbols. |
| `None of […] found` | Your account uses different symbol names. Edit `broker.symbol_candidates` in `config\config.json` to names shown in MT5 Market Watch. |
| `No module named 'MetaTrader5'` | Use 64-bit Windows Python and re-run Step 5. The package does not exist for 32-bit Python, macOS or Linux. |
| `pip install` fails with "connection interrupted" | Just run the same command again – it resumes. |
| Kill switch `ENGAGED`, "drawdown 80%" right after start | A stale risk record (see the tip in section 7). On the dashboard open **Configuration** and use *resume* with a reason, after checking your real equity is fine. |
| Everything is rejected / no trades | Normal. See *Rejected signals* in **Logs & Audit**. The research lab may report `RESEARCH_LAB_NO_EDGE` and block the strategy on purpose. |
| Dashboard does not open | The bot must be running. Use exactly <http://127.0.0.1:8787>. |
| Two bots seem to be running | Close one. Check Task Manager for extra `python.exe` processes started with `-m app.main run`. |

## Keeping secrets safe

* Your real passwords live **only** in `.env`. That file is listed in `.gitignore`, so Git never uploads it.
* `.env.example` is the shared template and contains placeholders only. **Never put real values in it.**
* Logs (`logs/`), downloaded history, research results and trained models are also ignored.
* Before every `git push`, run `git status` and make sure `.env` is **not** listed.
* If you ever commit a secret by mistake, **change that password straight away** – deleting the file later does not
  remove it from Git history.
* Never paste your `.env`, passwords or dashboard token into chats, screenshots or issues.

## 11. Versions and saving your work with Git

Git keeps a history of every change so you can go back. GitHub stores a copy online.

```powershell
git status                      # what changed? (.env must NOT appear)
git add .                       # stage your changes
git commit -m "Describe what you changed"
git push                        # upload to GitHub
git log --oneline               # list previous versions
```

To get updates on another PC: `git pull`.

## 12. Project layout and more documentation

```
app/        the program (broker, risk, strategies, execution, dashboard, research ...)
config/     config.json (settings) and strategies.json (strategy parameters)
database/   SQL files that create the database
docs/       full documentation
scripts/    double-click launchers for Windows (.bat files)
tests/      automated tests
tools/      helper tools for analysis
```

| Document | What it covers |
|---|---|
| [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) | The original detailed A–Z setup and operating guide |
| [docs/REFERENCE.md](docs/REFERENCE.md) | Technical feature reference (every setting and feature in depth) |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | Dashboard guide |
| [database/README_DATABASE.md](database/README_DATABASE.md) | Database setup details |
| [docs/REMOTE_ACCESS.md](docs/REMOTE_ACCESS.md) | Viewing the dashboard from another device |

Run the automated tests with `scripts\run_tests.bat`.

---

*This software is provided for education and research, without any warranty. Trading carries a risk of loss, and a
demo account does not guarantee similar results on a real account.*
