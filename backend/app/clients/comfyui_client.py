"""本地 ComfyUI 圖生影 (image-to-video) 客戶端。

真實模式流程：
  1. /upload/image 上傳首幀
  2. 載入 workflow 模板 (ComfyUI 匯出的 API json)，把 %IMAGE% / %PROMPT% 佔位字串
     替換成實際檔名與提示詞
  3. POST /prompt 排入佇列，輪詢 /history/{id} 直到完成
  4. 從 /view 下載輸出影片

mock=True 時用 ffmpeg 對首幀做緩慢推鏡 (Ken Burns)，輸出一段 mp4，
讓沒有 ComfyUI 的環境也能產出影片片段驗證流程。
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from ..config import settings, WORKFLOW_DIR


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self) -> None:
        self.cfg = settings.comfy
        self.mock = self.cfg.mock

    def image_to_video(self, image_path: Path, prompt: str, seconds: float,
                       out_path: Path) -> Path:
        if self.mock:
            return self._mock(image_path, seconds, out_path)
        return self._real(image_path, prompt, seconds, out_path)

    # ---- 真實 ComfyUI ----
    def _real(self, image_path: Path, prompt: str, seconds: float, out_path: Path) -> Path:
        base = self.cfg.base_url
        with httpx.Client(timeout=self.cfg.poll_timeout) as c:
            # 1. 上傳首幀
            with open(image_path, "rb") as f:
                up = c.post(f"{base}/upload/image",
                            files={"image": (image_path.name, f, "image/png")},
                            data={"overwrite": "true"})
            up.raise_for_status()
            server_name = up.json()["name"]

            # 2. 注入 workflow（%IMAGE%/%PROMPT% 為字串；%DURATION% 連同引號換成整數秒數）
            wf = self._load_workflow()
            wf_str = json.dumps(wf).replace("%IMAGE%", server_name)\
                                   .replace("%PROMPT%", prompt.replace('"', "'"))\
                                   .replace('"%DURATION%"', str(max(1, int(round(seconds)))))
            workflow = json.loads(wf_str)

            client_id = uuid.uuid4().hex
            q = c.post(f"{base}/prompt",
                       json={"prompt": workflow, "client_id": client_id})
            q.raise_for_status()
            prompt_id = q.json()["prompt_id"]

            # 3. 輪詢歷史
            deadline = time.time() + self.cfg.poll_timeout
            while time.time() < deadline:
                h = c.get(f"{base}/history/{prompt_id}").json()
                if prompt_id in h:
                    outputs = h[prompt_id]["outputs"]
                    info = _find_output(outputs)
                    if info:
                        v = c.get(f"{base}/view", params=info)
                        v.raise_for_status()
                        out_path.write_bytes(v.content)
                        return out_path
                    raise ComfyError("workflow 完成但找不到影片輸出")
                time.sleep(self.cfg.poll_interval)
            raise ComfyError("ComfyUI 輪詢逾時")

    def _load_workflow(self) -> dict:
        path = WORKFLOW_DIR / self.cfg.workflow
        if not path.exists():
            raise ComfyError(f"找不到 workflow 模板：{path}")
        return json.loads(path.read_text("utf-8"))

    # ---- 離線 mock：ffmpeg 推鏡 ----
    def _mock(self, image_path: Path, seconds: float, out_path: Path) -> Path:
        fps = settings.video.fps
        frames = max(1, int(seconds * fps))
        vf = (
            f"scale={settings.video.width*2}:-2,"
            f"zoompan=z='min(zoom+0.0008,1.2)':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"s={settings.video.width}x{settings.video.height}:fps={fps},"
            f"format=yuv420p"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-loop", "1",
            "-i", str(image_path), "-t", str(seconds),
            "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(out_path),
        ]
        subprocess.run(cmd, check=True)
        return out_path


_VIDEO_EXTS = (".mp4", ".webm", ".mkv", ".mov", ".gif")


def _find_output(outputs: dict) -> dict | None:
    """從 ComfyUI 輸出節點找出產出的檔案。

    不同節點（SaveVideo / CreateVideo / VHS / SaveImage…）用的 key 不一（videos/gifs/images…），
    故掃描所有節點的所有 list 欄位，優先回傳影片副檔名的檔案，否則退回第一個有 filename 的。
    """
    fallback = None
    for node in outputs.values():
        if not isinstance(node, dict):
            continue
        for val in node.values():
            if not isinstance(val, list):
                continue
            for item in val:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                info = {"filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output")}
                if item["filename"].lower().endswith(_VIDEO_EXTS):
                    return info
                fallback = fallback or info
    return fallback
