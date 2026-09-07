# Ukrainian Music Telegram Bot

Автоматичний постинг пересланих аудіотреків у Telegram-канал раз на годину.

## Як це працює

Ти пересилаєш аудіо боту в особистий чат. Бот зберігає лише Telegram `file_id` і metadata у PostgreSQL, щогодини публікує один трек із черги та знаходить атмосферне фото через Pexels. Аудіо не завантажується на диск.

`DRY_RUN=true` є безпечним значенням у `.env.example`: бот може перевірити пошук, але не завантажує та не публікує медіа. Для production GitHub Actions виставляє `DRY_RUN=false` і `RUN_ONCE=true`.

## Безпека

Токен Telegram, Pexels API key і `DATABASE_URL` не зберігаються в репозиторії. Вони передаються через environment variables локально або через GitHub Actions secrets. Не вставляйте реальні значення у `README.md`, `.env.example` чи логи.

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

## Безкоштовний deployment через GitHub Actions

Цей варіант не потребує постійної Fly Machine. GitHub Actions запускає один цикл бота щогодини. Розклад GitHub може виконуватися із затримкою. Для черги потрібен безкоштовний PostgreSQL provider, наприклад Neon Free або Supabase Free; цей проєкт не створює базу автоматично.

1. Створіть PostgreSQL database у вибраному provider і скопіюйте pooled connection string.
2. У GitHub repository відкрийте `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`.
3. Додайте такі secrets:

```text
BOT_TOKEN
TARGET_CHAT_ID
TARGET_CHANNEL_URL
PEXELS_API_KEY
DATABASE_URL
ADMIN_USER_ID
```

4. Відкрийте вкладку `Actions`, виберіть `Publish Ukrainian music` і натисніть `Run workflow` для першого запуску.
5. Наступні запуски відбуватимуться автоматично щогодини. Якщо відповідного треку немає, бот нічого не публікує і чекає наступної години. Результати та помилки доступні у вкладці workflow logs.

GitHub Actions secrets не виводяться у логи. Не додавайте їх у `.env`, commit або README.

## Альтернативний deployment на Fly.io

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

Встановіть secrets. `TARGET_CHANNEL_URL` — це посилання на твій канал або invite-link приватного каналу. `ADMIN_USER_ID` необов'язковий, але рекомендований: тоді бот прийматиме треки лише від твого Telegram-акаунта.

```bash
fly secrets set BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN" TARGET_CHAT_ID="@your_channel" PEXELS_API_KEY="YOUR_PEXELS_API_KEY" -a ukrainian-music-bot
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
2. Додайте бота до твого target-каналу як administrator із правом `Post Messages`.
3. Вкажіть у `TARGET_CHAT_ID` username публічного каналу або numeric ID приватного каналу.
4. Знайдіть `ADMIN_USER_ID` через бота на кшталт `@userinfobot` і додайте його як GitHub Secret.
5. Перешліть аудіо боту в особистий чат. Бот не потребує admin-доступу до каналу, звідки ти переслав трек.
6. GitHub Actions щогодини опублікує один трек із черги.

Після пересилання бот покаже кнопки `Черга` та `Опублікувати зараз`. У безкоштовному GitHub Actions режимі вони обробляються під час найближчого запуску workflow; для миттєвої реакції потрібен постійно запущений worker.

## Важливі обмеження API

- Pexels вимагає attribution із посиланням на фотографа та Pexels. Поточний короткий caption залишає текстовий credit без URL на прохання власника каналу; для повної відповідності Pexels terms потрібно повернути клікабельні посилання або замінити джерело зображень на джерело з відповідною ліцензією без такої вимоги.
- Telegram Bot API має ліміти та rate limits. HTTP 408/425/429/5xx і тимчасові помилки повторюються з exponential backoff.
- Копіюй лише музику, яку дозволено перепубліковувати. Бот не читає чужі канали самостійно й не обходить їхні права доступу.

## Офіційна документація

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [aiogram](https://docs.aiogram.dev/en/latest/)
- [Pexels API](https://www.pexels.com/api/documentation/)
- [Fly.io](https://fly.io/docs/)
- [Fly Managed Postgres](https://fly.io/docs/mpg/)
- [PostgreSQL](https://www.postgresql.org/docs/current/)
