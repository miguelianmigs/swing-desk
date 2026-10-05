# Swing desk + scanner

Everything in one place: a dashboard page that refreshes itself twice a day
with new setups, plus your trade planner, watchlist, open positions and log.
Telegram pings your phone when a scan runs.

## How it runs (you don't do anything daily)
- On market days at **10:30 AM and 3:30 PM ET**, GitHub runs `scanner.py` for free.
- It saves the results to `docs/data.json`, which updates the dashboard page,
  and sends the setups to Telegram.
- You open the dashboard link whenever you want. Check the news on a setup,
  tap **Plan**, and if it passes the checks, place the order in your broker.

## One-time setup (about 20 minutes)
1. **Telegram bot:** in Telegram, message @BotFather, send /newbot, copy the token.
   Send your new bot any message, then open
   https://api.telegram.org/bot<TOKEN>/getUpdates and copy the "chat":{"id" number.
2. **GitHub:** create a free account and a new repository named `swing-desk`.
   Make it **Public** (free GitHub Pages needs that; your Telegram keys stay
   hidden as secrets, and your trades stay on your phone, never in the repo).
   Upload every file and folder here, keeping the `.github/workflows/` and `docs/` paths.
3. **Secrets:** Settings > Secrets and variables > Actions > New repository secret.
   Add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **Dashboard:** Settings > Pages > Deploy from a branch > `main` / `docs` > Save.
   Your page will be at https://YOURNAME.github.io/swing-desk/. Add it to your
   phone's home screen.
5. **First run:** Actions tab > swing-scan > Run workflow. Telegram should ping
   and the dashboard should show "Last scan ...".

## Schedule
Runs on Eastern time (`America/New_York`), so it follows the clock change in
November and March on its own. To change the times, edit the cron line in
`.github/workflows/scan.yml`: "30 10,15" means 10:30 and 15:30 (3:30 PM).
GitHub can start scheduled runs a few minutes late when it's busy.

## Tuning
Change the rules at the top of `scanner.py`. Update `ACCOUNT` as your account
grows. Add tickers you read about to `watchlist.txt` so they're always checked.

## Notes
- Market data comes from Yahoo Finance via `yfinance`: free, unofficial, and
  occasionally down. A failed run usually works on the next one.
- Trades you log are stored in your phone's browser. Use **Export backup**
  now and then.
- Order placement through Webull comes later, once the alerts have earned trust.
