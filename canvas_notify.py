#!/usr/bin/env python3
"""
Canvas -> Telegram: утреннее уведомление о новых заданиях.

Что делает:
  1. Берёт активные курсы пользователя в Canvas.
  2. Собирает задания с будущим дедлайном (или без дедлайна).
  3. Сравнивает с прошлым запуском (state.json) и находит:
       - новые задания;
       - задания, у которых изменился дедлайн.
  4. Отправляет в Telegram: дисциплина, задание, дедлайн, ссылка.
  5. Сохраняет состояние только после успешной отправки.

Только стандартная библиотека Python 3.8+, ничего устанавливать не нужно.

Запуск:
  python3 canvas_notify.py            # обычный запуск
  python3 canvas_notify.py --check    # проверить токены и доступ
  python3 canvas_notify.py --dry-run  # показать сообщение, не отправлять

Коды выхода: 0 — успех, 1 — ошибка настроек, 2 — ошибка Canvas, 3 — ошибка Telegram.
"""

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TG_LIMIT = 4000
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


class ConfigError(Exception):
    pass


class CanvasError(Exception):
    pass


class TelegramError(Exception):
    pass


# ---------- настройки ----------

def load_env_file(path):
    """Читает .env рядом со скриптом. Переменные окружения имеют приоритет."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_config():
    load_env_file(BASE_DIR / ".env")
    cfg = {
        "canvas_url": os.environ.get("CANVAS_BASE_URL", "").strip().rstrip("/"),
        "canvas_token": os.environ.get("CANVAS_API_TOKEN", "").strip(),
        "tg_token": os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        "tg_chat": os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        "tz_hours": os.environ.get("TZ_OFFSET_HOURS", "5").strip(),
        "notify_empty": os.environ.get("NOTIFY_WHEN_EMPTY", "true").strip().lower(),
        "state_file": os.environ.get("STATE_FILE", "").strip(),
    }
    missing = [name for name, key in [
        ("CANVAS_BASE_URL", "canvas_url"),
        ("CANVAS_API_TOKEN", "canvas_token"),
        ("TELEGRAM_BOT_TOKEN", "tg_token"),
        ("TELEGRAM_CHAT_ID", "tg_chat"),
    ] if not cfg[key]]
    if missing:
        raise ConfigError("Не заданы переменные: " + ", ".join(missing))
    if not cfg["canvas_url"].startswith("http"):
        raise ConfigError("CANVAS_BASE_URL должен начинаться с https://")
    try:
        cfg["tz"] = timezone(timedelta(hours=float(cfg["tz_hours"])))
    except ValueError:
        raise ConfigError("TZ_OFFSET_HOURS должен быть числом, например 5")
    cfg["notify_empty"] = cfg["notify_empty"] in ("1", "true", "yes", "да")
    try:
        cfg["first_run_days"] = int(os.environ.get("FIRST_RUN_DAYS", "7").strip())
    except ValueError:
        raise ConfigError("FIRST_RUN_DAYS должен быть целым числом, например 7")
    cfg["state_path"] = Path(cfg["state_file"]) if cfg["state_file"] else BASE_DIR / "state.json"
    return cfg


# ---------- Canvas ----------

def _next_link(link_header):
    for part in (link_header or "").split(","):
        match = re.search(r'<([^>]+)>\s*;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


def canvas_get_all(cfg, path, params):
    url = "{}/api/v1/{}?{}".format(cfg["canvas_url"], path, urllib.parse.urlencode(params, doseq=True))
    items = []
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + cfg["canvas_token"],
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                link = resp.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            raise CanvasError("Canvas вернул HTTP {} для {}".format(e.code, path))
        except urllib.error.URLError as e:
            raise CanvasError("Нет связи с Canvas: {}".format(e.reason))
        items.extend(data if isinstance(data, list) else [data])
        url = _next_link(link)
    return items


def fetch_courses(cfg):
    courses = canvas_get_all(cfg, "users/self/courses", {
        "enrollment_state": "active",
        "per_page": 100,
    })
    return [c for c in courses if c.get("id") and c.get("name") and not c.get("access_restricted_by_date")]


def fetch_assignments(cfg, course):
    return canvas_get_all(cfg, "courses/{}/assignments".format(course["id"]), {
        "per_page": 100,
        "order_by": "due_at",
    })


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def collect_current(cfg, now_utc):
    """Возвращает dict ключ -> данные задания (только будущие или без дедлайна)."""
    current = {}
    warnings = []
    for course in fetch_courses(cfg):
        try:
            assignments = fetch_assignments(cfg, course)
        except CanvasError as e:
            warnings.append("{}: {}".format(course["name"], e))
            continue
        for a in assignments:
            if a.get("published") is False:
                continue
            due = parse_dt(a.get("due_at"))
            if due is not None and due <= now_utc:
                continue
            key = "{}:{}".format(course["id"], a["id"])
            current[key] = {
                "course": course["name"].strip(),
                "name": (a.get("name") or "Без названия").strip(),
                "due_at": a.get("due_at"),
                "url": a.get("html_url", ""),
            }
    return current, warnings


# ---------- состояние ----------

def load_state(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("assignments", {})
    except (ValueError, OSError):
        return None


def save_state(path, current):
    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "assignments": {k: v["due_at"] for k, v in current.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def diff(current, previous):
    previous = previous or {}
    new, changed = [], []
    for key, item in current.items():
        if key not in previous:
            new.append(item)
        elif previous[key] != item["due_at"]:
            changed.append(item)
    return new, changed


# ---------- сообщение ----------

def fmt_due(value, tz):
    dt = parse_dt(value)
    if dt is None:
        return "без дедлайна"
    local = dt.astimezone(tz)
    return "{} ({}) {}".format(local.strftime("%d.%m.%Y"), WEEKDAYS[local.weekday()], local.strftime("%H:%M"))


def _sort_key(item):
    dt = parse_dt(item["due_at"])
    return (dt is None, dt or datetime.max.replace(tzinfo=timezone.utc), item["name"])


def render_section(title, items, tz):
    lines = ["<b>{}</b>".format(title), ""]
    by_course = {}
    for item in sorted(items, key=_sort_key):
        by_course.setdefault(item["course"], []).append(item)
    for course in sorted(by_course, key=lambda c: _sort_key(by_course[c][0])):
        lines.append("📘 <b>{}</b>".format(html.escape(course)))
        for item in by_course[course]:
            name = html.escape(item["name"])
            if item["url"]:
                name = '<a href="{}">{}</a>'.format(html.escape(item["url"], quote=True), name)
            lines.append("• {}".format(name))
            lines.append("  ⏰ Дедлайн: {}".format(fmt_due(item["due_at"], tz)))
        lines.append("")
    return lines


def build_message(new, changed, tz, today):
    header = "📚 <b>Canvas — {}</b>".format(today)
    if not new and not changed:
        return None
    lines = [header, ""]
    if new:
        lines += render_section("Новые задания ({})".format(len(new)), new, tz)
    if changed:
        lines += render_section("Изменился дедлайн ({})".format(len(changed)), changed, tz)
    return "\n".join(lines).strip()


def build_first_run(current, tz, now_utc, today, days):
    """Первый запуск: запоминаем все задания, присылаем только ближайшие дедлайны."""
    horizon = now_utc + timedelta(days=days)
    soon = [i for i in current.values() if i["due_at"] and parse_dt(i["due_at"]) <= horizon]
    lines = [
        "📚 <b>Canvas — {}</b>".format(today),
        "",
        "✅ Canvas Notifier запущен. Отслеживаю заданий: {}.".format(len(current)),
        "Дальше буду присылать только новые задания и изменённые дедлайны.",
        "",
    ]
    if soon:
        lines += render_section("Дедлайны в ближайшие {} дн. ({})".format(days, len(soon)), soon, tz)
    else:
        lines.append("В ближайшие {} дн. дедлайнов нет.".format(days))
    return "\n".join(lines).strip()


def split_message(text, limit=TG_LIMIT):
    """Режет только по границам строк; строка дедлайна не отрывается от названия задания."""
    if len(text) <= limit:
        return [text]
    units = []
    for line in text.split("\n"):
        if units and line.startswith("  "):
            units[-1] += "\n" + line
        else:
            units.append(line)
    parts, chunk = [], ""
    for unit in units:
        candidate = (chunk + "\n" + unit) if chunk else unit
        if len(candidate) <= limit:
            chunk = candidate
        else:
            if chunk.strip():
                parts.append(chunk.strip())
            chunk = unit
    if chunk.strip():
        parts.append(chunk.strip())
    return parts


# ---------- Telegram ----------

def tg_send(cfg, text):
    url = "https://api.telegram.org/bot{}/sendMessage".format(cfg["tg_token"])
    for part in split_message(text):
        body = urllib.parse.urlencode({
            "chat_id": cfg["tg_chat"],
            "text": part,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            raise TelegramError("Telegram вернул HTTP {}: {}".format(e.code, detail))
        except urllib.error.URLError as e:
            raise TelegramError("Нет связи с Telegram: {}".format(e.reason))
        if not result.get("ok"):
            raise TelegramError("Telegram отклонил сообщение: {}".format(result.get("description")))


# ---------- запуск ----------

def run_check(cfg):
    courses = fetch_courses(cfg)
    print("Canvas: OK, активных курсов: {}".format(len(courses)))
    for c in courses:
        print("  - {}".format(c["name"]))
    tg_send(cfg, "✅ Canvas Notifier подключён. Курсов найдено: {}".format(len(courses)))
    print("Telegram: OK, тестовое сообщение отправлено")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Canvas -> Telegram уведомления")
    parser.add_argument("--check", action="store_true", help="проверить доступ и отправить тест")
    parser.add_argument("--dry-run", action="store_true", help="показать сообщение без отправки")
    args = parser.parse_args()

    try:
        cfg = get_config()
    except ConfigError as e:
        print("ОШИБКА НАСТРОЕК: {}".format(e))
        print("RESULT: status=config_error")
        return 1

    try:
        if args.check:
            run_check(cfg)
            print("RESULT: status=ok mode=check")
            return 0

        now_utc = datetime.now(timezone.utc)
        current, warnings = collect_current(cfg, now_utc)
        previous = load_state(cfg["state_path"])
        new, changed = diff(current, previous)
        today = now_utc.astimezone(cfg["tz"]).strftime("%d.%m.%Y")
        if previous is None:
            message = build_first_run(current, cfg["tz"], now_utc, today, cfg["first_run_days"])
        else:
            message = build_message(new, changed, cfg["tz"], today)

        if message is None and cfg["notify_empty"]:
            message = "📚 <b>Canvas — {}</b>\n\n✅ Новых заданий нет. Всего актуальных: {}".format(today, len(current))
        if message and warnings:
            message += "\n\n⚠️ Не удалось прочитать: " + html.escape("; ".join(warnings))

        for w in warnings:
            print("ПРЕДУПРЕЖДЕНИЕ: {}".format(w))

        if args.dry_run:
            print(message or "(сообщение не отправлялось бы: изменений нет)")
            print("RESULT: status=ok mode=dry_run new={} changed={} total={}".format(len(new), len(changed), len(current)))
            return 0

        if message:
            tg_send(cfg, message)
        save_state(cfg["state_path"], current)
        print("RESULT: status=ok new={} changed={} total={} sent={}".format(
            len(new), len(changed), len(current), "yes" if message else "no"))
        return 0

    except CanvasError as e:
        print("ОШИБКА CANVAS: {}".format(e))
        try:
            tg_send(cfg, "⚠️ Canvas Notifier: не удалось получить задания.\n{}".format(html.escape(str(e))))
        except TelegramError:
            pass
        print("RESULT: status=canvas_error")
        return 2
    except TelegramError as e:
        print("ОШИБКА TELEGRAM: {}".format(e))
        print("RESULT: status=telegram_error")
        return 3


if __name__ == "__main__":
    sys.exit(main())
