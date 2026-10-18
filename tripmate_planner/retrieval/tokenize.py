"""jieba tokenization with POI names and sandbox enum values as a user dictionary."""

import re
import threading
from functools import lru_cache

import jieba

from tripmate_planner.env.enums import get_enums
from tripmate_planner.paths import CACHE

_PUNCT = re.compile(r"^[\s\W_]+$", re.UNICODE)
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _tokenizer() -> jieba.Tokenizer:
    tok = jieba.Tokenizer()
    tok.tmp_dir = str(CACHE / "jieba")
    tok.initialize()
    enums = get_enums()
    words = {n for names in enums.names.values() for n in names}
    for field in ("attraction_types", "cuisines", "hotel_features"):
        for value in enums.union(field):
            words.update(p for p in value.split("/") if p)
            words.add(value)
    for word in words:
        if word.strip():
            tok.add_word(word.strip(), freq=100000)
    return tok


def tokenize(text: str) -> list[str]:
    tok = _tokenizer()
    with _lock:
        tokens = tok.lcut(text.lower())
    return [t for t in tokens if t.strip() and not _PUNCT.match(t)]
