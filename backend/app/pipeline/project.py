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

import hashlib
import json
import os
import random
import re
import shutil
import stat
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import DATA_DIR

# 流水線階段順序 (key, 顯示名稱)。character_cards 會寫入專案層級共用池，其餘為章節層級。
STAGES: list[tuple[str, str]] = [
    ("read_novel", "讀取小說"),
    ("character_cards", "角色卡產生"),
    ("location_cards", "場地卡產生"),
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


# 檔名/顯示名不允許的字元：Windows 保留字元 + 控制字元
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def clean_name(name: str, fallback: str = "未命名", limit: int = 80) -> str:
    """清理使用者輸入的名稱：移除不可用字元、壓縮空白、去頭尾的點與空白、限制長度。"""
    s = _INVALID_NAME.sub("", name or "")
    s = re.sub(r"\s+", " ", s).strip().strip(".").strip()
    return s[:limit] or fallback


def _derive_seed(base: int, key: str) -> int:
    """從專案基礎 seed 與 key（角色名/鏡頭 id）穩定推導出一個子 seed。

    同一專案 seed → 同一組子 seed（可重現）；不同 key → 不同子 seed（角色/鏡頭仍有變化）。
    """
    h = hashlib.md5(f"{base}:{key}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % 2_000_000_000 + 1


def _rmtree(path: Path) -> None:
    """刪除資料夾（Windows 友善）。

    Windows 上目錄/檔案被防毒、索引或前端輪詢短暫持有時，rmtree 會偶發存取被拒；
    這裡先清唯讀旗標、重試數次。為確保即使資料夾殘留、專案也不再出現在清單，
    先刪掉 state.json（list_all/load 以它為準）。
    """
    if not path.exists():
        return
    sp = path / "state.json"
    try:
        sp.unlink(missing_ok=True)   # 先讓專案/章節從清單消失
    except OSError:
        pass
    for attempt in range(10):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        os.chmod(os.path.join(root, f), stat.S_IWRITE)
                    except OSError:
                        pass
            time.sleep(0.1 * (attempt + 1))
    shutil.rmtree(path, ignore_errors=True)   # 盡力而為


def _atomic_write_json(path: Path, obj: Any) -> None:
    """以 .tmp + replace 原子寫入 JSON。

    Windows 上當另一個 handle（例如前端輪詢正在讀 state.json）短暫持有目標檔時，
    replace 會偶發 WinError 5（存取被拒）。此處重試數次；真的搬不動才退回直接覆寫。
    """
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    for attempt in range(10):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    # 最後手段：直接覆寫（非原子，但勝過整步失敗）
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    tmp.unlink(missing_ok=True)


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
            "name": clean_name(name, "未命名專案"),
            "created_at": time.time(),
            "seed": random.randint(1, 2_000_000_000),   # 專案層級固定 seed，整個專案共用
            "chapters": [],          # [{id, title, created_at}]
            "logs": [],
        }
        p.save()
        return p

    @classmethod
    def delete(cls, pid: str) -> None:
        d = DATA_DIR / "projects" / pid
        if not (d / "state.json").exists():
            raise FileNotFoundError(f"找不到專案 {pid}")
        _rmtree(d)

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
        _atomic_write_json(self.state_path, self.state)

    def log(self, msg: str) -> None:
        self.state.setdefault("logs", []).append({"t": time.time(), "msg": msg})
        self.state["logs"] = self.state["logs"][-300:]
        self.save()

    # ---- 專案層級 seed ----
    def base_seed(self) -> int:
        """專案固定 seed（舊專案沒有則補一個並存檔）。"""
        s = self.state.get("seed")
        if not s:
            s = random.randint(1, 2_000_000_000)
            self.state["seed"] = s
            self.save()
        return s

    def set_seed(self, seed: int | None = None) -> int:
        """設定（或隨機重設）專案 seed，回傳新值。"""
        self.state["seed"] = int(seed) if seed else random.randint(1, 2_000_000_000)
        self.save()
        return self.state["seed"]

    def derive_seed(self, key: str) -> int:
        """由專案 seed 推導角色/鏡頭專用 seed。"""
        return _derive_seed(self.base_seed(), key)

    # ---- 章節管理 ----
    def add_chapter(self, title: str, novel_text: str = "") -> "Chapter":
        cid = uuid.uuid4().hex[:10]
        title = clean_name(title, f"第 {len(self.state['chapters']) + 1} 章")
        self.state["chapters"].append({"id": cid, "title": title, "created_at": time.time()})
        self.save()
        ch = Chapter(self, cid, title)
        ch.write_text("novel.txt", novel_text or "")
        return ch

    def remove_chapter(self, cid: str) -> None:
        metas = self.state.get("chapters", [])
        if not any(c["id"] == cid for c in metas):
            raise FileNotFoundError(f"找不到章節 {cid}")
        self.state["chapters"] = [c for c in metas if c["id"] != cid]
        self.save()
        _rmtree(self.dir / "chapters" / cid)

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

    # ---- 專案層級共用場地卡 ----
    def read_locations(self) -> list[dict]:
        path = self.dir / "locations.json"
        if not path.exists():
            return []
        return json.loads(path.read_text("utf-8"))

    def write_locations(self, locs: list[dict]) -> None:
        (self.dir / "locations.json").write_text(
            json.dumps(locs, ensure_ascii=False, indent=2), "utf-8")

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
        _atomic_write_json(self.state_path, self.state)

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
