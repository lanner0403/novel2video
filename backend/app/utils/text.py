"""文字處理工具：分段、對白擷取、角色名偵測（供離線 mock 與真實前處理共用）。"""

from __future__ import annotations

import re

# 中文對白引號 與 英文引號
_DIALOGUE_RE = re.compile(r"[「『\"]([^」』\"]{1,80})[」』\"]")
_SPEECH_VERBS = "說说問问道喊叫想答應应回笑"
_STOP = set("的了是在我你他她它們们這这那有和與与就都也要不沒没很把被將将之其於于以為为而且並并")


def split_segments(text: str, max_chars: int = 120, min_chars: int = 16) -> list[str]:
    """依標點切句後，貪婪合併到 max_chars 內，形成適合一個鏡頭的段落。"""
    text = re.sub(r"\s+", " ", text.strip())
    pieces = re.split(r"(?<=[。！？!?；;\n])", text)
    segs: list[str] = []
    buf = ""
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if len(buf) + len(p) <= max_chars:
            buf += p
        else:
            if buf:
                segs.append(buf)
            buf = p
    if buf:
        segs.append(buf)
    # 合併過短的尾段
    merged: list[str] = []
    for s in segs:
        if merged and len(s) < min_chars:
            merged[-1] += s
        else:
            merged.append(s)
    return merged


def extract_dialogue(text: str) -> str:
    """回傳段落中的對白（合併），無則空字串。"""
    found = _DIALOGUE_RE.findall(text)
    return " ".join(found).strip()


def extract_names(text: str, top: int = 4) -> list[str]:
    """離線啟發式角色名偵測：說話動詞前的中文詞 + 英文大寫詞。"""
    names: dict[str, int] = {}

    # 中文：說話動詞前 2-3 字
    for m in re.finditer(rf"([\u4e00-\u9fa5]{{2,3}})[{_SPEECH_VERBS}]", text):
        w = m.group(1)
        if not (set(w) & _STOP):
            names[w] = names.get(w, 0) + 3

    # 英文：連續大寫開頭詞
    for m in re.finditer(r"\b([A-Z][a-z]{2,})\b", text):
        names[m.group(1)] = names.get(m.group(1), 0) + 2

    ranked = sorted(names.items(), key=lambda kv: kv[1], reverse=True)
    out = [w for w, _ in ranked[:top]]
    return out or ["主角"]
