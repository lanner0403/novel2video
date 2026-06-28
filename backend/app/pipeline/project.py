"""專案 / 章節工作區與斷點狀態。

資料模型：
  data/projects/{pid}/
  ├─ state.json            專案層級：名稱、章節索引、logs
  ├─ characters.json       專案層級「共用角色卡」池（跨章節共用）
  ├─ characters/{slug}.png 角色立繪（避免成像偏移的參考圖）
  └─ chapters/{cid}/       每章一個獨立工作區（資料夾彼此分開）
     ├─ state.json         章節層級：標題、各階段狀態、logs
     ├─ novel.txt segments.json storyboard.json
     └─ frames/ clips/ subtitles/ output/

角色卡是「專案層級、跨章共用」；其餘 artifact 都是「章節層級、各自獨立」。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import DATA_DIR

# 流水線階段順序 (key, 顯示名稱)。character_cards 會寫入專案層級共用池，其餘為章節層級。
STAGES: list[tuple[str, str]] = [
    ("read_novel", "讀取小說"),
    ("character_cards", "角色卡產生"),
    ("storyboard", "分鏡產生"),
    ("sd_first_frame", "SD 生成首幀"),
    ("comfy_video", "ComfyUI 生成影片"),
    ("subtitles", "字幕加載"),
    ("compose", "影片合成"),
]
STAGE_KEYS = [k for k, _ in STAGES]


def slugify(name: str) -> str:
    """角色名 → 檔名安全 slug（保留中英數，其餘換底線）。"""
    s = re.sub(r"[^\w一-龥]+", "_", name.strip())
    return s.strip("_") or "char"


# ============================================================
# 專案
# ============================================================
class Project:
    def __init__(self, pid: str):
        self.id = pid
        self.dir = DATA_DIR / "projects" / pid
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "chapters").mkdir(exist_ok=True)
        (self.dir / "characters").mkdir(exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state: dict[str, Any] = self._load()

    # ---- 建立 / 載入 ----
    @classmethod
    def create(cls, name: str) -> "Project":
        pid = uuid.uuid4().hex[:12]
        p = cls(pid)
        p.state = {
            "id": pid,
            "name": name,
            "created_at": time.time(),
            "chapters": [],          # [{id, title, created_at}]
            "logs": [],
        }
        p.save()
        return p

    @classmethod
    def load(cls, pid: str) -> "Project":
        p = cls(pid)
        if not p.state_path.exists():
            raise FileNotFoundError(f"找不到專案 {pid}")
        return p

    @classmethod
    def list_all(cls) -> list[dict]:
        root = DATA_DIR / "projects"
        out = []
        if not root.exists():
            return out
        for d in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            sp = d / "state.json"
            if sp.exists():
                try:
                    s = json.loads(sp.read_text("utf-8"))
                    out.append({
                        "id": s["id"], "name": s.get("name", s["id"]),
                        "created_at": s.get("created_at"),
                        "chapters": len(s.get("chapters", [])),
                    })
                except Exception:
                    continue
        return out

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text("utf-8"))
        return {"id": self.id, "name": self.id, "chapters": [], "logs": []}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.state_path)

    def log(self, msg: str) -> None:
        self.state.setdefault("logs", []).append({"t": time.time(), "msg": msg})
        self.state["logs"] = self.state["logs"][-300:]
        self.save()

    # ---- 章節管理 ----
    def add_chapter(self, title: str, novel_text: str = "") -> "Chapter":
        cid = uuid.uuid4().hex[:10]
        title = title or f"第 {len(self.state['chapters']) + 1} 章"
        self.state["chapters"].append({"id": cid, "title": title, "created_at": time.time()})
        self.save()
        ch = Chapter(self, cid, title)
        ch.write_text("novel.txt", novel_text or "")
        return ch

    def list_chapters(self) -> list[dict]:
        return self.state.get("chapters", [])

    def get_chapter(self, cid: str) -> "Chapter":
        meta = next((c for c in self.state.get("chapters", []) if c["id"] == cid), None)
        if meta is None:
            raise FileNotFoundError(f"找不到章節 {cid}")
        return Chapter(self, cid, meta.get("title", cid))

    # ---- 專案層級共用角色池 ----
    def read_characters(self) -> list[dict]:
        path = self.dir / "characters.json"
        if not path.exists():
            return []
        return json.loads(path.read_text("utf-8"))

    def write_characters(self, cards: list[dict]) -> None:
        (self.dir / "characters.json").write_text(
            json.dumps(cards, ensure_ascii=False, indent=2), "utf-8")

    def portrait_path(self, name: str) -> Path:
        return self.dir / "characters" / f"{slugify(name)}.png"

    def portrait_rel(self, name: str) -> str:
        return f"characters/{slugify(name)}.png"

    def has(self, name: str) -> bool:
        return (self.dir / name).exists()


# ============================================================
# 章節（每章獨立工作區）
# ============================================================
class Chapter:
    def __init__(self, project: Project, cid: str, title: str = ""):
        self.project = project
        self.id = cid
        self.title = title
        self.dir = project.dir / "chapters" / cid
        self.dir.mkdir(parents=True, exist_ok=True)
        for sub in ("frames", "clips", "subtitles", "output"):
            (self.dir / sub).mkdir(exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text("utf-8"))
        return {"id": self.id, "title": self.title,
                "stages": {k: {"status": "pending"} for k in STAGE_KEYS},
                "logs": []}

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self.state_path)

    # ---- 階段狀態 ----
    def set_stage(self, key: str, status: str, **meta: Any) -> None:
        self.state["stages"][key] = {"status": status, "updated_at": time.time(), **meta}
        self.save()

    def stage_status(self, key: str) -> str:
        return self.state["stages"].get(key, {}).get("status", "pending")

    def is_done(self, key: str) -> bool:
        return self.stage_status(key) == "done"

    def log(self, msg: str) -> None:
        self.state.setdefault("logs", []).append({"t": time.time(), "msg": msg})
        self.state["logs"] = self.state["logs"][-300:]
        self.save()

    # ---- artifact 讀寫（章節層級）----
    def write_json(self, name: str, obj: Any) -> None:
        (self.dir / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")

    def read_json(self, name: str) -> Any:
        path = self.dir / name
        if not path.exists():
            raise FileNotFoundError(f"缺少上游檔案：{name}（請先執行對應階段）")
        return json.loads(path.read_text("utf-8"))

    def write_text(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, "utf-8")

    def read_text(self, name: str) -> str:
        path = self.dir / name
        if not path.exists():
            raise FileNotFoundError(f"缺少上游檔案：{name}（請先執行對應階段）")
        return path.read_text("utf-8")

    def has(self, name: str) -> bool:
        return (self.dir / name).exists()
