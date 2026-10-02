# Reaction Bot — Manager Architecture

One Render service runs a Manager Bot plus multiple independent Reaction Bot workers.
Each worker uses its own Bot Token and keeps its own Firebase state under `reaction_bots/{bot_id}`.

## Render Environment Variables

- `MANAGER_BOT_TOKEN` — BotFather token for the manager bot.
- `TOKEN_ENCRYPTION_SECRET` — any long random secret used to encrypt stored reaction-bot tokens. Keep it unchanged after bots are added.
- `FIREBASE_DATABASE_URL`
- `FIREBASE_SERVICE_ACCOUNT_JSON`

`PORT` is optional; Render normally supplies it.

## Manager flow

1. Open the manager bot and send `/start`.
2. Tap **Add Reaction Bot**.
3. Send a BotFather token.
4. The manager validates the token, encrypts it, saves it in Firebase, and starts that bot.
5. Add the resulting reaction bot to a target channel/group and grant the permissions it needs.
6. Use **My Bots** to start, stop, restart, or remove a worker.

## Important

Do not change `TOKEN_ENCRYPTION_SECRET` after bots have been registered unless you intentionally re-encrypt their stored tokens. Telegram/API limits and Render CPU/RAM limits still apply; the architecture does not guarantee unlimited concurrent bots.
