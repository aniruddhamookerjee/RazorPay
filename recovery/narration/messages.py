"""Customer-facing recovery messages, in English and Hinglish.

**What the Day 7 quality check found**, because it changed the design:

1. **Zero-shot Hinglish does not work.** Asked plainly for Hinglish,
   `qwen2.5:7b-instruct` returned fluent plain English with no Hindi at all.
   Few-shot examples plus an explicit list of expected Hindi markers fixed it —
   9-12 markers per message, naturally code-mixed. The prompt below carries
   those examples for that reason, not for decoration.

2. **Raw cause codes leaked into customer text.** One message read
   "aapka charge bank_unavailable se fail hua". The model will happily print an
   internal enum at a customer. Only human-readable descriptions are ever put
   in the prompt now.

3. **~15 seconds per message.** Generating per customer across 10,000 events is
   not possible and never was. Messages are therefore generated as **templates**
   — one per (cause, language, channel) — and filled with name and amount
   locally. Roughly 50 model calls covers the whole book, cached.

Every path degrades to a written template if the model is unavailable, because
a demo that cannot send a message when Ollama is down is worse than one that
sends a plainer message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import httpx

from recovery.config import settings
from recovery.models import Action, Cause


class Language(str, Enum):
    ENGLISH = "en"
    HINGLISH = "hinglish"


class Channel(str, Enum):
    SMS = "sms"
    EMAIL = "email"
    WHATSAPP = "whatsapp"


# What the customer is told, in words rather than enum values. The model never
# sees a `Cause` member — see the docstring.
CAUSE_DESCRIPTIONS: dict[Cause, str] = {
    Cause.INSUFFICIENT_FUNDS: "there was not enough balance in the account that day",
    Cause.CARD_EXPIRED: "the saved card has expired",
    Cause.MANDATE_EXPIRED: "the standing payment permission has lapsed",
    Cause.MANDATE_REVOKED: "the standing payment permission is no longer active",
    Cause.BANK_UNAVAILABLE: "the bank's system was temporarily unavailable",
    Cause.NETWORK_TIMEOUT: "the payment did not get a clear response in time",
    Cause.HARD_DECLINE: "the bank declined the payment",
    Cause.AUTHENTICATION_REQUIRED: "the payment needs the customer to confirm it",
    Cause.UNKNOWN: "the payment could not be completed",
}

# What we want them to do. Kept separate from the cause so the ask is explicit
# rather than inferred by the model.
ACTION_ASKS: dict[Cause, str] = {
    Cause.INSUFFICIENT_FUNDS: "top up the account; we will try again automatically",
    Cause.CARD_EXPIRED: "add an up-to-date card",
    Cause.MANDATE_EXPIRED: "re-authorise the payment permission",
    Cause.MANDATE_REVOKED: "set up the payment permission again if they wish to continue",
    Cause.BANK_UNAVAILABLE: "do nothing; we will retry shortly",
    Cause.NETWORK_TIMEOUT: "do nothing; we will retry shortly",
    Cause.HARD_DECLINE: "use a different payment method",
    Cause.AUTHENTICATION_REQUIRED: "complete the verification step",
    Cause.UNKNOWN: "check the payment method",
}

_ENGLISH_SYSTEM = """You write short payment-recovery messages for customers of \
a subscription business.

Rules: 2 sentences maximum. Polite, never pushy, never blame the customer. Say \
what happened and the single thing they should do. No emoji. No "Dear Sir". \
Use {name} and {amount} as literal placeholders. Output only the message."""

# The Hinglish prompt carries examples and an explicit marker list because
# without them the model returns plain English. See the module docstring.
_HINGLISH_SYSTEM = """You write payment reminders in HINGLISH for Indian customers.

Hinglish means Hindi words written in Latin script, mixed with English. It is \
NOT English. Every message MUST contain Hindi words such as: aapka, hua, nahi, \
kripya, karein, ho gaya, thoda, phir se, dobara, paisa, khata, jaldi.

Examples of correct Hinglish:
- "{name} ji, aapka Rs {amount} ka payment complete nahi ho paya kyunki khaate \
mein balance thoda kam tha. Kripya account top-up karke dobara try karein."
- "{name} ji, aapka card expire ho gaya hai isliye Rs {amount} ka payment fail \
hua. App mein jaakar naya card add kar dijiye."

Rules: 2 sentences maximum. Polite, never blame the customer. No emoji. Use \
{name} and {amount} as literal placeholders. Output only the message."""

_CHANNEL_NOTE = {
    Channel.SMS: "Keep it under 160 characters.",
    Channel.WHATSAPP: "Conversational tone is fine.",
    Channel.EMAIL: "A slightly fuller sentence is fine.",
}

# Written by hand, per cause, in both languages. These are what ships whenever
# the model is unavailable or its output is rejected, so they are the floor for
# message quality rather than a placeholder.
#
# The first version composed them generically from CAUSE_DESCRIPTIONS and
# ACTION_ASKS, which produced third-person leaks ("if they wish to continue"),
# a tautology on `unknown` ("could not be completed because the payment could
# not be completed"), and Hinglish that was an English sentence in a Hindi
# wrapper. Generic composition is not good enough for text a customer reads.
_FALLBACKS: dict[Cause, dict[Language, str]] = {
    Cause.INSUFFICIENT_FUNDS: {
        Language.ENGLISH: (
            "Hi {name}, we could not collect Rs {amount} because there was not "
            "enough balance that day. Please top up and we will try again."
        ),
        Language.HINGLISH: (
            "{name} ji, aapka Rs {amount} ka payment nahi ho paya kyunki khaate "
            "mein balance thoda kam tha. Account top-up kar dijiye, hum phir se "
            "try karenge."
        ),
    },
    Cause.CARD_EXPIRED: {
        Language.ENGLISH: (
            "Hi {name}, your saved card has expired, so we could not collect "
            "Rs {amount}. Please add an up-to-date card."
        ),
        Language.HINGLISH: (
            "{name} ji, aapka card expire ho gaya hai isliye Rs {amount} ka "
            "payment nahi ho paya. Kripya naya card add kar dijiye."
        ),
    },
    Cause.MANDATE_EXPIRED: {
        Language.ENGLISH: (
            "Hi {name}, the standing permission for your Rs {amount} payment has "
            "lapsed. Please re-authorise it to continue your subscription."
        ),
        Language.HINGLISH: (
            "{name} ji, aapki payment permission expire ho gayi hai, isliye "
            "Rs {amount} nahi liya ja saka. Kripya dobara authorise kar dijiye."
        ),
    },
    Cause.MANDATE_REVOKED: {
        Language.ENGLISH: (
            "Hi {name}, the standing permission for your Rs {amount} payment is "
            "no longer active. If you would like to continue, please set it up again."
        ),
        Language.HINGLISH: (
            "{name} ji, aapki payment permission ab active nahi hai, isliye "
            "Rs {amount} nahi liya ja saka. Agar aap continue karna chahte hain "
            "to kripya dobara set kar dijiye."
        ),
    },
    Cause.BANK_UNAVAILABLE: {
        Language.ENGLISH: (
            "Hi {name}, your bank was briefly unreachable so the Rs {amount} "
            "payment did not go through. Nothing is needed from you, we will retry."
        ),
        Language.HINGLISH: (
            "{name} ji, bank ka system thodi der ke liye down tha isliye "
            "Rs {amount} ka payment nahi ho paya. Aapko kuch nahi karna hai, hum "
            "phir se try karenge."
        ),
    },
    Cause.NETWORK_TIMEOUT: {
        Language.ENGLISH: (
            "Hi {name}, the Rs {amount} payment did not get a clear response in "
            "time. Nothing is needed from you, we will retry shortly."
        ),
        Language.HINGLISH: (
            "{name} ji, Rs {amount} ke payment ka jawab time par nahi aaya. "
            "Aapko kuch nahi karna hai, hum thodi der mein phir se try karenge."
        ),
    },
    Cause.HARD_DECLINE: {
        Language.ENGLISH: (
            "Hi {name}, your bank declined the Rs {amount} payment. Please try a "
            "different payment method."
        ),
        Language.HINGLISH: (
            "{name} ji, aapke bank ne Rs {amount} ka payment decline kar diya. "
            "Kripya koi doosra payment method use kar dijiye."
        ),
    },
    Cause.AUTHENTICATION_REQUIRED: {
        Language.ENGLISH: (
            "Hi {name}, the Rs {amount} payment needs you to confirm it. Please "
            "complete the verification step."
        ),
        Language.HINGLISH: (
            "{name} ji, Rs {amount} ke payment ke liye aapka confirmation chahiye. "
            "Kripya verification step complete kar dijiye."
        ),
    },
    Cause.UNKNOWN: {
        Language.ENGLISH: (
            "Hi {name}, we were not able to collect Rs {amount} this time. Please "
            "check your saved payment method."
        ),
        Language.HINGLISH: (
            "{name} ji, is baar Rs {amount} ka payment nahi ho paya. Kripya apna "
            "saved payment method check kar lijiye."
        ),
    },
}

# Hindi markers used to verify that Hinglish output is actually Hinglish.
_HINDI_MARKERS = frozenset(
    {
        "aapka", "aap", "hua", "huye", "nahi", "kripya", "karein", "gaya",
        "thoda", "jaldi", "phir", "dobara", "paisa", "khata", "khaate", "hai",
        "ho", "se", "mein", "ji", "kar", "liye", "raha", "koi", "dijiye",
        "jaakar", "kyunki", "isliye", "naya", "paya",
    }
)

MIN_HINDI_MARKERS = 3


def _has_invented_numbers(text: str) -> bool:
    """Any digit that is not part of the {amount} placeholder is a fabrication."""
    stripped = text.replace("{amount}", "").replace("{name}", "")
    return any(ch.isdigit() for ch in stripped)


def looks_like_hinglish(text: str) -> bool:
    """Does this actually contain Hindi, or is it English wearing a label?

    The check exists because the first quality run produced fluent English
    every time. Shipping that as "Hinglish" to a room of Hindi speakers would
    be worse than shipping English and saying so.
    """
    words = set(text.lower().replace(",", " ").replace(".", " ").split())
    return len(words & _HINDI_MARKERS) >= MIN_HINDI_MARKERS


@dataclass
class MessageWriter:
    """Generates one template per (cause, language, channel), then caches it."""

    use_model: bool = True
    _cache: dict[tuple[Cause, Language, Channel], str] = field(
        default_factory=dict, repr=False
    )
    model_calls: int = 0
    fallbacks_used: int = 0
    hinglish_rejections: int = 0

    def template(
        self, cause: Cause, language: Language, channel: Channel = Channel.SMS
    ) -> str:
        """A message template with {name} and {amount} placeholders."""
        key = (cause, language, channel)
        if key in self._cache:
            return self._cache[key]

        text = self._fallback(cause, language)
        if self.use_model:
            generated = self._ask_model(cause, language, channel)
            if generated:
                text = generated
            else:
                self.fallbacks_used += 1

        self._cache[key] = text
        return text

    def write(
        self,
        *,
        cause: Cause,
        name: str,
        amount_paise: int,
        language: Language = Language.ENGLISH,
        channel: Channel = Channel.SMS,
    ) -> str:
        """A finished message. Placeholders filled locally, never by the model."""
        template = self.template(cause, language, channel)
        return template.replace("{name}", name).replace(
            "{amount}", f"{amount_paise / 100:,.0f}"
        )

    # -- internals ---------------------------------------------------------

    def _fallback(self, cause: Cause, language: Language) -> str:
        return _FALLBACKS[cause][language]

    def _ask_model(
        self, cause: Cause, language: Language, channel: Channel
    ) -> str | None:
        system = (
            _HINGLISH_SYSTEM if language is Language.HINGLISH else _ENGLISH_SYSTEM
        )
        # Descriptions, never enum values: the model printed a raw cause code
        # at a customer during the Day 7 check.
        ask = ACTION_ASKS[cause]
        # Stated as a constraint, not a hint. Asked loosely, the model told a
        # customer whose bank was down to go and check their account, when the
        # correct message was that no action was needed.
        prompt = (
            f"Situation: {CAUSE_DESCRIPTIONS[cause]}. "
            f"The customer must be told to: {ask}. "
            "Do not ask them to do anything other than that. "
            "Do not invent dates, times or figures. "
            f"{_CHANNEL_NOTE[channel]}"
        )

        self.model_calls += 1
        try:
            response = httpx.post(
                f"{settings.ollama_host}/api/chat",
                timeout=settings.llm_timeout_seconds,
                json={
                    "model": settings.llm_model_hinglish
                    if language is Language.HINGLISH
                    else settings.llm_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "options": {"temperature": 0.3},
                },
            )
            response.raise_for_status()
            text = response.json()["message"]["content"].strip().strip('"')
        except Exception:
            return None

        if not text or len(text) > 400:
            return None
        # A raw enum value reaching a customer is worse than a plain template.
        if any(c.value in text for c in Cause):
            return None
        # Reject invented specifics. During the Day 7 check the model produced
        # "there wasn't enough balance on the 15th" — there is no date anywhere
        # in the facts it was given. A message that states a detail we never
        # supplied is a message we cannot stand behind, so any digit outside
        # the {amount} placeholder disqualifies the template.
        if _has_invented_numbers(text):
            return None
        if language is Language.HINGLISH and not looks_like_hinglish(text):
            self.hinglish_rejections += 1
            return None
        return text

    @property
    def stats(self) -> dict[str, int]:
        return {
            "templates_cached": len(self._cache),
            "model_calls": self.model_calls,
            "fallbacks_used": self.fallbacks_used,
            "hinglish_rejections": self.hinglish_rejections,
        }
