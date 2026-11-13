# AUREX Company collaboration bot

Standalone Telegram bot and Mini App for applications, employee work reports, fixed monthly salary accruals, TRC20 wallet records, and manual payout tracking. It runs separately from AUREX Miner; it does not change the root bot or root Dockerfile.

## Local run

Requires Python 3.12+ and a Telegram bot created with BotFather. The app uses only the Python standard library.

```powershell
$env:BOT_TOKEN = "set locally; do not commit it"
$env:ADMIN_TELEGRAM_IDS = "123456789"
$env:WEBAPP_URL = "https://your-public-https-domain/"
python app.py
```

Set `WEBAPP_URL` to this service's public HTTPS URL and register it as the bot's Mini App domain with BotFather. Open the bot and send `/start`. The admin ID is the numeric Telegram user ID, not a username. Admin users can search users by username after they have started this bot; enrollment is always bound to the Telegram numeric ID.

## Deployment

Create a **separate Render Web Service** from this repository with `coop` as its Docker build context / Dockerfile directory. Do not change the existing root service or root `Dockerfile`. Attach a persistent disk mounted at `/var/data`, then set the variables below. Set `WEBAPP_URL` to the final service URL (ending in `/`). Redeploy/restart the service after secrets are configured.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `BOT_TOKEN` | Yes | — | Secret token from BotFather. Add through the hosting provider's secret environment settings, never to the repository or chat. |
| `ADMIN_TELEGRAM_IDS` | Yes for admin functions | — | Comma-separated numeric Telegram user IDs, e.g. `123456789,987654321`. |
| `WEBAPP_URL` | Yes for Telegram launch | — | Public HTTPS URL of this service, e.g. `https://aurex-coop.onrender.com/`. |
| `PORT` | No | `8080` | HTTP port provided by the host. |
| `DATABASE_PATH` | No | `coop/.data/coop.sqlite3` | SQLite database path; use `/var/data/coop.sqlite3` with a persistent Render disk. |
| `STORAGE_DIR` | No | `coop/.data` | Directory containing private uploads; use `/var/data` with a persistent disk. Uploads are not served from the public web root. |
| `ALLOWED_ORIGIN` | No | same-origin only | Optional exact browser origin if serving the Mini App from a different domain. |

Use the *same* persistent disk for both `DATABASE_PATH` and `STORAGE_DIR`. Telegram bot polling requires that only one service instance uses this token at a time; do not run overlapping deployments or multiple replicas with the same bot.

## Workflow

- Users start the bot and open its Mini App. The backend verifies Telegram's signed, time-limited `initData`; usernames are display/search fields only.
- Anyone may submit one pending job or collaboration application. Admins can accept/reject applications, and separately enroll a user who has started the bot, setting a fixed monthly USDT-denominated salary.
- Active employees can edit their TRC20 wallet and submit work reports with an optional attachment. Admins review and approve/reject every report.
- An approved report creates one internal salary credit for that employee per UTC calendar month. Approving additional reports during that month does not duplicate salary. This is an internal balance only.
- No on-chain transaction is sent. Admins make any USDT transfer outside the app and record the manual payout (amount plus optional TXID/note), which deducts from the internal balance.
- Upload limit is 10 MiB. Only PDF, PNG, JPG/JPEG, WEBP, TXT, CSV, and ZIP files with matching content are accepted. Files are stored under the configured private storage directory with generated names.

## Checks

Run focused tests with:

```powershell
python -m unittest discover -s tests -v
python -m py_compile app.py
```
