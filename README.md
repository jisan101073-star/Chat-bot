# Reaction Bot Manager — Premium UI v1

This build keeps the working Manager + dynamic Reaction Bot architecture and upgrades the Reaction Bot user-facing `/start` UI.

## UI changes
- Cleaner welcome message with user name, bot identity, owner line and official channel link.
- Styled inline buttons using Telegram's supported `primary` (blue) and `success` (green) button styles.
- Added `🤖 MORE BOTS` page that automatically lists enabled managed Reaction Bots from Firebase.
- Cleaner `HOW TO USE` screen and styled back button.
- Existing reaction logic, Firebase per-bot state, manager system, statistics, channel/group tracking and token encryption remain intact.

## Render variables
Keep:
- `FIREBASE_DATABASE_URL`
- `FIREBASE_SERVICE_ACCOUNT_JSON`

Add/keep:
- `MANAGER_BOT_TOKEN`
- `TOKEN_ENCRYPTION_SECRET`

Do not add old `BOT_TOKEN_1 ... BOT_TOKEN_49` variables.

## Runtime
Python 3.11.9 (`runtime.txt`).
