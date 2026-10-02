# Reaction Bot Manager

This build keeps the original Reaction Bot worker code/UI and replaces only the old `BOT_TOKEN_1 ... BOT_TOKEN_49` startup system with a single Manager Bot.

## Render Environment Variables

Keep the existing Firebase variables:

- `FIREBASE_DATABASE_URL`
- `FIREBASE_SERVICE_ACCOUNT_JSON`

Add:

- `MANAGER_BOT_TOKEN` — BotFather token for the Manager Bot.
- `TOKEN_ENCRYPTION_SECRET` — any long random secret used to encrypt managed bot tokens in Firebase. Keep it unchanged after deployment.

## Firebase

No manual database structure is required. The app automatically creates `managed_bots/{bot_id}` when a bot is added. Each worker continues using `reaction_bots/{bot_id}` for its original state.

## Manager Flow

1. Start the Manager Bot with `/start`.
2. Press `➕ Add Reaction Bot`.
3. Send a BotFather token.
4. The Manager validates it, encrypts the token, stores the registry record and starts that bot.
5. Add the new Reaction Bot to a Telegram channel/group with the permissions required by the original bot.
6. Use `My Bots` to start, stop, restart or remove managed bots.

## Important

Do not change `TOKEN_ENCRYPTION_SECRET` after tokens have been stored, otherwise previously encrypted tokens cannot be decrypted.

The Manager Bot is separate from the Reaction Bot workers. Every managed worker runs the same original reaction handlers and maintains its own Firebase state.
