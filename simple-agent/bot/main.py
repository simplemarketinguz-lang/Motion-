"""Simple Agent — следит за твоими обещаниями в Telegram и напоминает их выполнить.

Работает как юзербот: подключается к ТВОЕМУ аккаунту, читает чаты, ищет твои обещания
через Claude и присылает напоминания в «Избранное» (Saved Messages).

Команды пишутся в «Избранное»:
  /list            — все открытые обещания
  /done 12         — отметить №12 выполненным
  /cancel 12       — это не обещание / уже неактуально
  /reopen 12       — вернуть закрытое обещание в работу
  /snooze 12 2h    — отложить (30m, 2h, 1d, tomorrow/завтра)
  /scan 3          — проанализировать переписку за последние N дней
  /help            — справка
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Channel, Chat, User

from extractor import ChatLine, PromiseExtractor
from storage import Promise, Storage, utcnow

load_dotenv()

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "9"))
DEBOUNCE = int(os.getenv("DEBOUNCE_SECONDS", "90"))
OVERDUE_REPEAT = timedelta(hours=float(os.getenv("OVERDUE_REPEAT_HOURS", "4")))
INCLUDE_GROUPS = os.getenv("INCLUDE_GROUPS", "true").lower() == "true"
EXCLUDE = {x.strip().lstrip("@").lower() for x in os.getenv("EXCLUDE_CHATS", "").split(",") if x.strip()}

CONTEXT_MESSAGES = 25          # сколько последних сообщений чата давать модели
PRE_REMIND = timedelta(hours=1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("simple_agent")

client = TelegramClient("simple_agent", API_ID, API_HASH)
db = Storage("promises.db")
extractor = PromiseExtractor(MODEL)
pending: dict[int, asyncio.Task] = {}
locks: dict[int, asyncio.Lock] = {}
ME_ID: int = 0


# ───────────────────────── helpers ─────────────────────────

def local(dt: datetime) -> datetime:
    return dt.astimezone(TZ)


def fmt_due(p: Promise) -> str:
    if not p.due_at:
        return "без срока"
    d, now = local(p.due_at), local(utcnow())
    if d.date() == now.date():
        day = "сегодня"
    elif d.date() == (now + timedelta(days=1)).date():
        day = "завтра"
    else:
        day = f"{d:%d.%m}"
    late = " ⚠️ просрочено" if p.due_at < utcnow() else ""
    return f"{day} в {d:%H:%M}{late}"


def card(p: Promise, header: str = "") -> str:
    lines = [header] if header else []
    lines += [
        f"**#{p.id} · {p.task}**",
        f"🕐 {fmt_due(p)}   👤 {p.counterpart or p.chat_title}",
        f"💬 «{p.quote}» — {p.chat_title}",
    ]
    if p.link:
        lines.append(f"[Открыть сообщение]({p.link})")
    lines.append(f"`/done {p.id}`  `/snooze {p.id} 2h`  `/cancel {p.id}`")
    return "\n".join(lines)


async def notify(text: str) -> None:
    await client.send_message("me", text, link_preview=False, parse_mode="md")


def chat_title(entity) -> str:
    if isinstance(entity, User):
        name = " ".join(filter(None, [entity.first_name, entity.last_name]))
        return name or (entity.username or str(entity.id))
    return getattr(entity, "title", None) or str(entity.id)


def message_link(entity, msg_id: int) -> str | None:
    username = getattr(entity, "username", None)
    if isinstance(entity, Channel):
        return f"https://t.me/{username}/{msg_id}" if username else f"https://t.me/c/{entity.id}/{msg_id}"
    if isinstance(entity, User) and username:
        return f"https://t.me/{username}"
    return None


def is_watched(entity) -> bool:
    if entity is None or getattr(entity, "id", None) == ME_ID:
        return False
    keys = {str(entity.id), str(getattr(entity, "username", "") or "").lower()}
    if keys & EXCLUDE:
        return False
    if isinstance(entity, User):
        return not entity.bot
    if isinstance(entity, Chat):
        return INCLUDE_GROUPS
    if isinstance(entity, Channel):
        return INCLUDE_GROUPS and entity.megagroup      # каналы-витрины не читаем
    return False


def author_name(msg) -> str:
    if msg.out:
        return "Я"
    s = msg.sender
    if isinstance(s, User):
        return " ".join(filter(None, [s.first_name, s.last_name])) or s.username or "собеседник"
    return getattr(s, "title", None) or "собеседник"


def msg_text(msg) -> str:
    text = (msg.message or "").strip()
    if not text and msg.media:
        text = "[медиа без подписи]"
    return text[:1500]


def parse_local(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).replace(tzinfo=TZ).astimezone(timezone.utc) if value else None
    except ValueError:
        return None


# ───────────────────────── анализ ─────────────────────────

async def analyze(entity, messages: list, last_seen: int, *, announce: bool = True) -> int:
    """Отправляет кусок переписки в Claude и сохраняет найденные обещания.
    messages — в хронологическом порядке. Возвращает число новых обещаний."""
    if not any(m.out and m.id > last_seen and msg_text(m) for m in messages):
        return 0

    title = chat_title(entity)
    lines = [ChatLine(m.id, author_name(m), local(m.date), msg_text(m), m.id > last_seen)
             for m in messages if msg_text(m)]
    open_items = [(p.id, p.task) for p in db.open_promises(entity.id)]

    result = await extractor.extract(title, lines, open_items, local(utcnow()))

    open_ids = {pid for pid, _ in open_items}
    for pid in result.completed_ids:
        if pid in open_ids and db.set_status(pid, "done") and announce:
            p = db.by_id(pid)
            await notify(f"✅ Похоже, ты выполнил #{pid}: {p.task}. Закрыл. "
                         f"Если это ошибка — `/reopen {pid}`.")

    own_ids = {m.id for m in messages if m.out}
    added = 0
    for fp in result.promises:
        if fp.message_id not in own_ids:
            continue
        p = db.add(chat_id=entity.id, chat_title=title, msg_id=fp.message_id,
                   link=message_link(entity, fp.message_id), quote=fp.quote[:200],
                   task=fp.task, counterpart=fp.counterpart or None, due_at=parse_local(fp.due_at))
        if p:
            added += 1
            if announce:
                await notify(card(p, "📌 **Зафиксировал обещание**"))
    return added


async def analyze_chat_later(chat_id: int) -> None:
    """Ждём, пока разговор утихнет, и анализируем свежие сообщения чата."""
    try:
        await asyncio.sleep(DEBOUNCE)
    except asyncio.CancelledError:
        return
    pending.pop(chat_id, None)
    async with locks.setdefault(chat_id, asyncio.Lock()):
        try:
            entity = await client.get_entity(chat_id)
            msgs = list(reversed(await client.get_messages(entity, limit=CONTEXT_MESSAGES)))
            last_seen = int(db.get(f"last:{chat_id}", "0"))
            await analyze(entity, msgs, last_seen)
            if msgs:
                db.set(f"last:{chat_id}", str(max(m.id for m in msgs)))
        except Exception:
            log.exception("Не удалось проанализировать чат %s (повторю при следующем сообщении)", chat_id)


@client.on(events.NewMessage(outgoing=True))
async def on_my_message(event: events.NewMessage.Event) -> None:
    if event.chat_id == ME_ID:
        return
    entity = await event.get_chat()
    if not is_watched(entity):
        return
    if task := pending.get(event.chat_id):
        task.cancel()
    pending[event.chat_id] = asyncio.create_task(analyze_chat_later(event.chat_id))


# ───────────────────────── команды в «Избранном» ─────────────────────────

HELP = __doc__.split("Команды пишутся в «Избранное»:")[1]


def parse_delta(arg: str) -> datetime | None:
    arg = arg.strip().lower()
    if arg in ("tomorrow", "завтра", "ertaga"):
        d = local(utcnow()) + timedelta(days=1)
        return d.replace(hour=10, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    m = re.fullmatch(r"(\d+)\s*(m|min|м|мин|h|ч|d|д)", arg)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"m": "minutes", "min": "minutes", "м": "minutes", "мин": "minutes",
             "h": "hours", "ч": "hours", "d": "days", "д": "days"}[unit]
    return utcnow() + timedelta(**{delta: n})


@client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"^/(\w+)\s*(.*)$"))
async def on_command(event: events.NewMessage.Event) -> None:
    cmd, arg = event.pattern_match.group(1).lower(), event.pattern_match.group(2).strip()

    if cmd in ("help", "start"):
        await notify("🤖 **Simple Agent** — слежу за твоими обещаниями.\n" + HELP)

    elif cmd == "list":
        items = db.open_promises()
        if not items:
            await notify("✨ Открытых обещаний нет. Ты чист.")
        for p in items:
            await notify(card(p))

    elif cmd in ("done", "cancel"):
        if not arg.isdigit():
            await notify(f"Формат: `/{cmd} 12`")
        elif db.set_status(int(arg), "done" if cmd == "done" else "cancelled"):
            await notify("✅ Отлично, закрыто." if cmd == "done" else "🗑 Убрал из списка.")
        else:
            await notify(f"Не нашёл открытое обещание #{arg}.")

    elif cmd == "reopen":
        if arg.isdigit() and db.reopen(int(arg)):
            await notify(card(db.by_id(int(arg)), "↩️ **Вернул в работу**"))
        else:
            await notify("Формат: `/reopen 12` (номер закрытого обещания)")

    elif cmd == "snooze":
        parts = arg.split(maxsplit=1)
        until = parse_delta(parts[1]) if len(parts) == 2 and parts[0].isdigit() else None
        if not until:
            await notify("Формат: `/snooze 12 2h` (также 30m, 1d, завтра)")
        elif db.snooze(int(parts[0]), until):
            await notify(f"⏰ Напомню {local(until):%d.%m в %H:%M}.")
        else:
            await notify(f"Не нашёл открытое обещание #{parts[0]}.")

    elif cmd == "scan":
        days = int(arg) if arg.isdigit() else 3
        await notify(f"🔎 Сканирую переписку за {days} дн. Это может занять несколько минут…")
        asyncio.create_task(scan_history(days))


async def scan_history(days: int) -> None:
    """Разовый проход по истории: находит обещания, данные до запуска бота."""
    since = utcnow() - timedelta(days=days)
    found = chats = 0
    async for dialog in client.iter_dialogs():
        if dialog.date and dialog.date < since:
            if dialog.pinned:                        # закреплённые идут первыми вне порядка
                continue
            break                                    # остальные отсортированы по свежести
        if not is_watched(dialog.entity):
            continue
        msgs = []
        async for m in client.iter_messages(dialog.entity, offset_date=None, limit=400):
            if m.date < since:
                break
            msgs.append(m)
        msgs.reverse()
        if not any(m.out for m in msgs):
            continue
        chats += 1
        async with locks.setdefault(dialog.id, asyncio.Lock()):
            # окна по 40 сообщений с перекрытием 10, чтобы не терять контекст
            for start in range(0, len(msgs), 30):
                window = msgs[max(0, start - 10): start + 30]
                last_seen = msgs[start - 1].id if start else 0
                try:
                    found += await analyze(dialog.entity, window, last_seen, announce=False)
                except Exception:
                    log.exception("Ошибка при сканировании %s", chat_title(dialog.entity))
            db.set(f"last:{dialog.id}", str(max(m.id for m in msgs)))
    await notify(f"🔎 Готово: проверил {chats} чатов, нашёл {found} обещаний. Отправь /list, чтобы увидеть.")


# ───────────────────────── напоминания ─────────────────────────

async def reminder_loop() -> None:
    while True:
        try:
            now = utcnow()
            for p in db.open_promises():
                if p.snoozed_until and p.snoozed_until > now:
                    continue
                if p.snoozed_until and (not p.last_reminded_at or p.last_reminded_at < p.snoozed_until):
                    await notify(card(p, "⏰ **Отложенное напоминание**"))
                    db.mark_reminded(p.id)
                    continue
                if not p.due_at:
                    continue
                if not p.pre_reminded and now < p.due_at <= now + PRE_REMIND:
                    await notify(card(p, "🔔 **Через час срок обещания**"))
                    db.mark_reminded(p.id, pre=True)
                elif p.due_at <= now and (not p.last_reminded_at or now - p.last_reminded_at >= OVERDUE_REPEAT):
                    header = "⏰ **Пора выполнить обещание**" if not p.last_reminded_at else "⚠️ **Всё ещё не выполнено**"
                    await notify(card(p, header))
                    db.mark_reminded(p.id)

            # утренний дайджест раз в день
            today = local(now).date().isoformat()
            if local(now).hour >= DIGEST_HOUR and db.get("digest") != today:
                db.set("digest", today)
                await send_digest()
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(60)


async def send_digest() -> None:
    items = db.open_promises()
    if not items:
        return
    now = utcnow()
    overdue = [p for p in items if p.due_at and p.due_at < now]
    today = [p for p in items if p.due_at and p.due_at >= now and local(p.due_at).date() == local(now).date()]
    later = [p for p in items if p.due_at and local(p.due_at).date() > local(now).date()]
    no_due = [p for p in items if not p.due_at]

    def block(title: str, ps: list[Promise]) -> list[str]:
        return [f"\n**{title}**"] + [f"#{p.id} · {p.task} — {p.counterpart or p.chat_title} ({fmt_due(p)})"
                                     for p in ps] if ps else []

    text = ["☀️ **Доброе утро! Твои обещания:**"]
    text += block("🔴 Просрочено", overdue)
    text += block("🟡 Сегодня", today)
    text += block("⚪️ Без срока", no_due)
    text += block("🟢 Позже", later)
    text.append("\nЗакрыть: `/done N`  ·  Подробно: `/list`")
    await notify("\n".join(text))


# ───────────────────────── запуск ─────────────────────────

async def main() -> None:
    global ME_ID
    await client.start()                             # при первом запуске спросит телефон и код
    me = await client.get_me()
    ME_ID = me.id
    log.info("Simple Agent запущен для %s, модель %s", me.first_name, MODEL)
    if not db.get("welcomed"):
        db.set("welcomed", "1")
        await notify("🤖 **Simple Agent на связи.**\nСлежу за твоими обещаниями во всех чатах "
                     "и буду напоминать о них здесь.\nНачни с `/scan 3` — найду обещания за последние 3 дня.\n"
                     + HELP)
    asyncio.create_task(reminder_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
