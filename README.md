Reaction Bot Manager — UI v3 / Strong Multi-Bot Build

Main Reaction Bot UI:
- Keeps the requested Hey! 👋 + Zinn decorative header style.
- Keeps the original seamless reaction sentence.
- Removes the unwanted “Automatic • Fast • Random” line.
- Adds a clean owner line and polished button typography.

Navigation:
- Owner gets ALL CHANNELS and ALL GROUPS separately.
- MORE BOTS accepts only usernames matching Jr_Auto_Reaction_<number>_Bot.
- MORE BOTS sorts by numeric serial: 1, 2, 3, 5, 10...
- The current bot is excluded from its own MORE BOTS list.

Manager UI:
- Refreshed premium manager menu.
- Added RESTART ALL so all managed worker bots can be refreshed without changing Firebase data.

Multi-bot resource improvements:
- Bounded Telegram update concurrency per bot instead of unbounded concurrent updates.
- Smaller HTTP connection pools per bot to reduce idle resource usage.
- Per-bot reaction semaphore limits simultaneous reaction API calls.
- Short Firebase managed-bot registry cache reduces repeated reads from button clicks.
- Existing bots are restored in the background after the Manager starts, with a small startup concurrency limit to avoid a large boot-time spike.
- Chat registration Firebase writes are lightly debounced so bursts are grouped.

Deployment:
- Keep runtime.txt as python-3.11.9.
- Keep the same Firebase environment variables.
- Keep MANAGER_BOT_TOKEN and TOKEN_ENCRYPTION_SECRET.
- No manual Firebase structure creation is required.

Important:
- This build still uses one polling connection per running Telegram bot; it reduces application/request overhead and startup bursts, but it does not make bot capacity unlimited.
- Bots running in separate old Render services will not receive this code until those old services are replaced/redeployed.
