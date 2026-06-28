"""FastAPI 入口：流水線控制 API + 靜態前端。

資料模型：專案 → 多個獨立章節；角色卡為專案層級、跨章共用。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings, ROOT
from .pipeline.project import Project, Chapter, STAGES
from .pipeline import orchestrator as orch

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
    return {"characters": cards}


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
