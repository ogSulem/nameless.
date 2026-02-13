from __future__ import annotations


def escape_markdown(text: str) -> str:
    # Escape for Telegram legacy Markdown parse_mode="Markdown".
    # We escape characters that can start entities.
    if text is None:
        return ""

    s = str(text)
    s = s.replace("\\", "\\\\")
    for ch in ("_", "*", "[", "]", "(", ")", "`"):
        s = s.replace(ch, f"\\{ch}")
    return s
