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
- **AI**: Google Gemini via Replit AI Integrations (google-genai)

## Telegram Bot

Located in `telegram-bot/` directory. A Python Telegram bot powered by Gemini AI with:

- **Dual model support**: gemini-2.5-flash (fast) and gemini-2.5-pro (deep thinking)
- **Conversation memory**: SQLite database (`telegram-bot/conversations.db`)
- **Image analysis**: Send photos for AI-powered analysis
- **Voice processing**: Send voice messages for transcription and response
- **Document analysis**: Send files/code for analysis
- **System prompts**: Customizable bot personality per chat
- **Commands**: /start, /help, /clear, /model, /system, /stats

### Environment Variables

- `TELEGRAM_BOT_TOKEN` — Telegram bot API token
- `OWNER_ID` — Telegram user ID of the bot owner
- `AI_INTEGRATIONS_GEMINI_BASE_URL` — Gemini API proxy URL (auto-configured)
- `AI_INTEGRATIONS_GEMINI_API_KEY` — Gemini API key (auto-configured)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally
- `cd telegram-bot && python bot.py` — run Telegram bot

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
