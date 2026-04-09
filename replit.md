# Workspace

## Overview

pnpm workspace monorepo using TypeScript + Python Telegram Bot. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)
- **Python version**: 3.12
- **Telegram Bot**: python-telegram-bot 22.7
- **AI**: Google Gemini via user's own API key (google-genai SDK)

## Telegram Bot ("Mismari" / مسماري)

Located in `telegram-bot/` directory. A Python Telegram bot with persona "Mismari" (مسماري), developed by @mohmmed.

### Features

- **Smart model routing**: AI automatically chooses gemini-2.5-flash-lite (simple) or gemini-2.5-flash (complex)
- **Mismari persona**: Fixed system instruction with Iraqi-tech personality, never forgets identity
- **Conversation memory**: SQLite with auto-summarization (prunes old messages, keeps summary)
- **Context pruning**: Only sends last 10 messages + summary to save tokens
- **Image compression**: Pillow resizes to 720p before sending to API
- **Response caching**: Common identity questions cached in SQLite
- **Token optimization**: Short max_tokens (1024) for simple queries, full (8192) for complex
- **Image/voice/document analysis**: Full multimodal support
- **Custom system prompts**: ConversationHandler flow for /system command
- **Forced channel subscription**: Configurable via REQUIRED_CHANNEL env var
- **Admin dashboard**: /admin shows full stats (owner-only)
- **User tracking**: SQLite users table tracks all users with activity data
- **Media downloader**: Download from YouTube, Instagram, TikTok, Spotify, SoundCloud, Deezer, Facebook, Twitter, Pinterest, Threads, Google Drive, Snapchat, Likee, Kwai via yt-dlp
- **Dual mode**: /start shows Download vs AI buttons; each mode has its own flow
- **Commands**: /start, /help, /system, /clear, /stats, /admin

### Environment Variables

- `TELEGRAM_BOT_TOKEN` — Telegram bot API token
- `OWNER_ID` — Telegram user ID of the bot owner
- `GEMINI_API_KEY` — User's own Google AI Studio API key
- `REQUIRED_CHANNEL` — (Optional) Telegram channel username for forced subscription (e.g. @mychannel)

### Model Routing Logic

- Simple text (<300 chars, no complex keywords) → gemini-2.5-flash-lite (1000 RPD)
- Complex text, code, analysis, long messages → gemini-2.5-flash (250 RPD)
- Photos, voice, documents → always gemini-2.5-flash

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally
- `cd telegram-bot && python bot.py` — run Telegram bot

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
