# Reaction Bot Manager — UI v4

Built from the working manager/reaction system.

## UI changes
- Dynamic `/start` greeting shows the Telegram display name of the person who opened that specific bot.
- Unicode/stylized Telegram names are preserved and safely HTML-escaped.
- Main menu includes Bot Analytics.
- Existing More Bots numbering remains restricted to `@Jr_Auto_Reaction_<N>_Bot` and sorted numerically.
- All Channels and All Groups remain separate owner sections.

## Analytics
Each reaction bot now tracks locally and periodically saves:
- Messages seen
- Reactions sent
- Reaction failures
- Channels
- Groups
- Users started
- Uptime

Analytics writes are debounced so the bot does not write to Firebase for every single reaction.

## Environment variables
- `MANAGER_BOT_TOKEN`
- `TOKEN_ENCRYPTION_SECRET`
- `FIREBASE_DATABASE_URL`
- `FIREBASE_SERVICE_ACCOUNT_JSON`

Start command: `python bot.py`
