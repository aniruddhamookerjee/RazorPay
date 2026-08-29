"""Dump every customer message template for human review.

There are only nine causes and two languages, so the entire set of messages this
system can send is small enough for a person to read in one sitting. That is
worth doing: the Day 7 quality check found a model-written message inventing a
date, and another telling a customer to act when the correct message was that no
action was needed. Automated guards catch the first kind. The second needs eyes.

    .venv/Scripts/python.exe -m recovery.narration
    .venv/Scripts/python.exe -m recovery.narration --no-model
"""

from __future__ import annotations

import argparse

from recovery.models import Cause
from recovery.narration import Channel, Language, MessageWriter, looks_like_hinglish


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--channel", default=Channel.SMS.value)
    args = parser.parse_args()

    writer = MessageWriter(use_model=not args.no_model)
    channel = Channel(args.channel)

    mode = "off" if args.no_model else "on"
    print(f"MESSAGE TEMPLATES   channel={channel.value}   model={mode}")
    print("Review by hand. Ask of each: is the instruction correct, and would a")
    print("native speaker write it this way?")
    print()

    concerns: list[str] = []

    for cause in Cause:
        print(f"-- {cause.value}")
        for language in (Language.ENGLISH, Language.HINGLISH):
            text = writer.template(cause, language, channel)
            flag = ""
            if language is Language.HINGLISH and not looks_like_hinglish(text):
                flag = "   <-- NOT HINGLISH"
                concerns.append(f"{cause.value}/{language.value} reads as English")
            print(f"   {language.value:<9} {text}{flag}")
        print()

    print(f"stats: {writer.stats}")
    if concerns:
        print()
        print("CONCERNS:")
        for concern in concerns:
            print(f"  - {concern}")
    else:
        print()
        print("Every Hinglish template contains Hindi.")
        print("Grammar and tone still need a human read.")


if __name__ == "__main__":
    main()
