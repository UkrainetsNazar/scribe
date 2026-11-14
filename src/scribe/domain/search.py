import re


def make_snippet(text: str, query: str, width: int = 160) -> str:
    """A window of `width` characters around the first matching word, with ellipses."""
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    lowered = text.lower()
    terms = re.findall(r"\w+", query.lower())
    positions = [p for p in (lowered.find(term) for term in terms) if p >= 0]
    position = min(positions, default=0)
    start = max(0, position - width // 4)
    end = min(len(text), start + width)
    start = max(0, end - width)
    snippet = text[start:end].strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")
