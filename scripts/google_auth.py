from __future__ import annotations

from app.services.google_calendar import GoogleCalendarService


def main() -> None:
    service = GoogleCalendarService()
    raw_id = input(
        "Введіть ваш Telegram user id: "
    ).strip()
    try:
        telegram_id = int(raw_id)
    except ValueError:
        raise SystemExit("Потрібно ввести ціле число")

    credentials = service.ensure_credentials(telegram_id)
    print("Авторизацію Google пройдено успішно.")
    print(f"Токен збережено для користувача {telegram_id}.")

    events = service.list_upcoming_events(telegram_id, max_results=3)
    if not events:
        print("У календарі поки немає майбутніх подій.")
    else:
        print("Найближчі події:")
        for event in events:
            summary = event.get("summary", "(без назви)")
            start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
            print(f" • {summary} — {start}")


if __name__ == "__main__":
    main()
