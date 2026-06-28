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
        comfy.image_to_video(frame, _video_prompt(shot),
                             float(shot.get("duration", 4.0)), out)
        shot["clip"] = f'clips/{shot["id"]}.mp4'
        done += 1
        ch.log(f'ComfyUI 影片 {shot["id"]} 完成')
    ch.write_json("storyboard.json", shots)
    return {"generated": done, "total": len(shots), "mock": comfy.mock}


def _video_prompt(shot: dict) -> str:
    """組出給圖生影/語音模型的提示：場景＋人物＋運鏡＋氛圍，並帶入要唸的台詞與語氣。

    LTX-2.3 等帶語音的模型會依此生成口白；旁白/對白用中文、其餘描述用英文。
    """
    cp = shot.get("comfy_prompt", {})
    parts = [cp.get("scene"), cp.get("characters"),
             ", ".join(t for t in [cp.get("camera"), cp.get("motion")] if t),
             f'mood: {cp.get("mood")}' if cp.get("mood") else ""]
    speech = (shot.get("dialogue") or shot.get("narration") or "").strip()
    if speech:
        tone = shot.get("voice_tone") or "沉穩"
        kind = "對白" if shot.get("dialogue") else "旁白"
        parts.append(f'{kind}（語氣：{tone}）：{speech}')
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
