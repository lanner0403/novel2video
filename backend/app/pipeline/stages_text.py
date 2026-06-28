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

STYLE = "masterpiece, best quality, highly detailed, cinematic lighting, anime style"
PORTRAIT_STYLE = ("full body character reference sheet, standing pose, plain background, "
                  "front view, " + STYLE)
CAMERAS = ["medium shot", "close-up", "wide establishing shot", "over-the-shoulder shot",
           "low angle shot", "bird's-eye view"]
MOTIONS = ["slow push-in", "gentle pan left", "subtle handheld sway",
           "slow dolly out", "static with ambient motion"]


# ---------- 階段 1：讀取小說 ----------
def run_read_novel(project: Project, ch: Chapter, options: dict) -> dict:
    text = ch.read_text("novel.txt")
    if not text.strip():
        raise ValueError("本章小說內容為空，請先貼上或上傳文字。")
    segs = split_segments(text)
    data = [{"index": i, "text": s} for i, s in enumerate(segs)]
    ch.write_json("segments.json", data)
    ch.log(f"讀取小說完成，切出 {len(segs)} 個段落")
    return {"segments": len(segs)}


_CHAR_SYSTEM = ("你是分鏡師，從小說片段擷取主要角色，輸出 JSON 物件 {\"characters\": [...]}，"
                "每個角色含 name, aliases(陣列), appearance(外貌), personality(性格), "
                "sd_prompt(用於 Stable Diffusion 的英文外貌提示詞)。只列有名有姓或明確指稱的角色。")


def _mock_cards(text: str, top: int = 4) -> list[dict]:
    """離線：依文字啟發式推測角色名，產生佔位角色卡。"""
    return [{
        "name": name,
        "aliases": [],
        "appearance": f"{name}，外貌特徵待補（離線推測）",
        "personality": "性格描述待補（離線推測）",
        "sd_prompt": f"1person, {name}, detailed face, expressive eyes, {STYLE}",
    } for name in extract_names(text, top=top)]


def _normalize_card(raw: dict) -> dict | None:
    """補齊角色卡欄位；無名字則丟棄。aliases 強制成 list。"""
    if not isinstance(raw, dict):
        return None
    name = (raw.get("name") or "").strip()
    if not name:
        return None
    aliases = raw.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a.strip() for a in re.split(r"[、,/]", aliases) if a.strip()]
    return {
        "name": name,
        "aliases": list(aliases),
        "appearance": (raw.get("appearance") or f"{name}，外貌特徵待補").strip(),
        "personality": (raw.get("personality") or "性格描述待補").strip(),
        "sd_prompt": (raw.get("sd_prompt")
                      or f"1person, {name}, detailed face, expressive eyes, {STYLE}").strip(),
    }


def _extract_characters(llm: LLMClient, segments: list[dict]) -> list[dict]:
    """分批掃完整章內文擷取角色並聯集去重（避免長章節因截斷漏掉後段角色）。"""
    n = settings.llm.character_batch
    batches = ([segments[i:i + n] for i in range(0, len(segments), n)]
               if n and n > 0 else [segments])
    merged: dict[str, dict] = {}
    for segs in batches:
        chunk = "\n".join(s["text"] for s in segs)
        sb = llm.generate_json(
            system=_CHAR_SYSTEM,
            user=chunk[:6000],
            mock_builder=lambda c=chunk: {"characters": _mock_cards(c)},
        )
        raw = sb.get("characters", sb) if isinstance(sb, dict) else sb
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
        seed = random.randint(1, 2_000_000_000)   # (重)生成一律給新種子
        card["seed"] = seed
        card["portrait"] = project.portrait_rel(name)
        portrait_prompt = f'{card.get("sd_prompt", "")}, {PORTRAIT_STYLE}'
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


_SB_SYSTEM = ("你是專業分鏡師。為「給定的每個段落」各產生一個鏡頭，輸出 JSON {\"shots\": [...]}。"
              "每個鏡頭含 id, segment_index(對應段落編號), summary, characters(陣列), "
              "first_frame_prompt{positive,negative}(英文，用於 SD 首幀), "
              "comfy_prompt{motion,camera,scene_transition}(英文，給 ComfyUI 圖生影), "
              "narration(旁白,中文), dialogue(對白,中文), duration(秒)。"
              "shots 數量需與輸入段落數量一致，segment_index 用輸入給的編號。")


def _mock_shot(seg: dict, char_lookup: dict) -> dict:
    """離線/補空用：依單一段落啟發式產生一個完整鏡頭。"""
    i, txt = seg["index"], seg["text"]
    present = [c for n, c in char_lookup.items() if n in txt]
    char_tags = ", ".join(c["sd_prompt"] for c in present[:2])
    dialogue = extract_dialogue(txt)
    narration = txt if not dialogue else txt.replace(dialogue, "").strip()
    positive = ", ".join(t for t in [STYLE, char_tags, _scene_keywords(txt)] if t)
    return {
        "id": f"shot_{i:04d}",
        "segment_index": i,
        "summary": txt[:40],
        "characters": [c["name"] for c in present],
        "first_frame_prompt": {"positive": positive, "negative": NEGATIVE},
        "comfy_prompt": {
            "motion": MOTIONS[i % len(MOTIONS)],
            "camera": CAMERAS[i % len(CAMERAS)],
            "scene_transition": "fade" if i % 4 == 0 else "cut",
        },
        "narration": narration or txt,
        "dialogue": dialogue,
        "duration": 4.0,
    }


def _normalize_shot(raw: dict | None, seg: dict, char_lookup: dict) -> dict:
    """把 LLM 回的鏡頭補成「一段一個、欄位齊全」；缺漏處用啟發式預設補上。"""
    base = _mock_shot(seg, char_lookup)
    if not isinstance(raw, dict):
        return base
    i, txt = seg["index"], seg["text"]
    ff = raw.get("first_frame_prompt") or {}
    cp = raw.get("comfy_prompt") or {}
    return {
        "id": f"shot_{i:04d}",                 # id / segment_index 一律以實際段落為準
        "segment_index": i,
        "summary": raw.get("summary") or base["summary"],
        "characters": raw.get("characters") or base["characters"],
        "first_frame_prompt": {
            "positive": ff.get("positive") or base["first_frame_prompt"]["positive"],
            "negative": ff.get("negative") or NEGATIVE,
        },
        "comfy_prompt": {
            "motion": cp.get("motion") or base["comfy_prompt"]["motion"],
            "camera": cp.get("camera") or base["comfy_prompt"]["camera"],
            "scene_transition": cp.get("scene_transition") or base["comfy_prompt"]["scene_transition"],
        },
        "narration": raw.get("narration") or base["narration"],
        "dialogue": raw.get("dialogue") if raw.get("dialogue") is not None else base["dialogue"],
        "duration": float(raw.get("duration") or 4.0),
    }


def _storyboard_batch(llm: LLMClient, segs: list[dict], characters: list[dict],
                      char_lookup: dict) -> list[dict]:
    """對一批段落產生分鏡，回傳正規化後、與段落一一對應的鏡頭。"""
    sb = llm.generate_json(
        system=_SB_SYSTEM,
        user="角色：\n" + str(characters)[:2000] + "\n\n段落：\n"
             + "\n".join(f'{s["index"]}. {s["text"]}' for s in segs)[:6000],
        mock_builder=lambda: {"shots": [_mock_shot(s, char_lookup) for s in segs]},
    )
    raw = sb.get("shots", sb) if isinstance(sb, dict) else sb
    raw = raw if isinstance(raw, list) else []
    by_idx = {s.get("segment_index"): s for s in raw if isinstance(s, dict)}
    out = []
    for pos, seg in enumerate(segs):
        # 先用 segment_index 對齊，否則用位置對齊，再不行就純啟發式補
        match = by_idx.get(seg["index"]) or (raw[pos] if len(raw) == len(segs) else None)
        out.append(_normalize_shot(match, seg, char_lookup))
    return out


# ---------- 階段 3：分鏡 ----------
def run_storyboard(project: Project, ch: Chapter, options: dict) -> dict:
    segments = ch.read_json("segments.json")
    characters = project.read_characters()      # 用專案層級共用角色
    char_lookup = {c["name"]: c for c in characters}
    llm = LLMClient()

    # 長章節分批送 LLM：每批 N 段，降低單次生成過久/逾時與 JSON 解析失敗的風險。
    n = settings.llm.storyboard_batch
    batches = ([segments[i:i + n] for i in range(0, len(segments), n)]
               if n and n > 0 else [segments])

    shots: list[dict] = []
    for bi, segs in enumerate(batches):
        shots.extend(_storyboard_batch(llm, segs, characters, char_lookup))
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
