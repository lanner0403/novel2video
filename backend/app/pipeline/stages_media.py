"""階段 4-7：SD 首幀 / ComfyUI 影片 / 字幕 / 合成（皆為章節層級）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..clients.sd_client import SDClient
from ..clients.comfyui_client import ComfyClient
from ..config import settings
from .project import Project, Chapter


# ---------- 階段 4：SD 生成首幀 ----------
def run_sd_first_frame(project: Project, ch: Chapter, options: dict) -> dict:
    shots = ch.read_json("storyboard.json")
    sd = SDClient()
    # 角色 → 立繪種子，讓含該角色的首幀沿用其種子，降低成像偏移
    seed_by_char = {c["name"]: c.get("seed") for c in project.read_characters()}
    done = 0
    for shot in shots:
        out = ch.dir / "frames" / f'{shot["id"]}.png'
        if out.exists():  # 段級斷點續傳
            shot["frame"] = f'frames/{shot["id"]}.png'
            continue
        ff = shot["first_frame_prompt"]
        present = shot.get("characters") or []
        seed = next((seed_by_char[n] for n in present if seed_by_char.get(n)), None)
        sd.txt2img(ff["positive"], ff.get("negative", ""), out, seed=seed)
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
    for shot in shots:
        frame = ch.dir / "frames" / f'{shot["id"]}.png'
        if not frame.exists():
            raise FileNotFoundError(f'缺少首幀 {shot["id"]}.png，請先執行 SD 階段')
        out = ch.dir / "clips" / f'{shot["id"]}.mp4'
        if out.exists():
            shot["clip"] = f'clips/{shot["id"]}.mp4'
            continue
        cp = shot["comfy_prompt"]
        motion_prompt = f'{cp.get("camera","")}, {cp.get("motion","")}'
        comfy.image_to_video(frame, motion_prompt, float(shot.get("duration", 4.0)), out)
        shot["clip"] = f'clips/{shot["id"]}.mp4'
        done += 1
        ch.log(f'ComfyUI 影片 {shot["id"]} 完成')
    ch.write_json("storyboard.json", shots)
    return {"generated": done, "total": len(shots), "mock": comfy.mock}


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
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(merged),
               "-vf", f"subtitles='{_ff_path(srt)}':force_style='{style}'",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", str(final)]
        subprocess.run(cmd, check=True)
        merged.unlink(missing_ok=True)
    else:
        merged.replace(final)
    ch.log(f"影片合成完成：{final.name}")
    return {"output": "output/final.mp4", "clips": len(clips)}


# ---------- helpers ----------
def _concat(clips: list[Path], out: Path) -> None:
    w, h, fps = settings.video.width, settings.video.height, settings.video.fps
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    n = len(clips)
    filt = "".join(
        f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}];"
        for i in range(n)
    )
    filt += "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[outv]"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *inputs,
           "-filter_complex", filt, "-map", "[outv]",
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
