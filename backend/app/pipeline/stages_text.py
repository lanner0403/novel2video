"""階段 1-3：讀取小說 / 角色卡 / 分鏡。

角色卡是「專案層級、跨章共用」：
  - 本章偵測到、但池中已存在的角色 → 預設沿用（共用），不重生。
  - 本章新角色，或在 options["regenerate"] 名單中的角色 → 產生角色卡並呼叫 SD 生成立繪。
立繪用固定種子，讓同角色在不同章節/鏡頭的成像盡量一致，降低偏移。
"""

from __future__ import annotations

import random

from ..clients.llm_client import LLMClient
from ..clients.sd_client import SDClient
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


# ---------- 階段 2：角色卡（專案層級共用池 + 立繪）----------
def run_character_cards(project: Project, ch: Chapter, options: dict) -> dict:
    segments = ch.read_json("segments.json")
    full = "\n".join(s["text"] for s in segments)
    llm = LLMClient()
    regenerate = set(options.get("regenerate") or [])

    def mock() -> list[dict]:
        cards = []
        for name in extract_names(full, top=4):
            cards.append({
                "name": name,
                "aliases": [],
                "appearance": f"{name}，外貌特徵待補（離線推測）",
                "personality": "性格描述待補（離線推測）",
                "sd_prompt": f"1person, {name}, detailed face, expressive eyes, {STYLE}",
            })
        return cards

    candidates = llm.generate_json(
        system="你是分鏡師，從小說擷取主要角色，輸出 JSON 物件 {\"characters\": [...]}，"
               "每個角色含 name, aliases(陣列), appearance(外貌), personality(性格), "
               "sd_prompt(用於 Stable Diffusion 的英文外貌提示詞)。",
        user=full[:6000],
        mock_builder=lambda: {"characters": mock()},
    )
    candidates = candidates.get("characters", candidates) if isinstance(candidates, dict) else candidates

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

    for name, card in to_process.items():
        exists = name in pool_by_name
        seed = random.randint(1, 2_000_000_000)   # (重)生成一律給新種子
        card["seed"] = seed
        card["portrait"] = project.portrait_rel(name)
        portrait_prompt = f'{card.get("sd_prompt", "")}, {PORTRAIT_STYLE}'
        try:
            sd.txt2img(portrait_prompt, NEGATIVE, project.portrait_path(name), seed=seed)
        except Exception as e:  # noqa: BLE001 — 立繪失敗不應中斷角色卡
            ch.log(f"⚠ 角色 {name} 立繪生成失敗：{e}")
        pool_by_name[name] = card
        regen += int(exists)
        added += int(not exists)
        ch.log(f"角色卡 {name} {'重生' if exists else '新增'}（立繪 seed={seed}）")

    project.write_characters(list(pool_by_name.values()))
    ch.log(f"角色卡完成：新增 {added}、重生 {regen}、沿用 {reused}，共用池共 {len(pool_by_name)} 名")
    return {"added": added, "regenerated": regen, "reused": reused,
            "total": len(pool_by_name), "mock": sd.mock}


# ---------- 階段 3：分鏡 ----------
def run_storyboard(project: Project, ch: Chapter, options: dict) -> dict:
    segments = ch.read_json("segments.json")
    characters = project.read_characters()      # 用專案層級共用角色
    char_lookup = {c["name"]: c for c in characters}
    llm = LLMClient()

    def mock() -> list[dict]:
        shots = []
        for seg in segments:
            i = seg["index"]
            txt = seg["text"]
            present = [c for n, c in char_lookup.items() if n in txt]
            char_tags = ", ".join(c["sd_prompt"] for c in present[:2])
            dialogue = extract_dialogue(txt)
            narration = txt if not dialogue else txt.replace(dialogue, "").strip()
            positive = ", ".join(t for t in [STYLE, char_tags, _scene_keywords(txt)] if t)
            shots.append({
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
            })
        return shots

    sb = llm.generate_json(
        system="你是專業分鏡師。為每個段落產生一個鏡頭，輸出 JSON {\"shots\": [...]}。"
               "每個鏡頭含 id, segment_index, summary, characters(陣列), "
               "first_frame_prompt{positive,negative}(英文，用於 SD 首幀), "
               "comfy_prompt{motion,camera,scene_transition}(英文，給 ComfyUI 圖生影), "
               "narration(旁白,中文), dialogue(對白,中文), duration(秒)。",
        user="角色：\n" + str(characters)[:2000] + "\n\n段落：\n"
             + "\n".join(f'{s["index"]}. {s["text"]}' for s in segments)[:6000],
        mock_builder=lambda: {"shots": mock()},
    )
    shots = sb.get("shots", sb) if isinstance(sb, dict) else sb
    ch.write_json("storyboard.json", shots)
    ch.log(f"分鏡完成，共 {len(shots)} 個鏡頭")
    return {"shots": len(shots)}


def _scene_keywords(text: str) -> str:
    table = {
        "夜": "night, moonlight", "晚": "night", "雨": "rain, wet ground",
        "森林": "forest", "城": "city street", "海": "ocean, waves",
        "山": "mountain", "房": "indoor room", "戰": "battle, dynamic",
        "笑": "smiling", "哭": "crying, tears", "風": "wind",
    }
    hits = [v for k, v in table.items() if k in text]
    return ", ".join(hits)
