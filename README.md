# Ventorme — бэкенд Telegram-напоминаний

Python-бот (`bot.py`) работает с той же базой Supabase, что и сайт `../index.html`.
Сайт сохраняет в таблицу `telegram_subscriptions` username, время и часовой пояс. Бот привязывает chat_id и каждый день шлёт напоминание о серии.

## Запуск

```bash
cd backend
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python bot.py
```

В `.env` нужно заполнить:

| Переменная | Где взять |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → проект **ventorme** → Project Settings → API Keys → `service_role` (Legacy) или Secret key |
| `SITE_URL` | публичный адрес сайта, например `https://ventorme.example.com` (нужен для кнопки «Открыть Ventorme») |

При старте бот сам записывает свой `@username` в таблицу `app_config`. После этого на сайте в «Настройках» появляется кнопка «Открыть бота».

Бот должен работать постоянно: на VPS, Render или Railway (Background Worker) или на любом компьютере, который не выключается.

## Команды бота

`/start` — привязка (сайт открывает бота со ссылкой `?start=<token>`) · `/status` — серия и настройки · `/time 20:00` — время напоминания · `/stop` / `/resume` — выключить или включить напоминания.

## База

`schema.sql` — полная схема: таблицы, RLS, функции `check_in`, `link_child`, `get_children`. Она уже применена к проекту `uumdxurztnvykksrfbim`. Файл нужен только для развёртывания в другом проекте.
