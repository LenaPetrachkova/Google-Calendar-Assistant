# Calendar Assist Telegram Bot

AI-асистент для керування часом через Telegram. Бот спілкується українською мовою, розуміє природні запити через Gemini API та керує подіями у Google Calendar.

## Основні можливості

- **Управління подіями** — створення, редагування, видалення, пошук подій через природну мову
- **Конструктор звичок** — автоматичне планування звичок з підбором вільних слотів
- **Аналітика** — статистика зайнятого часу, візуалізація, рекомендації
- **Розкладання задач** — розбиття великих задач на блоки з урахуванням дедлайну
- **Пошук вільного часу** — динамічний пошук слотів для подій
- **Google Meet інтеграція** — автоматичне додавання посилань на зустрічі
- **Нагадування та конфлікти** — автоматичні нагадування та перевірка накладань

## Швидкий старт

### 1. Встановіть залежності

```bash
pip install -r requirements.txt
```

### 2. Налаштуйте змінні оточення

Створіть файл `.env` в корені проєкту:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret
GOOGLE_PROJECT_ID=your_google_project_id
GOOGLE_OAUTH_PORT=8080
GEMINI_API_KEY=your_gemini_api_key
DATABASE_URL=sqlite:///calendarassist.db
TZ=Europe/Kyiv
```

### 3. Налаштуйте Google OAuth

```bash
python -m scripts.google_auth
```

### 4. Запустіть бота

```bash
python -m app.bot.main
```

## Команди

- `/start` — запуск бота
- `/help` — список можливостей
- `/events` — найближчі події
- `/window` — знайти вільний час
- `/habit` — налаштувати звичку
- `/insights` — аналітика тижня
- `/plan` — розкласти задачу на блоки

