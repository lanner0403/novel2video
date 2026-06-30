"""階段 1-3：讀取小說 / 角色卡 / 分鏡。

角色卡是「專案層級、跨章共用」：
  - 本章偵測到、但池中已存在的角色 → 預設沿用（共用），不重生。
  - 本章新角色，或在 options["regenerate"] 名單中的角色 → 產生角色卡並呼叫 SD 生成立繪。
立繪用固定種子，讓同角色在不同章節/鏡頭的成像盡量一致，降低偏移。
"""

from __future__ import annotations

import random
import re

from ..clients.llm_client import LLMClient
from ..clients.sd_client import SDClient
from ..config import settings
from ..utils.text import split_segments, extract_dialogue, extract_names
from .project import Project, Chapter

# 通用畫質負向詞
NEGATIVE = ("lowres, bad anatomy, bad hands, text, error, missing fingers, "
            "extra digit, fewer digits, cropped, worst quality, low quality, "
            "jpeg artifacts, signature, watermark, blurry")

# 全專案統一的正向品質詞（立繪與首幀共用，放最前確保不被 CLIP 77 token 截掉）
QUALITY = ("masterpiece, best quality, newest, absurdres, highres, detailed eyes, "
           "beautiful, perfect eyes, glossy material render, semi-realistic")
STYLE = QUALITY   # 相容舊引用
# 立繪框景詞（簡短，不再疊一份品質詞，組裝時去重）
PORTRAIT_STYLE = "full body, standing pose, no background, front view"
# 與 semi-realistic 衝突、組 prompt 時要濾掉的風格詞（不分大小寫，整段比對）
_CONFLICT_STYLES = {"anime style", "anime", "2d", "cartoon", "manga", "comic",
                    "flat color", "flat colors", "cel shading", "chibi"}


def _portrait_prompt(card: dict) -> str:
    """組角色立繪 prompt：品質詞在前（必留）、角色外貌次之、框景最後，去重＋濾衝突風格。"""
    return _dedupe_prompt(f'{QUALITY}, {card.get("sd_prompt", "")}, {PORTRAIT_STYLE}')


def regenerate_portrait(project: Project, name: str, seed: int | None = None) -> int:
    """重生單一角色的立繪（用其現有 sd_prompt），更新 seed 並寫回共用池。回傳採用的 seed。"""
    cards = project.read_characters()
    card = next((c for c in cards if c["name"] == name), None)
    if card is None:
        raise FileNotFoundError(f"共用池找不到角色：{name}")
    # 明確指定 > 角色現有 seed > 由專案 seed 推導（預設可重現）
    seed = int(seed) if seed else (card.get("seed") or project.derive_seed(name))
    SDClient().txt2img(_portrait_prompt(card), NEGATIVE, project.portrait_path(name), seed=seed)
    card["seed"] = seed
    card["portrait"] = project.portrait_rel(name)
    project.write_characters(cards)
    return seed


def _dedupe_prompt(text: str, max_terms: int = 24) -> str:
    """以逗號為單位去重（不分大小寫、保留順序）、濾掉衝突風格詞並截斷，避免 CLIP 77 token 超限。"""
    seen: set[str] = set()
    out: list[str] = []
    for term in text.split(","):
        t = term.strip()
        key = t.lower()
        if not t or key in seen or key in _CONFLICT_STYLES:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= max_terms:
            break
    return ", ".join(out)


# ---------- 階段 1：讀取小說 ----------
def _to_traditional(text: str, ch: Chapter | None = None) -> str:
    """把簡體中文轉繁體（用純 Python 的 zhconv）。未安裝套件時原樣回傳並提示。"""
    try:
        import zhconv
    except ImportError:
        if ch is not None:
            ch.log("⚠ 未安裝 zhconv，略過簡轉繁（pip install zhconv）")
        return text
    return zhconv.convert(text, "zh-hant")


def run_read_novel(project: Project, ch: Chapter, options: dict) -> dict:
    text = ch.read_text("novel.txt")
    if not text.strip():
        raise ValueError("本章小說內容為空，請先貼上或上傳文字。")
    converted = False
    if settings.text.to_traditional:
        new_text = _to_traditional(text, ch)
        if new_text != text:   # 真的有簡體被轉換才回寫，讓編輯器與下游都用繁體
            text = new_text
            ch.write_text("novel.txt", text)
            converted = True
    segs = split_segments(text)
    data = [{"index": i, "text": s} for i, s in enumerate(segs)]
    ch.write_json("segments.json", data)
    ch.log(f"讀取小說完成，切出 {len(segs)} 個段落" + ("，已將簡體轉繁體" if converted else ""))
    return {"segments": len(segs), "converted": converted}


_CHAR_SYSTEM = ("你是分鏡師，從小說片段擷取主要角色，輸出 JSON 物件 {\"characters\": [...]}，"
                "每個角色含 name, aliases(陣列), "
                "ref_term(角色替代詞，中文，簡短的外型指稱，給之後動畫提示詞用，"
                "如「白衣女子」「紅髮男子」「黑袍老人」，只要外型特徵不含名字), "
                "appearance(外貌，**必須包含**：年齡、衣著、髮色、髮型，可再補其他特徵), "
                "personality(性格), "
                "sd_prompt(英文外貌提示詞，**必須含** age、clothing、hair color、hairstyle 對應描述)。"
                "只列有名有姓或明確指稱的角色。")


def _mock_cards(text: str, top: int = 4) -> list[dict]:
    """離線：依文字啟發式推測角色名，產生佔位角色卡（外貌含年齡/衣著/髮色/髮型欄位）。"""
    return [{
        "name": name,
        "aliases": [],
        "ref_term": name,   # 離線無外貌可推，先用名字當替代詞，使用者可再編輯
        "appearance": f"{name}：年齡待補、衣著待補、髮色待補、髮型待補（離線推測）",
        "personality": "性格描述待補（離線推測）",
        "sd_prompt": (f"1person, {name}, young adult, casual outfit, "
                      f"dark hair, medium-length hair, detailed face, expressive eyes"),
    } for name in extract_names(text, top=top)]


def _txt(v, default: str = "") -> str:
    """把 LLM 可能回成 dict/list/None 的欄位安全轉成字串（避免對非字串呼叫 .strip）。"""
    if isinstance(v, str):
        return v.strip()
    if v is None or isinstance(v, (dict, list)):
        return default
    return str(v).strip()


def _normalize_card(raw: dict) -> dict | None:
    """補齊角色卡欄位；無名字則丟棄。aliases 強制成 list、各欄位容忍 LLM 回傳非字串。"""
    if not isinstance(raw, dict):
        return None
    name = _txt(raw.get("name"))
    if not name:
        return None
    aliases = raw.get("aliases") or []
    if isinstance(aliases, str):
        aliases = re.split(r"[、,/]", aliases)
    aliases = [a for a in (_txt(x) for x in aliases) if a]   # 元素可能非字串
    return {
        "name": name,
        "aliases": list(dict.fromkeys(aliases)),
        "ref_term": _txt(raw.get("ref_term")) or name,       # 替代詞：缺則退回名字
        "appearance": (_txt(raw.get("appearance"))
                       or f"{name}：年齡待補、衣著待補、髮色待補、髮型待補"),
        "personality": _txt(raw.get("personality")) or "性格描述待補",
        "sd_prompt": (_txt(raw.get("sd_prompt"))
                      or f"1person, {name}, young adult, casual outfit, dark hair, "
                         f"medium-length hair, detailed face"),
    }


def _extract_characters(llm: LLMClient, segments: list[dict]) -> list[dict]:
    """分批掃完整章內文擷取角色並聯集去重（避免長章節因截斷漏掉後段角色）。"""
    n = settings.llm.character_batch
    batches = ([segments[i:i + n] for i in range(0, len(segments), n)]
               if n and n > 0 else [segments])
    merged: dict[str, dict] = {}
    for segs in batches:
        chunk = "\n".join(s["text"] for s in segs)
        try:
            sb = llm.generate_json(
                system=_CHAR_SYSTEM,
                user=chunk[:6000],
                mock_builder=lambda c=chunk: {"characters": _mock_cards(c)},
            )
            raw = sb.get("characters", sb) if isinstance(sb, dict) else sb
        except Exception:  # noqa: BLE001 — 壞 JSON / 逾時：該批退回啟發式
            raw = _mock_cards(chunk)
        for item in (raw if isinstance(raw, list) else []):
            card = _normalize_card(item)
            if not card:
                continue
            if card["name"] in merged:   # 跨批重複：聯集別名，其餘保留先出現的
                ex = merged[card["name"]]
                ex["aliases"] = list(dict.fromkeys(ex["aliases"] + card["aliases"]))
            else:
                merged[card["name"]] = card
    return list(merged.values())


# ---------- 場地卡擷取 ----------
_LOC_SYSTEM = ("你是美術設定師，從小說片段擷取出現的『場景／場地』，輸出 JSON 物件 {\"locations\": [...]}。"
               "每個場地含 name(場地名稱), description(場地描述,中文,重點描述環境), "
               "indoor_outdoor(室內或室外), props(場景物品,中文陣列), "
               "time_of_day(時間,如 白天/黃昏/夜晚/清晨), "
               "sd_prompt(英文場景背景提示詞,給 Stable Diffusion)。重複場景請合併，只列明確場景。")


def _mock_locations(text: str) -> list[dict]:
    """離線：用場景關鍵字粗略推一個場地卡。"""
    kw = _scene_keywords(text)
    if not kw:
        return []
    outdoor = any(k in text for k in "街城海山森林野外路橋")
    night = ("夜" in text or "晚" in text)
    return [{
        "name": "場景（離線推測）",
        "description": f"含 {kw}",
        "indoor_outdoor": "室外" if outdoor else "室內",
        "props": [],
        "time_of_day": "夜晚" if night else "白天",
        "sd_prompt": kw,
    }]


def _normalize_location(raw: dict) -> dict | None:
    """補齊場地卡欄位；無名字則丟棄。props 強制成 list。"""
    if not isinstance(raw, dict):
        return None
    name = _txt(raw.get("name"))
    if not name:
        return None
    props = raw.get("props") or []
    if isinstance(props, str):
        props = re.split(r"[、,/]", props)
    props = [p for p in (_txt(x) for x in props) if p]   # 元素可能非字串
    return {
        "name": name,
        "description": _txt(raw.get("description")),
        "indoor_outdoor": _txt(raw.get("indoor_outdoor")),
        "props": list(dict.fromkeys(props)),
        "time_of_day": _txt(raw.get("time_of_day")),
        "sd_prompt": _txt(raw.get("sd_prompt")),
    }


def _extract_locations(llm: LLMClient, segments: list[dict]) -> list[dict]:
    """分批掃完整章內文擷取場地並聯集去重。"""
    n = settings.llm.character_batch
    batches = ([segments[i:i + n] for i in range(0, len(segments), n)]
               if n and n > 0 else [segments])
    merged: dict[str, dict] = {}
    for segs in batches:
        chunk = "\n".join(s["text"] for s in segs)
        try:
            sb = llm.generate_json(
                system=_LOC_SYSTEM,
                user=chunk[:6000],
                mock_builder=lambda c=chunk: {"locations": _mock_locations(c)},
            )
            raw = sb.get("locations", sb) if isinstance(sb, dict) else sb
        except Exception:  # noqa: BLE001 — 壞 JSON / 逾時：該批退回啟發式
            raw = _mock_locations(chunk)
        for item in (raw if isinstance(raw, list) else []):
            loc = _normalize_location(item)
            if not loc:
                continue
            if loc["name"] in merged:   # 跨批重複：聯集物品，其餘保留先出現的
                ex = merged[loc["name"]]
                ex["props"] = list(dict.fromkeys(ex["props"] + loc["props"]))
            else:
                merged[loc["name"]] = loc
    return list(merged.values())


# ---------- 階段：場地卡（專案層級共用池）----------
def run_location_cards(project: Project, ch: Chapter, options: dict) -> dict:
    segments = ch.read_json("segments.json")
    llm = LLMClient()
    regenerate = set(options.get("regenerate") or [])
    candidates = _extract_locations(llm, segments)

    by_name = {l["name"]: l for l in project.read_locations()}
    added = 0
    for loc in candidates:
        name = loc["name"]
        if name in by_name and name not in regenerate:
            # 沿用既有，聯集物品
            by_name[name]["props"] = list(dict.fromkeys(by_name[name].get("props", []) + loc["props"]))
        else:
            added += int(name not in by_name)
            by_name[name] = loc
    project.write_locations(list(by_name.values()))
    ch.log(f"場地卡完成：本章抽到 {len(candidates)} 個，共用池共 {len(by_name)} 個場地")
    return {"found": len(candidates), "added": added, "total": len(by_name)}


# ---------- 階段 2：角色卡（專案層級共用池 + 立繪）----------
def run_character_cards(project: Project, ch: Chapter, options: dict) -> dict:
    segments = ch.read_json("segments.json")
    llm = LLMClient()
    regenerate = set(options.get("regenerate") or [])

    candidates = _extract_characters(llm, segments)

    # 既有共用池
    pool = project.read_characters()
    pool_by_name = {c["name"]: c for c in pool}
    cand_by_name = {c["name"]: c for c in candidates if c.get("name")}
    sd = SDClient()
    added, regen, reused = 0, 0, 0

    # 決定要 (重新) 生成哪些角色：
    #   - 本章抽到的新角色 → 生成
    #   - 本章抽到、且在 regenerate 名單 → 重生
    #   - 本章抽到、已存在、未被要求重生 → 沿用（不動）
    #   - regenerate 名單中、本章雖未抽到但已在共用池 → 用既有卡重生（讓 UI 取消勾選一定生效）
    to_process: dict[str, dict] = {}
    for name, cand in cand_by_name.items():
        if name in pool_by_name and name not in regenerate:
            reused += 1
        else:
            to_process[name] = cand
    for name in regenerate:
        if name in pool_by_name and name not in to_process:
            to_process[name] = dict(pool_by_name[name])

    portraits_ok, portrait_err = 0, None
    for name, card in to_process.items():
        exists = name in pool_by_name
        seed = project.derive_seed(name)   # 由專案固定 seed 推導，整個專案可重現
        card["seed"] = seed
        card["portrait"] = project.portrait_rel(name)
        portrait_prompt = _portrait_prompt(card)
        out = project.portrait_path(name)
        try:
            sd.txt2img(portrait_prompt, NEGATIVE, out, seed=seed)
            if not out.exists():
                raise RuntimeError("txt2img 未產生檔案")
            portraits_ok += 1
            ch.log(f"角色卡 {name} {'重生' if exists else '新增'}，立繪完成（seed={seed}）")
        except Exception as e:  # noqa: BLE001 — 立繪失敗不中斷角色卡，但要把原因浮出來
            portrait_err = portrait_err or f"{name}: {type(e).__name__}: {e}"
            ch.log(f"⚠ 角色 {name} 立繪生成失敗：{type(e).__name__}: {e}")
        pool_by_name[name] = card
        regen += int(exists)
        added += int(not exists)

    project.write_characters(list(pool_by_name.values()))
    ch.log(f"角色卡完成：新增 {added}、重生 {regen}、沿用 {reused}，"
           f"立繪 {portraits_ok}/{len(to_process)}，共用池共 {len(pool_by_name)} 名")
    result = {"added": added, "regenerated": regen, "reused": reused,
              "portraits": portraits_ok, "total": len(pool_by_name), "mock": sd.mock}
    if portrait_err:
        result["portrait_error"] = portrait_err   # 顯示在階段結果，方便排查
    return result


_SB_SYSTEM = ("你是專業分鏡師，採用 LTX 圖生影（圖＋動作＋語音）。"
              "為「給定的每個段落」各產生一個鏡頭，輸出 JSON {\"shots\": [...]}。"
              "每個鏡頭含 id, segment_index(對應段落編號), summary, characters(陣列), "
              "location(場地名稱，從給定『場地清單』挑一個最符合的名字，沒有適合的就留空字串), "
              "continue_prev(布林；本鏡若與上一鏡為同場景、連續不換鏡的接續動作設 true，"
              "會用上一鏡尾幀接本鏡首幀，換場景/換鏡時設 false，預設 false), "
              "first_frame_prompt{positive,negative}(英文；positive 要明確寫出『人物外貌＋場景＋動作』，"
              "讓首幀圖就能看出是誰在什麼場景), "
              "comfy_prompt{action,scene,characters,camera,motion,mood,scene_transition}："
              "**action 最重要**，用中文描述『角色替代詞＋動作』，如「白衣女子走向窗邊」「紅髮男子轉身怒視」，"
              "請用角色清單裡的 ref_term 指稱角色；scene/camera/mood 一律留空字串(\"\")，由使用者自行補；"
              "scene_transition 給 cut 或 fade。 "
              "narration(旁白,中文,可適度精簡濃縮,不必照抄原文), "
              "dialogue(對白,中文,**盡量完整保留原文對話**), "
              "voice_tone(語音語氣,中文,如 溫柔/急促/憤怒/沉穩), "
              "duration(秒,需足夠唸完旁白/對白)。"
              "shots 數量需與輸入段落數量一致，segment_index 用輸入給的編號。")

# 語氣/氛圍啟發式：(關鍵字, 英文 mood, 中文語氣)
_MOOD_TABLE = [
    ("哭", "sad, melancholic", "哀傷"), ("淚", "sad, melancholic", "哀傷"),
    ("笑", "cheerful, warm", "愉快"), ("怒", "tense, intense", "憤怒"),
    ("吼", "tense, intense", "激動"), ("喊", "urgent, dramatic", "急促"),
    ("驚", "suspenseful", "驚訝"), ("戰", "epic, dynamic", "緊張"),
    ("愛", "romantic, soft", "深情"),
]


def _scene_mood(text: str) -> tuple[str, str]:
    """回傳 (英文 mood, 中文語氣)；找不到關鍵字給沉穩預設。"""
    for kw, mood, tone in _MOOD_TABLE:
        if kw in text:
            return mood, tone
    return "calm, cinematic", "沉穩"


def _estimate_duration(text: str, default: float = 4.0) -> float:
    """依語音長度估鏡頭秒數（中文約每秒 4 字），夾在 3~12 秒。"""
    n = len(re.sub(r"\s", "", text or ""))
    if n == 0:
        return default
    return float(max(3.0, min(12.0, round(n / 4.0, 1))))


def _mock_shot(seg: dict, char_lookup: dict) -> dict:
    """離線/補空用：依單一段落啟發式產生一個完整鏡頭（含場景/人物/語氣/語音長度）。"""
    i, txt = seg["index"], seg["text"]
    present = [c for n, c in char_lookup.items() if n in txt]
    char_tags = ", ".join(c["sd_prompt"] for c in present[:2])
    scene_kw = _scene_keywords(txt)
    _, tone_zh = _scene_mood(txt)
    dialogue = extract_dialogue(txt)
    narration = txt if not dialogue else txt.replace(dialogue, "").strip()
    # 首幀 prompt：人物外貌＋場景在前（重點），風格在後；去重避免 CLIP 截斷
    positive = _dedupe_prompt(", ".join(t for t in [char_tags, scene_kw, STYLE] if t))
    # LTX 動作：離線無法推動詞，先用在場角色的替代詞當佔位，使用者再補動作
    ref_terms = "、".join(c.get("ref_term") or c["name"] for c in present)
    return {
        "id": f"shot_{i:04d}",
        "segment_index": i,
        "summary": txt[:40],
        "characters": [c["name"] for c in present],
        "location": "",          # 場地卡名稱（選填，給首幀當背景）；離線無法可靠對應，留空
        "continue_prev": False,  # 連續不換鏡：用上一鏡尾幀當本鏡首幀（預設關）
        "first_frame_prompt": {"positive": positive, "negative": NEGATIVE},
        "comfy_prompt": {
            "action": ref_terms,    # 角色動作（替代詞＋動作），LTX 主要依據
            "scene": "",            # 場景：預設空白，使用者自行補
            "characters": "",
            "camera": "",           # 鏡頭：預設空白
            "motion": "",           # 運鏡：預設空白
            "mood": "",             # 氣氛：預設空白
            "scene_transition": "fade" if i % 4 == 0 else "cut",
        },
        "narration": narration or txt,
        "dialogue": dialogue,
        "voice_tone": tone_zh,
        "duration": _estimate_duration(dialogue or narration or txt),
    }


def _normalize_shot(raw: dict | None, seg: dict, char_lookup: dict) -> dict:
    """把 LLM 回的鏡頭補成「一段一個、欄位齊全」；缺漏處用啟發式預設補上。"""
    base = _mock_shot(seg, char_lookup)
    if not isinstance(raw, dict):
        return base
    i = seg["index"]
    ff = raw.get("first_frame_prompt") or {}
    cp = raw.get("comfy_prompt") or {}
    bcp = base["comfy_prompt"]
    narration = raw.get("narration") or base["narration"]
    dialogue = raw.get("dialogue") if raw.get("dialogue") is not None else base["dialogue"]
    return {
        "id": f"shot_{i:04d}",                 # id / segment_index 一律以實際段落為準
        "segment_index": i,
        "summary": raw.get("summary") or base["summary"],
        "characters": raw.get("characters") or base["characters"],
        "location": _txt(raw.get("location")),  # LLM 可從場地清單挑一個，否則留空
        "continue_prev": bool(raw.get("continue_prev")),  # 連續不換鏡（承接上一鏡尾幀）
        "first_frame_prompt": {
            "positive": _dedupe_prompt(ff.get("positive") or base["first_frame_prompt"]["positive"]),
            "negative": ff.get("negative") or NEGATIVE,
        },
        "comfy_prompt": {
            "action": cp.get("action") or bcp["action"],
            # scene/camera/motion/mood 預設空白（base 已是空字串），尊重 LLM 有給就用
            "scene": cp.get("scene") or bcp["scene"],
            "characters": cp.get("characters") or bcp["characters"],
            "camera": cp.get("camera") or bcp["camera"],
            "motion": cp.get("motion") or bcp["motion"],
            "mood": cp.get("mood") or bcp["mood"],
            "scene_transition": cp.get("scene_transition") or bcp["scene_transition"],
        },
        "narration": narration,
        "dialogue": dialogue,
        "voice_tone": raw.get("voice_tone") or base["voice_tone"],
        # 尊重 LLM 給的秒數，但至少要夠唸完語音
        "duration": max(float(raw.get("duration") or 0.0),
                        _estimate_duration(dialogue or narration)),
    }


def _storyboard_batch(llm: LLMClient, segs: list[dict], characters: list[dict],
                      char_lookup: dict, ch: Chapter | None = None,
                      locations: list[dict] | None = None) -> list[dict]:
    """對一批段落產生分鏡，回傳正規化後、與段落一一對應的鏡頭。

    該批 LLM 逾時或回傳壞 JSON 時，退回啟發式（_normalize_shot(None,...) 等同 _mock_shot），
    只讓這批降級、不讓整個分鏡階段崩潰。
    """
    loc_ctx = ("\n\n場地（請依此保持場景一致）：\n" + str(locations)[:1500]) if locations else ""
    try:
        sb = llm.generate_json(
            system=_SB_SYSTEM,
            user="角色：\n" + str(characters)[:2000] + loc_ctx + "\n\n段落：\n"
                 + "\n".join(f'{s["index"]}. {s["text"]}' for s in segs)[:6000],
            mock_builder=lambda: {"shots": [_mock_shot(s, char_lookup) for s in segs]},
        )
        raw = sb.get("shots", sb) if isinstance(sb, dict) else sb
        raw = raw if isinstance(raw, list) else []
    except Exception as e:  # noqa: BLE001 — 壞 JSON / 逾時都降級處理
        if ch is not None:
            ch.log(f"⚠ 分鏡批次改用啟發式（LLM 失敗：{type(e).__name__}: {e}）")
        raw = []
    by_idx = {s.get("segment_index"): s for s in raw if isinstance(s, dict)}
    out = []
    for pos, seg in enumerate(segs):
        # 先用 segment_index 對齊，否則用位置對齊，再不行就純啟發式補
        match = by_idx.get(seg["index"]) or (raw[pos] if len(raw) == len(segs) else None)
        out.append(_normalize_shot(match, seg, char_lookup))
    return out


def _merge_segments(segments: list[dict], target: int) -> list[dict]:
    """分鏡前先把相鄰短段落整合到約 target 字一鏡頭，並重新編號。target<=0 則不整合。"""
    if not target or target <= 0:
        return [{"index": i, "text": s["text"]} for i, s in enumerate(segments)]
    merged: list[str] = []
    buf = ""
    for s in segments:
        t = s["text"]
        if buf and len(buf) + len(t) > target:
            merged.append(buf)
            buf = t
        else:
            buf += t
    if buf:
        merged.append(buf)
    return [{"index": i, "text": t} for i, t in enumerate(merged)]


# ---------- 階段 3：分鏡 ----------
def run_storyboard(project: Project, ch: Chapter, options: dict) -> dict:
    raw_segments = ch.read_json("segments.json")
    # 先整合一部分段落，讓每個鏡頭更完整、語音更連貫
    segments = _merge_segments(raw_segments, settings.llm.storyboard_merge_chars)
    characters = project.read_characters()      # 用專案層級共用角色
    locations = project.read_locations()        # 用專案層級共用場地（讓場景一致）
    char_lookup = {c["name"]: c for c in characters}
    llm = LLMClient()

    # 長章節分批送 LLM：每批 N 段，降低單次生成過久/逾時與 JSON 解析失敗的風險。
    n = settings.llm.storyboard_batch
    batches = ([segments[i:i + n] for i in range(0, len(segments), n)]
               if n and n > 0 else [segments])

    shots: list[dict] = []
    for bi, segs in enumerate(batches):
        shots.extend(_storyboard_batch(llm, segs, characters, char_lookup, ch, locations))
        if len(batches) > 1:
            ch.log(f"分鏡批次 {bi + 1}/{len(batches)} 完成（累計 {len(shots)} 鏡頭）")

    ch.write_json("storyboard.json", shots)
    ch.log(f"分鏡完成，共 {len(shots)} 個鏡頭（{len(batches)} 批）")
    return {"shots": len(shots), "batches": len(batches)}


def _scene_keywords(text: str) -> str:
    table = {
        "夜": "night, moonlight", "晚": "night", "雨": "rain, wet ground",
        "森林": "forest", "城": "city street", "海": "ocean, waves",
        "山": "mountain", "房": "indoor room", "戰": "battle, dynamic",
        "笑": "smiling", "哭": "crying, tears", "風": "wind",
    }
    hits = [v for k, v in table.items() if k in text]
    return ", ".join(hits)
