"""階段 4-7：SD 首幀 / ComfyUI 影片 / 字幕 / 合成（皆為章節層級）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..clients.sd_client import SDClient
from ..clients.comfyui_client import ComfyClient
from ..config import settings
from .project import Project, Chapter
from .stages_text import QUALITY, NEGATIVE, _dedupe_prompt


def _compose_ff_positive(shot: dict, char_by_name: dict, loc_by_name: dict) -> str:
    """組首幀正向提示：品質詞 ＋ 選定角色卡外貌 ＋ 選定場地卡背景 ＋ 鏡頭原始 positive，去重。

    角色卡（`shot["characters"]`）與場地卡（`shot["location"]`）皆為選填：
    未選或在共用池查無者略過，等同沿用鏡頭原本的 positive。品質詞與角色外貌放前面，
    確保不被 CLIP 77 token 截掉；重複詞由 `_dedupe_prompt` 收斂。
    """
    parts = [QUALITY]
    for n in shot.get("characters") or []:        # 選定角色卡 → 外貌一致來源
        c = char_by_name.get(n)
        if c and c.get("sd_prompt"):
            parts.append(c["sd_prompt"])
    loc = loc_by_name.get((shot.get("location") or "").strip())  # 選定場地卡 → 背景
    if loc and loc.get("sd_prompt"):
        parts.append(loc["sd_prompt"])
    base = (shot.get("first_frame_prompt") or {}).get("positive") or ""
    if base:
        parts.append(base)
    return _dedupe_prompt(", ".join(p for p in parts if p))


# ---------- 階段 4：SD 生成首幀 ----------
def run_sd_first_frame(project: Project, ch: Chapter, options: dict) -> dict:
    shots = ch.read_json("storyboard.json")
    sd = SDClient()
    chars = project.read_characters()
    char_by_name = {c["name"]: c for c in chars}
    loc_by_name = {l["name"]: l for l in project.read_locations()}
    # 角色 → 立繪種子，讓含該角色的首幀沿用其種子，降低成像偏移
    seed_by_char = {c["name"]: c.get("seed") for c in chars}
    done = 0
    for shot in shots:
        out = ch.dir / "frames" / f'{shot["id"]}.png'
        if out.exists():  # 段級斷點續傳
            shot["frame"] = f'frames/{shot["id"]}.png'
            continue
        ff = shot.get("first_frame_prompt") or {}
        present = shot.get("characters") or []
        # 首幀提示：把選定的角色卡、場地卡外貌合進原始 positive
        positive = _compose_ff_positive(shot, char_by_name, loc_by_name)
        # 有角色用角色 seed（跨鏡頭一致）；無角色則由專案 seed 推導（仍可重現、各鏡頭不同）
        seed = next((seed_by_char[n] for n in present if seed_by_char.get(n)), None)
        if seed is None:
            seed = project.derive_seed(shot["id"])
        sd.txt2img(positive, ff.get("negative") or NEGATIVE, out, seed=seed)
        shot["frame"] = f'frames/{shot["id"]}.png'
        done += 1
        ch.log(f'SD 首幀 {shot["id"]} 完成')
    ch.write_json("storyboard.json", shots)
    return {"generated": done, "total": len(shots), "mock": sd.mock}


# ---------- 階段 5：ComfyUI 生成影片 ----------
def run_comfy_video(project: Project, ch: Chapter, options: dict) -> dict:
    shots = ch.read_json("storyboard.json")
    comfy = ComfyClient()
    done = 0
    prev_clip: Path | None = None   # 上一鏡的片段，供「連續不換鏡」承接尾幀
    for shot in shots:
        frame = ch.dir / "frames" / f'{shot["id"]}.png'
        out = ch.dir / "clips" / f'{shot["id"]}.mp4'
        # 連續不換鏡：本鏡標記 continue_prev，且上一鏡已有片段 → 用上一鏡尾幀當本鏡首幀
        # （只在需要重生本鏡片段時覆寫，避免改動已完成的鏡頭）
        if (shot.get("continue_prev") and prev_clip and prev_clip.exists()
                and not out.exists()):
            if _extract_last_frame(prev_clip, frame):
                shot["frame"] = f'frames/{shot["id"]}.png'
                ch.log(f'{shot["id"]} 承接上一鏡尾幀為首幀（連續不換鏡）')
        if not frame.exists():
            raise FileNotFoundError(f'缺少首幀 {shot["id"]}.png，請先執行 SD 階段')
        if out.exists():
            shot["clip"] = f'clips/{shot["id"]}.mp4'
            prev_clip = out
            continue
        comfy.image_to_video(frame, _video_prompt(shot),
                             float(shot.get("duration", 4.0)), out)
        shot["clip"] = f'clips/{shot["id"]}.mp4'
        prev_clip = out
        done += 1
        ch.log(f'ComfyUI 影片 {shot["id"]} 完成')
    ch.write_json("storyboard.json", shots)
    return {"generated": done, "total": len(shots), "mock": comfy.mock}


def _extract_last_frame(clip: Path, out_png: Path) -> bool:
    """抽出影片最後一幀存成 png（給「連續不換鏡」接首幀用）；成功回 True。

    `-sseof -1` 先 seek 到結尾前約 1 秒，`-update 1` 把後續每幀都寫到同一檔、覆寫到最後一張，
    取得片段尾幀。極短片段 seek 會落在 0，仍能取到尾幀。
    """
    out_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-sseof", "-1", "-i", str(clip),
             "-update", "1", "-q:v", "2", str(out_png)],
            check=True)
        return out_png.exists()
    except Exception:  # noqa: BLE001 — 抽幀失敗就退回原本首幀，不中斷流程
        return False


def _video_prompt(shot: dict) -> str:
    """組出給 LTX 圖生影/語音模型的提示：以『角色動作』為主，並帶入對白與旁白。

    LTX 只需描述角色動作（如「白衣女子走向窗邊」），不必照抄原文場景描述；
    對白盡量完整、旁白可精簡。場景/鏡頭/運鏡/氣氛預設空白，使用者有填才帶入。
    旁白/對白用中文、其餘描述可中可英。
    """
    cp = shot.get("comfy_prompt", {})
    # action 為主；其餘選填欄位（場景/人物/鏡頭/運鏡/氣氛）有填才補上
    parts = [
        (cp.get("action") or "").strip(),
        (cp.get("scene") or "").strip(),
        (cp.get("characters") or "").strip(),
        ", ".join(t for t in [cp.get("camera"), cp.get("motion")] if t),
        f'mood: {cp.get("mood")}' if cp.get("mood") else "",
    ]
    tone = shot.get("voice_tone") or "沉穩"
    dialogue = (shot.get("dialogue") or "").strip()
    narration = (shot.get("narration") or "").strip()
    if dialogue:
        parts.append(f'對白（語氣：{tone}）：{dialogue}')
    if narration:
        parts.append(f'旁白：{narration}')
    return ". ".join(p for p in parts if p)


# ---------- 階段 6：字幕加載 ----------
def run_subtitles(project: Project, ch: Chapter, options: dict) -> dict:
    shots = ch.read_json("storyboard.json")
    lines = []
    idx = 1
    t = 0.0
    for shot in shots:
        dur = float(shot.get("duration", 4.0))
        text = shot.get("dialogue") or shot.get("narration") or ""
        text = text.strip()
        if text:
            lines.append(f"{idx}\n{_ts(t)} --> {_ts(t + dur)}\n{text}\n")
            idx += 1
        t += dur
    srt = "\n".join(lines)
    ch.write_text("subtitles/full.srt", srt)
    ch.log(f"字幕完成，共 {idx - 1} 條")
    return {"entries": idx - 1}


# ---------- 階段 7：影片合成 ----------
def run_compose(project: Project, ch: Chapter, options: dict) -> dict:
    shots = ch.read_json("storyboard.json")
    clips = []
    for shot in shots:
        c = ch.dir / "clips" / f'{shot["id"]}.mp4'
        if not c.exists():
            raise FileNotFoundError(f'缺少影片片段 {shot["id"]}.mp4，請先執行 ComfyUI 階段')
        clips.append(c)
    if not clips:
        raise ValueError("沒有可合成的片段")

    # 1. 統一規格後串接 (concat filter 比 demuxer 對不同編碼更穩)
    merged = ch.dir / "output" / "_merged.mp4"
    _concat(clips, merged)

    # 2. 燒錄字幕
    srt = ch.dir / "subtitles" / "full.srt"
    final = ch.dir / "output" / "final.mp4"
    if srt.exists() and srt.read_text("utf-8").strip():
        style = "FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,MarginV=60"
        # -c:a copy 把合併後的音軌（若有，例如 LTX 生成的語音）一起帶進成片
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(merged),
               "-vf", f"subtitles='{_ff_path(srt)}':force_style='{style}'",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy", str(final)]
        subprocess.run(cmd, check=True)
        merged.unlink(missing_ok=True)
    else:
        merged.replace(final)
    ch.log(f"影片合成完成：{final.name}")
    return {"output": "output/final.mp4", "clips": len(clips)}


# ---------- helpers ----------
def _has_audio(path: Path) -> bool:
    """用 ffprobe 偵測片段是否含音軌（LTX 生成的影片帶語音；mock 推鏡則無）。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001 — 沒有 ffprobe 就當無音軌
        return False


def _concat(clips: list[Path], out: Path) -> None:
    w, h, fps = settings.video.width, settings.video.height, settings.video.fps
    # 全部片段都有音軌才併音軌（避免部分有部分無導致 concat 失敗）
    with_audio = all(_has_audio(c) for c in clips)
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    n = len(clips)
    vfilt = "".join(
        f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}];"
        for i in range(n)
    )
    if with_audio:
        afilt = "".join(f"[{i}:a]aresample=async=1[a{i}];" for i in range(n))
        pairs = "".join(f"[v{i}][a{i}]" for i in range(n))
        filt = vfilt + afilt + pairs + f"concat=n={n}:v=1:a=1[outv][outa]"
        maps = ["-map", "[outv]", "-map", "[outa]", "-c:a", "aac"]
    else:
        filt = vfilt + "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[outv]"
        maps = ["-map", "[outv]"]
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
           "-filter_complex", filt, *maps,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    subprocess.run(cmd, check=True)


def _ff_path(path: Path) -> str:
    """把路徑轉成 ffmpeg subtitles 濾鏡可安全解析的字串。

    濾鏡會把 ``\\`` 與 ``:`` 當成特殊字元，Windows 路徑（C:\\…）因此會被誤切。
    改用正斜線並跳脫磁碟代號的冒號：``C:/Users/…`` → ``C\\:/Users/…``。
    """
    s = str(path).replace("\\", "/")
    return s.replace(":", "\\:")


def _ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
