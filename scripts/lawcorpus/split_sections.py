"""Split an Act 333 PDF into one text file per section.

Fixed-length chunking cuts a statute mid-provision, which makes retrieval return
neighbouring sections with indistinguishable scores. One file per section keeps
each provision whole, so a retrieved chunk is a citable unit.

    python scripts/lawcorpus/split_sections.py act333.pdf out_dir/
"""

import pathlib
import re
import sys

from pypdf import PdfReader

SRC = sys.argv[1] if len(sys.argv) > 1 else "act333.pdf"
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "sections")
OUT.mkdir(parents=True, exist_ok=True)

reader = PdfReader(SRC)
raw = "\n".join((p.extract_text() or "") for p in reader.pages)
text = re.sub(r"[ \t]+", " ", raw)

# Drop the table of contents: the body is where provisions actually carry text.
anchor = text.find("shall be guilty of an offence")
body = text[max(0, anchor - 25000) :]

# Section openers look like "42. (1) Any person who..." or "76A. (1) ...".
pattern = re.compile(r"\n\s*(\d+[A-Z]?)\.\s*\((1)\)")
marks = [(m.start(), m.group(1)) for m in pattern.finditer(body)]

written, skipped = [], 0
for i, (pos, num) in enumerate(marks):
    end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
    chunk = re.sub(r"\s+", " ", body[pos:end]).strip()
    chunk = re.sub(r"\d+ Laws of Malaysia ACT 333", " ", chunk)
    chunk = re.sub(r"Road Transport \d+", " ", chunk)
    chunk = re.sub(r"\s+", " ", chunk).strip()
    if len(chunk) < 150:
        skipped += 1
        continue
    # Look backwards for the marginal heading that names this section.
    before = re.sub(r"\s+", " ", body[max(0, pos - 220) : pos]).strip()
    # The heading is the tail of the preceding text; the previous provision ends
    # with a full stop, so take what follows the last sentence boundary.
    heading = re.split(r"(?<=[.;:])\s+", before)[-1].strip()
    heading = re.sub(r"^[a-z)\d\s]*", "", heading)[:110].strip() or "(untitled)"

    header = f"Road Transport Act 1987 (Act 333), Malaysia\nSection {num}\nHeading: {heading}\n\n"
    path = OUT / f"act333-s{num}.txt"
    path.write_text(header + chunk[:6000], encoding="utf-8")
    written.append((num, heading[:60], len(chunk)))

print(f"sections written: {len(written)}  (skipped short: {skipped})")
for num, head, n in written[:8]:
    print(f"  s.{num:<5} {n:>5} chars  {head}")
print("  ...")
for target in ("42", "43", "79"):
    p = OUT / f"act333-s{target}.txt"
    if p.exists():
        first = p.read_text(encoding="utf-8").splitlines()[2]
        print(f"  s.{target}: {first}")
