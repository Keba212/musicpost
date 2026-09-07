# Ukrainian Music Telegram Bot

Легальний автоматичний постинг української музики у Telegram-канал кожні 6 годин.

## Як це працює

Бот шукає треки через офіційний Jamendo API v3, бере лише треки з `audiodownload_allowed=true`, знаходить фото через Pexels API, публікує фото й аудіо через Telegram Bot API та записує `track_id` у PostgreSQL для захисту від повторів. Тимчасові файли створюються у `/tmp` і видаляються одразу після публікації.

`DRY_RUN=true` є безпечним значенням у `.env.example`: бот може перевірити пошук, але не завантажує та не публікує медіа. Для production Fly.io конфігурація виставляє `DRY_RUN=false`.

## Безпека

Токен Telegram, Jamendo client ID, Pexels API key і `DATABASE_URL` не зберігаються в репозиторії. Вони передаються через environment variables локально або через Fly.io secrets. Не вставляйте реальні значення у `README.md`, `.env.example` чи логи.

## GitHub і локальні файли

1. Створіть порожній repository на GitHub через GitHub UI, без README/license/gitignore.
2. У цій папці виконайте:

```bash
git init
git add .
git commit -m "Initial Ukrainian music bot"
git branch -M main
git remote add origin https://github.com/YOUR_GITHUB_USER/YOUR_REPOSITORY.git
git push -u origin main
```

Для локального Docker запуску створіть `.env` на основі `.env.example`, заповніть secrets і запустіть:

```bash
docker compose up --build
```

## Deployment на Fly.io

Встановіть актуальний `flyctl`, потім увійдіть:

```bash
fly auth login
```

У корені проєкту створіть Fly app. Команда прочитає наявний `fly.toml`; якщо ім'я `ukrainian-music-bot` вже зайняте, задайте у `fly launch` інше унікальне ім'я та оновіть `app` у `fly.toml`:

```bash
fly launch
```

Створіть Managed Postgres cluster актуальною командою Fly.io. Команда інтерактивна і запропонує назву, регіон та plan:

```bash
fly mpg create
```

Приєднайте cluster до app. `CLUSTER_ID` візьміть з `fly mpg list`:

```bash
fly mpg list
fly mpg attach CLUSTER_ID -a ukrainian-music-bot
```

`fly mpg attach` встановлює pooled `DATABASE_URL` як secret і перезапускає app. Якщо ви приєднали database до іншої назви app, використовуйте фактичне ім'я app.

Встановіть решту secrets. Значення після `=` вводьте локально, не комітьте їх і не вставляйте у цей README:

```bash
fly secrets set BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN" TARGET_CHAT_ID="@your_channel" JAMENDO_CLIENT_ID="YOUR_JAMENDO_CLIENT_ID" PEXELS_API_KEY="YOUR_PEXELS_API_KEY" -a ukrainian-music-bot
```

Якщо `DATABASE_URL` не встановлювався через attach, встановіть його через безпечний connection string з MPG dashboard:

```bash
fly secrets set DATABASE_URL="postgres://USER:PASSWORD@HOST:PORT/DATABASE" -a ukrainian-music-bot
```

Зробіть deployment:

```bash
fly deploy
```

Перевірте live logs:

```bash
fly logs -a ukrainian-music-bot
```

## Telegram налаштування

1. Створіть бота через `@BotFather` і візьміть token.
2. Додайте бота до каналу як administrator.
3. Видайте йому право `Post Messages`. Для цього проєкту достатньо публікувати повідомлення; інші права не потрібні.
4. Вкажіть у `TARGET_CHAT_ID` username публічного каналу у форматі `@channel_username`. Для приватного каналу використовуйте numeric chat ID, отриманий через Telegram Bot API після додавання бота.
5. Після `fly deploy` бот автоматично публікуватиме новий трек приблизно кожні 360 хвилин.

## Важливі обмеження API

- Jamendo не гарантує українську мову кожного результату за одним тегом. Бот використовує `fuzzytags=ukrainian` і пропускає треки без дозволеного download URL. Це найближчий легальний варіант без scraping або обходу обмежень.
- Pexels вимагає attribution: caption містить посилання на фотографа, фото і Pexels.
- Telegram Bot API має ліміти розміру медіа та rate limits. HTTP 408/425/429/5xx і тимчасові помилки повторюються з exponential backoff. Якщо окремий трек не завантажився, цикл завершується з помилкою, файли чистяться, а наступна спроба не зупиняє процес.
- На Fly.io root filesystem є ephemeral. Бот не зберігає медіа після публікації; PostgreSQL зберігає лише metadata і deduplication IDs.

## Офіційна документація

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [aiogram](https://docs.aiogram.dev/en/latest/)
- [Jamendo API v3](https://developer.jamendo.com/v3.0/docs)
- [Pexels API](https://www.pexels.com/api/documentation/)
- [Fly.io](https://fly.io/docs/)
- [Fly Managed Postgres](https://fly.io/docs/mpg/)
- [PostgreSQL](https://www.postgresql.org/docs/current/)
