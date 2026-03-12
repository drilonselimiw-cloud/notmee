# Encar Car Monitor Bot 🚗

An interactive **Telegram bot** that monitors [encar.com](https://www.encar.com) for new car listings. Create multiple search filters via chat commands and get instant notifications when new cars match.

## Features

- **Interactive** — manage everything via Telegram chat commands
- **Multiple filters** — monitor 4+ different car types simultaneously
- **Two ways to add filters** — guided step-by-step or paste an encar URL
- **Persistent** — filters and seen-cars survive restarts
- **Pause/resume** — temporarily stop individual filters without deleting them

## Quick Start

### 1. Create a Telegram bot (~1 minute)

1. Open Telegram, search **@BotFather**, send `/newbot`
2. Follow the prompts — you'll get a **bot token**
3. Copy the token

### 2. Configure & run

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env and paste your TELEGRAM_BOT_TOKEN
python monitor.py
```

### 3. Chat with your bot

Open Telegram, find your bot, and send `/start`.

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message & help |
| `/add <name>` | Add a filter with guided steps |
| `/url <name> <encar_url>` | Quick-add from an encar search URL |
| `/list` | Show all your filters |
| `/remove <id>` | Delete a filter |
| `/pause <id>` | Pause a filter |
| `/resume <id>` | Resume a paused filter |
| `/clear <id>` | Reset seen cars (re-alerts on existing) |
| `/status` | Monitor status & stats |
| `/help` | Show commands |

## Usage Examples

**Add a filter step-by-step:**
```
You: /add Grandeur 2023+
Bot: 🚗 Car type? (kor/for or skip)
You: kor
Bot: 🏭 Manufacturer?
You: 현대
Bot: 📋 Model?
You: 그랜저
...
Bot: ✅ Filter created!
```

**Quick-add from encar URL:**
```
You: /url Palisade https://www.encar.com/dc/dc_carsearchlist.do?carType=kor...
Bot: ✅ Filter created from URL!
```

**Run 4 filters at once:**
```
/add BMW-X5
/add 팰리세이드
/url Tesla-Y https://www.encar.com/...
/url Carnival https://www.encar.com/...
```

All four run simultaneously — you'll get notified for each separately.

## How It Works

1. You create filters via Telegram chat commands
2. A background thread checks all active filters every N minutes
3. New car IDs are compared against each filter's seen-cars list
4. New matches trigger a Telegram message with car details + direct encar link
5. Filters and seen-cars persist to `filters.json` (survives restarts)

## Configuration (`.env`)

```ini
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...   # Required
CHECK_INTERVAL_MINUTES=10               # Check frequency
MAX_RESULTS=50                          # Results per filter per check
```

## File Structure

```
cApp/
├── monitor.py          # Telegram bot + background monitor
├── scraper.py          # Encar API scraper  
├── notifier.py         # Telegram message sender
├── filter_store.py     # Multi-filter persistence (filters.json)
├── requirements.txt    # Python dependencies
├── .env.example        # Config template
├── .env                # Your config (git-ignored)
├── filters.json        # Auto-generated: your filters + seen cars
└── README.md
```

## Common Manufacturer/Model Names (Korean)

| Manufacturer | Models |
|---|---|
| 현대 (Hyundai) | 그랜저, 쏘나타, 아반떼, 투싼, 싼타페, 팰리세이드, 캐스퍼, 스타리아, 아이오닉5 |
| 기아 (Kia) | K5, K8, K9, 쏘렌토, 카니발, 스포티지, EV6, 셀토스, 레이 |
| 제네시스 (Genesis) | G70, G80, G90, GV60, GV70, GV80 |
| BMW | 3시리즈, 5시리즈, 7시리즈, X3, X5 |
| 벤츠 (Mercedes) | E클래스, S클래스, GLC, GLE |
| 테슬라 (Tesla) | 모델3, 모델Y |
