import time

import pytest

from finder.bot import FinderBot
from finder.config import parse_config
from finder.posters import render, wa_link

PHOTO = b"\xff\xd8fake-jpeg"


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def test_save_then_ask_where_returns_spot_route_and_photo(config, store):
    bot = FinderBot(config, store)
    [r] = bot.handle_text("911", "SAVE E2")
    assert "E2: East gate, right of the steps" in r.text and "Send a photo" in r.text
    [r] = bot.handle_photo("911", PHOTO, "image/jpeg")
    assert "Photo saved with spot *E2" in r.text

    text, photo = bot.handle_text("911", "where are my chappals?")
    assert "E2: East gate, right of the steps" in text.text
    assert "Exit 2" in text.text and "FOUND" in text.text
    assert photo.image == PHOTO


def test_photo_first_then_code(config, store):
    bot = FinderBot(config, store)
    [r] = bot.handle_photo("911", PHOTO, "image/jpeg")
    assert "reply with the code" in r.text
    [r] = bot.handle_text("911", "where?")
    assert "Which spot" in r.text
    [r] = bot.handle_text("911", "n1")  # lower case, no SAVE
    assert "N1: North gate" in r.text and "Send a photo" not in r.text  # photo kept for this visit
    assert store.get("911")["photo"] == PHOTO


@pytest.mark.parametrize("word", ["FOUND", "found!", "Got it", "mil gaya", "मिल गया"])
def test_found_deletes_everything(config, store, word):
    bot = FinderBot(config, store)
    bot.handle_text("911", "SAVE E1")
    bot.handle_photo("911", PHOTO, "image/jpeg")
    [r] = bot.handle_text("911", word)
    assert "Glad you found it" in r.text
    assert store.get("911") is None


def test_unknown_code_and_help(config, store):
    bot = FinderBot(config, store)
    [r] = bot.handle_text("911", "SAVE Z9")
    assert "don't know spot *Z9*" in r.text
    [r] = bot.handle_text("911", "hello")
    assert "Scan the QR code" in r.text
    assert store.get("911") is None


def test_codes_must_match_whole_words(config, store):
    bot = FinderBot(config, store)
    [r] = bot.handle_text("911", "I parked near E15 and P10")  # not E1 / P1
    assert "Scan the QR code" in r.text


def test_new_visit_next_day_drops_old_photo(config, store):
    clock = Clock()
    bot = FinderBot(config, store, clock=clock)
    bot.handle_text("911", "SAVE E1")
    bot.handle_photo("911", PHOTO, "image/jpeg")
    clock.t += 3600
    [r] = bot.handle_text("911", "SAVE N1")
    assert "Send a photo" in r.text
    assert store.get("911")["photo"] is None


def test_people_are_kept_separate(config, store):
    bot = FinderBot(config, store)
    bot.handle_text("911", "SAVE E1")
    bot.handle_text("922", "SAVE P1")
    assert "E1" in bot.handle_text("911", "?")[0].text
    assert "P1" in bot.handle_text("922", "?")[0].text
    assert store.counts_by_spot() == {"E1": 1, "P1": 1}


def test_purge_deletes_old_records(config, store):
    bot = FinderBot(config, store)
    bot.handle_text("911", "SAVE E1")
    assert store.purge(retention_seconds=3600) == 0
    assert store.purge(retention_seconds=-1) == 1
    assert store.get("911") is None


def test_config_validation():
    with pytest.raises(ValueError, match="Duplicate"):
        parse_config({"spots": [{"code": "E1", "name": "a"}, {"code": "e1", "name": "b"}]})
    with pytest.raises(ValueError, match="letters and digits"):
        parse_config({"spots": [{"code": "E-1", "name": "a"}]})


def test_posters_have_one_qr_per_spot(config):
    page = render(config)
    assert page.count("<svg") == len(config.spots)
    assert "SAVE E1" in page and "East gate, left of the steps" in page
    assert wa_link("919800000000", "E1") == "https://wa.me/919800000000?text=SAVE%20E1"
