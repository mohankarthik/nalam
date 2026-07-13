"""The token oracle: an INDEPENDENT reading of the document, to check the model against.

The extractor's whole safety model is that a value the model reports must also
appear in a reading of the document that the model did not produce. For a
digital PDF that is the embedded text layer -- the document's own bytes, never
mangled.

But most scanned prescriptions have NO text layer. Nearly two thirds of them are
images of printed pages. Without an oracle a vision model could invent a drug
name and nothing would catch it, and a wrong drug name is the most dangerous
thing this system can emit.

So there is a second oracle: **Paperless has already OCR'd every document with
Tesseract.** That is a completely different engine reading the same pixels. If
the vision model reports "Metformin" and Tesseract independently saw "Metformin",
that is real corroboration -- not the model marking its own homework.

    digital PDF   -> embedded text layer   (strong: the document's own bytes)
    scanned PDF   -> Paperless OCR text    (real: an independent engine)
    neither       -> NO ORACLE -> human review, always

Tesseract garbles printed text in predictable ways, so comparison folds the
classic confusions (l/1/I, rn/m, 0/O) and nothing else. Fold too aggressively and
a hallucinated drug slips through wearing a badge that says "verified".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Tesseract's classic misreadings on printed text. Each group folds to one
# character so that "Glycomet" and "Glvcornet" compare equal -- and no further.
_CONFUSIONS = [
    (r"[0oO]", "0"),
    (r"[1lI|!]", "1"),
    (r"[5sS]", "5"),
    (r"[8bB]", "8"),
    (r"[2zZ]", "2"),
    (r"rn", "m"),
    (r"vv", "w"),
    (r"cl", "d"),
]


def fold(text: str) -> str:
    """Canonicalise for OCR comparison. Lowercase, fold confusions, drop punctuation."""
    t = (text or "").lower()
    t = t.replace("rn", "m").replace("vv", "w")
    for pattern, rep in _CONFUSIONS:
        t = re.sub(pattern, rep, t)
    return re.sub(r"[^a-z0-9]+", "", t)


@dataclass
class Oracle:
    """An independent reading of a document, and where it came from."""

    text: str
    source: str  # "text_layer" | "ocr" | "none"

    @property
    def available(self) -> bool:
        return bool(self.text.strip()) and self.source != "none"

    def corroborates(self, value: str, min_length: int = 3) -> bool:
        """Did the independent reading also see this?

        Deliberately strict: the folded value must appear as a substring of the
        folded oracle. A short token ("PAN", "OD") is not evidence of anything --
        two letters match by chance -- so anything under `min_length` returns
        False and lands in review rather than being waved through.
        """
        if not self.available:
            return False
        needle = fold(value)
        if len(needle) < min_length:
            return False
        return needle in fold(self.text)

    def corroborates_measurement(self, name: str, value: str, window: int = 60) -> bool:
        """Did the independent reading see this measurement -- name AND value, together?

        A bare number is not evidence. `corroborates()` therefore refuses anything
        under three characters, which is correct for a drug name ("PAN", "OD") and
        catastrophic for an echocardiogram: LVIDD 39, IVSd 13, TAPSE 18, HR 94 --
        almost every number on the report is two digits. Checked as bare values they
        are ALL unverifiable, so every measurement on every echo lands in review, and
        the extraction is worthless precisely where it matters most.

        The number alone is weak. The number NEXT TO ITS NAME is strong: "39" could
        be anything, but "LVIDD" followed within a few characters by "39" is the row
        off the report. So corroborate the pair, and require them to be near each
        other -- a 39 elsewhere on the page is not this 39.

        `window` is in FOLDED characters, which have no spaces, so 60 is roughly a
        line of the report. Wide enough to survive OCR putting the value in the next
        column; far too narrow to pair a name with an unrelated number further down.
        """
        if not self.available:
            return False

        needle_name = fold(name)
        needle_value = fold(value)
        if not needle_name or not needle_value:
            return False

        # The name on its own must still be real. Without this, a one-character name
        # would match everywhere and drag any number through with it.
        if len(needle_name) < 3:
            return False

        haystack = fold(self.text)
        start = 0
        while True:
            at = haystack.find(needle_name, start)
            if at < 0:
                return False
            after = haystack[at + len(needle_name) : at + len(needle_name) + window]
            if needle_value in after:
                return True
            start = at + 1

    def coverage(self, prose: str, min_word_length: int = 4) -> float:
        """What fraction of this prose did the independent reading also see?

        `corroborates()` is a substring test, and that is right for a value or a
        drug name -- short, exact, and either there or not. It is useless for a
        radiology impression: one garbled character anywhere in a 40-word
        paragraph breaks the whole substring, and Tesseract garbles something in
        almost every paragraph. Demanding an exact match would send every report
        to review, which is the same as not reading them at all.

        So prose is corroborated word by word. A model that TRANSCRIBED the
        impression will score near 1.0 even through bad OCR, because most words
        survive. A model that INVENTED one scores near 0.0 -- the words are not
        on the page, and no amount of OCR noise puts them there.

        Short words are ignored: "the", "of", "no" appear in every document and
        would pad the score of a fabricated sentence towards passing.

        Returns 0.0 when there is no oracle, and when there is nothing to check --
        an empty impression corroborates nothing, and must not score 1.0 by
        vacuous truth.
        """
        if not self.available:
            return 0.0

        words = [w for w in re.findall(r"[A-Za-z0-9]+", prose or "") if len(w) >= min_word_length]
        if not words:
            return 0.0

        haystack = fold(self.text)
        seen = sum(1 for w in words if fold(w) in haystack)
        return seen / len(words)


NO_ORACLE = Oracle(text="", source="none")


def build(text_layer: str, ocr_text: Optional[str] = None, min_chars: int = 40) -> Oracle:
    """Pick the best available independent reading.

    The embedded text layer wins when there is one: it is the document's own
    bytes and cannot be misread. Paperless' OCR is the fallback for scans. A
    handful of characters is not a reading -- below `min_chars` we say we have
    no oracle rather than pretend.
    """
    if text_layer and len(text_layer.strip()) >= min_chars:
        return Oracle(text=text_layer, source="text_layer")
    if ocr_text and len(ocr_text.strip()) >= min_chars:
        return Oracle(text=ocr_text, source="ocr")
    return NO_ORACLE
