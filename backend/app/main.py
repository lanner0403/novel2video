"""FastAPI 入口：流水線控制 API + 靜態前端。

資料模型：專案 → 多個獨立章節；角色卡為專案層級、跨章共用。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings, ROOT
from .pipeline.project import Project, Chapter, STAGES, slugify
from .pipeline import orchestrator as orch
from .pipeline import stages_text as st

app = FastAPI(title="小說轉影片流水線")

FRONTEND = ROOT / "frontend"

# 章節層級 artifact（角色卡是專案層級，走 /characters）
CHAPTER_ARTIFACTS = {
    "read_novel": "segments.json",
    "storyboard": "storyboard.json",
}


# ---------- 模型 ----------
class CreateProject(BaseModel):
    name: str
    novel_text: str = ""          # 給定時自動建立「第 1 章」


class CreateChapter(BaseModel):
    title: str = ""
    novel_text: str = ""


class RunRequest(BaseModel):
    start: str | None = None       # 從此階段往後跑
    only: str | None = None        # 只跑此階段
    options: dict = {}             # 階段參數，如 character_cards 的 {"regenerate": [...]}


class EditCharacter(BaseModel):
    appearance: str | None = None
    personality: str | None = None
    sd_prompt: str | None = None
    aliases: list[str] | None = None
    seed: int | None = None


class RegenPortrait(BaseModel):
    seed: int | None = None


# ---------- 後設 ----------
@app.get("/api/settings")
def get_settings() -> dict:
    return {
        "settings": settings.public_dict(),
        "stages": [{"key": k, "label": l} for k, l in STAGES],
    }


# ---------- 專案 CRUD ----------
@app.get("/api/projects")
def list_projects() -> dict:
    return {"projects": Project.list_all()}


@app.post("/api/projects")
def create_project(body: CreateProject) -> dict:
    p = Project.create(body.name or "未命名專案")
    if body.novel_text.strip():
        p.add_chapter("第 1 章", body.novel_text)
    return _project_view(p)


@app.get("/api/projects/{pid}")
def get_project(pid: str) -> dict:
    return _project_view(_load(pid))


@app.post("/api/projects/{pid}/seed")
def set_project_seed(pid: str, body: RegenPortrait) -> dict:
    """設定或隨機重設專案 seed（影響之後新生成的立繪/首幀，已存在檔案不變）。"""
    p = _load(pid)
    return {"seed": p.set_seed(body.seed)}


@app.delete("/api/projects/{pid}")
def delete_project(pid: str) -> dict:
    p = _load(pid)
    # 任一章節執行中則不刪
    for m in p.list_chapters():
        if orch.is_running(pid, m["id"]):
            raise HTTPException(409, "專案有章節正在執行中，請稍候再刪除")
    Project.delete(pid)
    return {"ok": True}


# ---------- 章節 ----------
@app.post("/api/projects/{pid}/chapters")
def add_chapter(pid: str, body: CreateChapter) -> dict:
    p = _load(pid)
    ch = p.add_chapter(body.title, body.novel_text)
    return _chapter_view(p, ch)


@app.get("/api/projects/{pid}/chapters/{cid}")
def get_chapter(pid: str, cid: str) -> dict:
    p = _load(pid)
    return _chapter_view(p, _load_chapter(p, cid))


@app.delete("/api/projects/{pid}/chapters/{cid}")
def delete_chapter(pid: str, cid: str) -> dict:
    p = _load(pid)
    _load_chapter(p, cid)
    if orch.is_running(pid, cid):
        raise HTTPException(409, "此章節正在執行中，請稍候再刪除")
    p.remove_chapter(cid)
    return {"ok": True}


@app.post("/api/projects/{pid}/chapters/{cid}/novel")
async def set_chapter_novel(pid: str, cid: str, text: str = Form(default=""),
                            file: UploadFile | None = File(default=None)) -> dict:
    p = _load(pid)
    ch = _load_chapter(p, cid)
    content = (await file.read()).decode("utf-8", errors="ignore") if file else text
    ch.write_text("novel.txt", content)
    ch.log(f"更新小說內容（{len(content)} 字）")
    return {"ok": True, "chars": len(content)}


# ---------- 執行流水線（章節層級）----------
@app.post("/api/projects/{pid}/chapters/{cid}/run")
def run(pid: str, cid: str, body: RunRequest) -> dict:
    p = _load(pid)
    _load_chapter(p, cid)
    if orch.is_running(pid, cid):
        raise HTTPException(409, "此章節正在執行中，請稍候")
    try:
        plan = orch.plan_stages(body.start, body.only)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not orch.run_stages_async(pid, cid, start=body.start, only=body.only,
                                 options=body.options):
        raise HTTPException(409, "此章節正在執行中")
    return {"ok": True, "plan": plan}


# ---------- 章節 artifact / 檔案 ----------
@app.get("/api/projects/{pid}/chapters/{cid}/artifact/{key}")
def get_chapter_artifact(pid: str, cid: str, key: str) -> JSONResponse:
    p = _load(pid)
    ch = _load_chapter(p, cid)
    name = CHAPTER_ARTIFACTS.get(key)
    if not name:
        raise HTTPException(404, "無此 artifact")
    if not ch.has(name):
        return JSONResponse({"available": False, "data": None})
    return JSONResponse({"available": True, "data": ch.read_json(name)})


@app.put("/api/projects/{pid}/chapters/{cid}/shots/{shot_id}")
def edit_shot(pid: str, cid: str, shot_id: str, body: dict) -> dict:
    """編輯單一鏡頭（合併允許的欄位）。改了 prompt 後可再單獨重生首幀/片段。"""
    p = _load(pid)
    ch = _load_chapter(p, cid)
    shots = ch.read_json("storyboard.json")
    shot = next((s for s in shots if s.get("id") == shot_id), None)
    if shot is None:
        raise HTTPException(404, "找不到鏡頭")
    for k in ("summary", "characters", "first_frame_prompt", "comfy_prompt",
              "narration", "dialogue", "voice_tone", "duration"):
        if k in body:
            shot[k] = body[k]
    ch.write_json("storyboard.json", shots)
    return shot


@app.post("/api/projects/{pid}/chapters/{cid}/shots/{shot_id}/frame")
def regenerate_frame(pid: str, cid: str, shot_id: str) -> dict:
    """重生單一鏡頭首幀：刪該檔後跑 sd_first_frame（其餘已存在者會被跳過）。"""
    p = _load(pid)
    ch = _load_chapter(p, cid)
    if orch.is_running(pid, cid):
        raise HTTPException(409, "此章節正在執行中，請稍候")
    if not _shot_exists(ch, shot_id):
        raise HTTPException(404, "找不到鏡頭")
    (ch.dir / "frames" / f"{shot_id}.png").unlink(missing_ok=True)
    orch.run_stages_async(pid, cid, only="sd_first_frame")
    return {"ok": True}


@app.post("/api/projects/{pid}/chapters/{cid}/shots/{shot_id}/clip")
def regenerate_clip(pid: str, cid: str, shot_id: str) -> dict:
    """重生單一鏡頭片段：刪該檔後跑 comfy_video（其餘已存在者會被跳過）。"""
    p = _load(pid)
    ch = _load_chapter(p, cid)
    if orch.is_running(pid, cid):
        raise HTTPException(409, "此章節正在執行中，請稍候")
    if not _shot_exists(ch, shot_id):
        raise HTTPException(404, "找不到鏡頭")
    (ch.dir / "clips" / f"{shot_id}.mp4").unlink(missing_ok=True)
    orch.run_stages_async(pid, cid, only="comfy_video")
    return {"ok": True}


@app.get("/api/projects/{pid}/chapters/{cid}/file/{path:path}")
def get_chapter_file(pid: str, cid: str, path: str) -> FileResponse:
    p = _load(pid)
    ch = _load_chapter(p, cid)
    return _safe_file(ch.dir, path)


@app.get("/api/projects/{pid}/chapters/{cid}/logs")
def get_chapter_logs(pid: str, cid: str) -> dict:
    p = _load(pid)
    ch = _load_chapter(p, cid)
    return {"logs": ch.state.get("logs", [])[-80:], "running": orch.is_running(pid, cid)}


# ---------- 專案層級共用角色卡 ----------
@app.get("/api/projects/{pid}/characters")
def get_characters(pid: str) -> dict:
    p = _load(pid)
    cards = p.read_characters()
    for c in cards:
        rel = c.get("portrait") or p.portrait_rel(c["name"])
        c["portrait"] = rel
        c["portrait_available"] = (p.dir / rel).exists()
        c["regenerating"] = orch.is_running_key(f"{pid}:char:{slugify(c['name'])}")
    return {"characters": cards}


@app.put("/api/projects/{pid}/characters/{name}")
def edit_character(pid: str, name: str, body: EditCharacter) -> dict:
    p = _load(pid)
    cards = p.read_characters()
    card = next((c for c in cards if c["name"] == name), None)
    if card is None:
        raise HTTPException(404, "找不到角色")
    for f in ("appearance", "personality", "sd_prompt"):
        v = getattr(body, f)
        if v is not None:
            card[f] = v
    if body.aliases is not None:
        card["aliases"] = body.aliases
    if body.seed is not None:
        card["seed"] = int(body.seed)
    p.write_characters(cards)
    return card


@app.post("/api/projects/{pid}/characters/{name}/regenerate")
def regenerate_character_portrait(pid: str, name: str, body: RegenPortrait) -> dict:
    p = _load(pid)
    if not any(c["name"] == name for c in p.read_characters()):
        raise HTTPException(404, "找不到角色")
    key = f"{pid}:char:{slugify(name)}"
    if orch.is_running_key(key):
        raise HTTPException(409, "此角色立繪正在生成中")
    seed = body.seed
    orch.run_task_async(key, lambda: st.regenerate_portrait(p, name, seed))
    return {"ok": True}


@app.get("/api/projects/{pid}/file/{path:path}")
def get_project_file(pid: str, path: str) -> FileResponse:
    """專案層級檔案（如角色立繪 characters/{slug}.png）。"""
    p = _load(pid)
    return _safe_file(p.dir, path)


# ---------- helpers ----------
def _load(pid: str) -> Project:
    try:
        return Project.load(pid)
    except FileNotFoundError:
        raise HTTPException(404, "找不到專案")


def _load_chapter(p: Project, cid: str) -> Chapter:
    try:
        return p.get_chapter(cid)
    except FileNotFoundError:
        raise HTTPException(404, "找不到章節")


def _shot_exists(ch: Chapter, shot_id: str) -> bool:
    """確認 shot_id 真的在分鏡中（兼作檔名防護，避免用任意字串拼路徑）。"""
    if not ch.has("storyboard.json"):
        return False
    return any(s.get("id") == shot_id for s in ch.read_json("storyboard.json"))


def _safe_file(base, path: str) -> FileResponse:
    target = (base / path).resolve()
    if not str(target).startswith(str(base.resolve())) or not target.exists():
        raise HTTPException(404, "檔案不存在")
    return FileResponse(target)


def _chapter_summary(p: Project, meta: dict) -> dict:
    """給專案列表/側欄用的章節摘要（含各階段狀態）。"""
    ch = p.get_chapter(meta["id"])
    return {
        "id": ch.id,
        "title": meta.get("title", ch.id),
        "running": orch.is_running(p.id, ch.id),
        "stages": {k: ch.stage_status(k) for k in [s[0] for s in STAGES]},
    }


def _project_view(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.state.get("name"),
        "seed": p.base_seed(),
        "chapters": [_chapter_summary(p, m) for m in p.list_chapters()],
        "character_count": len(p.read_characters()),
    }


def _chapter_view(p: Project, ch: Chapter) -> dict:
    stages = []
    for key, label in STAGES:
        s = ch.state["stages"].get(key, {"status": "pending"})
        stages.append({
            "key": key, "label": label,
            "status": s.get("status", "pending"),
            "result": s.get("result"),
            "error": s.get("error"),
        })
    files = {
        "frames": sorted(x.name for x in (ch.dir / "frames").glob("*.png")),
        "clips": sorted(x.name for x in (ch.dir / "clips").glob("*.mp4")),
        "subtitle": "subtitles/full.srt" if ch.has("subtitles/full.srt") else None,
        "output": "output/final.mp4" if ch.has("output/final.mp4") else None,
    }
    return {
        "id": ch.id,
        "project_id": p.id,
        "title": ch.title,
        "novel_text": ch.read_text("novel.txt") if ch.has("novel.txt") else "",
        "novel_chars": len(ch.read_text("novel.txt")) if ch.has("novel.txt") else 0,
        "running": orch.is_running(p.id, ch.id),
        "stages": stages,
        "files": files,
    }


# ---------- 靜態前端 ----------
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND), html=True), name="frontend")
