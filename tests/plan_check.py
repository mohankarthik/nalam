"""One-off: print the Drive -> Paperless sync plan. Verifies the folder maps."""

import collections
import logging

from src.drive_sync import collect

logging.basicConfig(level=logging.WARNING)

docs, skipped = collect()
print(f"consumable:   {len(docs)}")
print(f"unconsumable: {len(skipped)}")
for s in skipped:
    print(f"    - {s}")
print(f"with a date:  {sum(1 for d in docs if d.created)} / {len(docs)}")

print("\nper person:")
for p, n in collections.Counter(d.correspondent for d in docs).most_common():
    print(f"  {p:<18} {n}")

print("\nper tag:")
for t, n in collections.Counter(d.tag for d in docs).most_common():
    print(f"  {t:<28} {n}")

print("\nsample:")
for d in docs[:5]:
    print(f"  [{d.correspondent}] {d.tag} | {d.created} | {d.title!r}")
