# Deploying to Railway (free, 24/7)

## What you need
- A [GitHub](https://github.com) account (free)
- A [Railway](https://railway.app) account (free, sign in with GitHub)
- Your bot token from BotFather

---

## Step 1 — Push your files to GitHub

1. Go to https://github.com/new and create a **new repository** (name it anything, e.g. `truth-or-lie-bot`). Set it to **Private** so your token stays safe.

2. On your PC, open `cmd` inside your `Truth or Lie` folder and run:

```cmd
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/truth-or-lie-bot.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username.

> If git isn't installed: https://git-scm.com/download/win — install it, then restart cmd.

---

## Step 2 — Create a Railway project

1. Go to https://railway.app and click **Start a New Project**.
2. Choose **Deploy from GitHub repo**.
3. Select your `truth-or-lie-bot` repository.
4. Railway will detect the `Procfile` and start building automatically.

---

## Step 3 — Add your bot token

1. In your Railway project, click your service (the box that appeared).
2. Go to the **Variables** tab.
3. Click **Add Variable** and set:
   - Key: `BOT_TOKEN`
   - Value: your bot token from BotFather
4. Railway will automatically restart the bot with the new variable.

---

## Step 4 — Add PostgreSQL (keeps leaderboard across redeploys)

Without this, the leaderboard resets every time you redeploy. Takes 30 seconds to add:

1. In your Railway project dashboard, click **+ New** → **Database** → **Add PostgreSQL**.
2. Railway automatically sets the `DATABASE_URL` variable in your service. That's it — the bot detects it and switches from SQLite to PostgreSQL automatically.

---

## Step 5 — Verify it's running

1. Go to the **Deployments** tab in Railway — you should see a green **Active** deployment.
2. Click **View Logs** and look for:
   ```
   Starting Two Truths and a Lie bot...
   ```
3. Open Telegram, find your bot, send `/start` — it should reply.

---

## Updating the bot later

Whenever you change the code on your PC, just push to GitHub:

```cmd
git add .
git commit -m "describe your change"
git push
```

Railway detects the push and redeploys automatically within ~30 seconds.

---

## Staying within the free tier

Railway's free tier gives **$5 of credit per month**. A small Python bot uses roughly **$0.50–$1.50/month** depending on activity, so you'll comfortably stay free. You can monitor usage in **Project Settings → Usage**.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Build fails | Check **Logs** tab — usually a missing package. Make sure `requirements.txt` was committed. |
| Bot not responding | Check `BOT_TOKEN` is set correctly in Variables (no quotes, no spaces). |
| `DATABASE_URL` not found | Make sure PostgreSQL was added to the **same project** as the bot service. |
| Leaderboard empty after redeploy | You didn't add PostgreSQL — do Step 4 above. |
| Bot crashes on startup | Check Logs for the error. Most common: wrong Python version (runtime.txt pins 3.12 which should fix this). |
