"""The conversation. Deliberately simple and predictable: no AI is needed to remember a spot code.

  Devotee scans the QR on board E3  ->  WhatsApp opens with "SAVE E3"  ->  bot confirms
  (optional) sends a photo of their footwear                           ->  bot attaches it
  after darshan, sends anything                                       ->  bot replies with spot,
                                                                          route and photo
  "FOUND"                                                             ->  everything is deleted
"""

import re
import time
from dataclasses import dataclass

from .config import FinderConfig
from .store import Store

# A photo and a spot code sent within this window belong to the same visit.
SAME_VISIT_SECONDS = 15 * 60

FOUND_WORDS = {
    "found", "got it", "gotit", "done", "thanks", "thank you", "ok found",
    "mil gaya", "mila", "मिल गया", "मिल गई", "கிடைத்தது", "దొరికింది", "ಸಿಕ್ಕಿತು", "കിട്ടി",
}
HELP_WORDS = {"help", "hi", "hello", "namaste", "नमस्ते", "start"}


@dataclass
class Reply:
    text: str
    image: bytes | None = None
    image_mime: str = "image/jpeg"


class FinderBot:
    def __init__(self, config: FinderConfig, store: Store, clock=time.time):
        self.config = config
        self.store = store
        self.clock = clock
        codes = sorted(config.spots, key=len, reverse=True)
        self._code_re = re.compile(r"(?<![A-Z0-9])(" + "|".join(map(re.escape, codes)) + r")(?![A-Z0-9])")
        self._any_code_re = re.compile(r"\bSAVE\s*([A-Z]{1,3}\d{1,3})\b")

    def handle_text(self, phone: str, text: str) -> list[Reply]:
        normalised = " ".join(text.strip().lower().split())
        upper = text.upper()
        record = self.store.get(phone)

        if normalised in FOUND_WORDS or normalised.rstrip("!. ") in FOUND_WORDS:
            if record:
                self.store.delete(phone)
                return [Reply("🙏 Glad you found it! Your saved spot and photo have been deleted.\n"
                              "Please walk with the flow of people, not against it. Have a blessed day.")]
            return [self._instructions()]

        if match := self._code_re.search(upper):
            return [self._save_spot(phone, match.group(1), record)]
        if match := self._any_code_re.search(upper):
            return [Reply(f"I don't know spot *{match.group(1)}*. Please check the code on the board next to "
                          "your footwear and send it again, e.g. *SAVE E1*.")]

        if record and record["spot_code"]:
            return self._where(record)
        if record:  # photo but no spot yet
            return [Reply("Which spot did you leave your footwear at? Reply with the code on the nearest "
                          f"board, e.g. *{self._example_code()}*.")]
        if normalised in HELP_WORDS:
            return [self._instructions()]
        return [self._instructions()]

    def mentions_spot(self, text: str) -> bool:
        return bool(text) and bool(self._code_re.search(text.upper()))

    def handle_photo(self, phone: str, photo: bytes, mime: str) -> list[Reply]:
        self.store.save_photo(phone, photo, mime)
        record = self.store.get(phone)
        if record and record["spot_code"]:
            spot = self.config.spots.get(record["spot_code"])
            where = f" with spot *{spot.code}: {spot.name}*" if spot else ""
            return [Reply(f"📸 Photo saved{where}. After darshan, send me any message and I'll show you "
                          "where your footwear is.")]
        return [Reply("📸 Photo saved. Now reply with the code on the board next to your footwear, "
                      f"e.g. *{self._example_code()}*.")]

    def _save_spot(self, phone: str, code: str, record: dict | None) -> Reply:
        spot = self.config.spots[code]
        same_visit = record is not None and self.clock() - record["updated_at"] < SAME_VISIT_SECONDS
        self.store.save_spot(phone, code, keep_photo=same_visit)
        has_photo = same_visit and record and record["photo"]
        text = f"✅ Saved: your footwear is at *{spot.code}: {spot.name}*.\n"
        if not has_photo:
            text += "📸 Send a photo of your footwear too, so it's easy to recognise.\n"
        text += "After darshan, send me any message and I'll guide you back."
        return Reply(text)

    def _where(self, record: dict) -> list[Reply]:
        spot = self.config.spots.get(record["spot_code"])
        if spot is None:  # spot removed from config since it was saved
            return [Reply(f"Your footwear was saved at spot {record['spot_code']}. Please ask a volunteer.")]
        text = f"👟 Your footwear is at *{spot.code}: {spot.name}*."
        if spot.route:
            text += f"\n🚶 {spot.route}"
        text += "\nReply *FOUND* once you have it."
        if record["photo"]:
            return [Reply(text), Reply("Your photo:", image=record["photo"], image_mime=record["photo_mime"])]
        return [Reply(text)]

    def _instructions(self) -> Reply:
        return Reply(
            f"🙏 Welcome to {self.config.site_name}.\n"
            "I help you find your footwear after darshan:\n"
            "1. Scan the QR code on the board where you leave your footwear, or send *SAVE* and the "
            f"board's code (e.g. *SAVE {self._example_code()}*).\n"
            "2. Send a photo of your footwear (optional).\n"
            "3. After darshan, message me and I'll show you where it is."
        )

    def _example_code(self) -> str:
        return next(iter(self.config.spots))
