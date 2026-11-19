"""Поиск обещаний в переписке с помощью Claude."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

import anthropic

log = logging.getLogger("simple_agent.extractor")

# Модели, у которых есть серверный фолбэк при отказе модели (refusal).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
                    "claude-fable-5-2", "claude-fable-5-1"}

SYSTEM_PROMPT = """Ты — Simple Agent, личный ассистент владельца Telegram-аккаунта. \
Твоя задача — находить в его переписке ОБЕЩАНИЯ, которые дал он сам, чтобы потом напомнить их выполнить.

В расшифровке сообщения владельца помечены автором «Я». Переписка может быть на русском, узбекском \
(латиница и кириллица) или английском, часто вперемешку.

Обещание — это когда «Я» берёт на себя конкретное действие в будущем:
- прямо: «скину завтра», «выложу пост вечером», «позвоню в 5», «ertaga yuboraman», «I'll send it tonight»;
- согласием на просьбу собеседника: собеседник просит «пришли прайс», а «Я» отвечает «ок», «хоп», «ладно», \
«сделаю», «xo'p», «bo'ladi» — это обещание прислать прайс;
- договорённостью о встрече или созвоне, если «Я» её подтвердил.

НЕ считай обещанием:
- то, что пообещали другие люди (не «Я»);
- уже сделанное («отправил», «вот файл», «yubordim») — а если это выполняет одно из открытых обещаний, \
верни его id в completed_ids;
- вопросы, предположения и пустые вежливости без конкретики («надо как-нибудь увидеться»);
- сообщения, которые лишь пересказывают чужие планы.

Анализируй только сообщения с пометкой [НОВОЕ]; более старые даны для контекста. Не дублируй обещания, \
которые уже есть в списке открытых.

Для каждого обещания верни:
- message_id — id сообщения «Я», где прозвучало обещание (или согласие);
- task — что нужно сделать, коротко, в повелительном наклонении, на языке владельца (по умолчанию русский), \
с конкретикой: кому и что. Например: «Отправить Азизу КП по SMM»;
- counterpart — кому обещано (имя или название чата);
- due_at — срок в формате YYYY-MM-DDTHH:MM по местному времени владельца, вычисленный относительно времени \
сообщения. «завтра» без времени → 10:00 следующего дня; «утром» → 10:00; «днём» → 14:00; \
«вечером» → 19:00; «до конца недели» → пятница 18:00; «на днях» → +2 дня 12:00; «сейчас/скоро/через минуту» → \
время сообщения + 1 час. Если срок не назван — пустая строка;
- quote — дословная цитата обещания (до 200 символов).

Если обещаний нет — верни пустой список. Лучше пропустить сомнительное, чем засорить список."""

SCHEMA = {
    "type": "object",
    "properties": {
        "promises": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "integer"},
                    "task": {"type": "string"},
                    "counterpart": {"type": "string"},
                    "due_at": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["message_id", "task", "counterpart", "due_at", "quote"],
                "additionalProperties": False,
            },
        },
        "completed_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["promises", "completed_ids"],
    "additionalProperties": False,
}


@dataclass
class ChatLine:
    msg_id: int
    author: str          # «Я» или имя собеседника
    sent_at: datetime    # местное время
    text: str
    is_new: bool


@dataclass
class FoundPromise:
    message_id: int
    task: str
    counterpart: str
    due_at: str          # местное время YYYY-MM-DDTHH:MM или ""
    quote: str


@dataclass
class Extraction:
    promises: list[FoundPromise]
    completed_ids: list[int]


WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def _render(chat_title: str, lines: list[ChatLine], open_items: list[tuple[int, str]],
            now_local: datetime) -> str:
    out = [
        f"Сейчас: {now_local:%Y-%m-%d %H:%M}, {WEEKDAYS[now_local.weekday()]}.",
        f"Чат: {chat_title}",
        "",
        "Открытые обещания в этом чате:",
    ]
    out += [f"- id={pid}: {task}" for pid, task in open_items] or ["- нет"]
    out += ["", "<переписка>"]
    for ln in lines:
        mark = " [НОВОЕ]" if ln.is_new else ""
        text = ln.text.replace("\n", " ⏎ ")
        out.append(f"[id={ln.msg_id}] {ln.sent_at:%Y-%m-%d %H:%M} {WEEKDAYS[ln.sent_at.weekday()][:2]} "
                   f"— {ln.author}{mark}: {text}")
    out.append("</переписка>")
    return "\n".join(out)


class PromiseExtractor:
    def __init__(self, model: str):
        self.model = model
        self.client = anthropic.AsyncAnthropic()

    async def extract(self, chat_title: str, lines: list[ChatLine],
                      open_items: list[tuple[int, str]], now_local: datetime) -> Extraction:
        kwargs: dict = {}
        if self.model in _FALLBACK_MODELS:
            # При отказе модели запрос автоматически повторяется на подходящей модели.
            kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

        try:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SCHEMA},
                },
                messages=[{"role": "user",
                           "content": _render(chat_title, lines, open_items, now_local)}],
                **kwargs,
            )
        except anthropic.RateLimitError:
            log.warning("Лимит запросов Claude, чат «%s» будет проверен позже", chat_title)
            raise
        except anthropic.APIStatusError as e:
            log.error("Ошибка Claude API (%s): %s", e.status_code, e.message)
            raise
        except anthropic.APIConnectionError:
            log.error("Нет связи с Claude API")
            raise

        if response.stop_reason == "refusal":
            log.info("Модель отказалась анализировать чат «%s»", chat_title)
            return Extraction([], [])
        if response.stop_reason == "max_tokens":
            log.warning("Ответ обрезан по max_tokens в чате «%s»", chat_title)
            return Extraction([], [])

        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
        return Extraction(
            promises=[FoundPromise(**p) for p in data["promises"]],
            completed_ids=list(data["completed_ids"]),
        )
