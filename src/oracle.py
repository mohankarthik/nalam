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


NO_ORACLE = Oracle(text="", source="none")


def build(
    text_layer: str, ocr_text: Optional[str] = None, min_chars: int = 40
) -> Oracle:
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
