"""流水線調度器（章節層級）。

- 每個階段都是獨立函式，簽名 (project, chapter, options)，輸入/輸出皆落地存檔。
- run_stages 可執行：單一階段、從某階段往後、或全部。
- character_cards 寫入專案層級共用角色池，其餘階段為章節層級。
- 在背景執行緒跑，狀態寫入 chapter.state，前端輪詢取得進度。
- 以 (專案, 章節) 為單位防重複觸發；不同章節可並行。
"""

from __future__ import annotations

import threading
import traceback
from typing import Callable

from .project import Project, Chapter, STAGE_KEYS, STAGES
from . import stages_text as st
from . import stages_media as sm

# 階段 key -> 處理函式 (project, chapter, options) -> dict
HANDLERS: dict[str, Callable[[Project, Chapter, dict], dict]] = {
    "read_novel": st.run_read_novel,
    "character_cards": st.run_character_cards,
    "storyboard": st.run_storyboard,
    "sd_first_frame": sm.run_sd_first_frame,
    "comfy_video": sm.run_comfy_video,
    "subtitles": sm.run_subtitles,
    "compose": sm.run_compose,
}

# 正在執行的 (專案:章節)
_running: set[str] = set()
_lock = threading.Lock()


def _key(pid: str, cid: str) -> str:
    return f"{pid}:{cid}"


def stage_label(key: str) -> str:
    return dict(STAGES).get(key, key)


def is_running(pid: str, cid: str) -> bool:
    return _key(pid, cid) in _running


def plan_stages(start: str | None, only: str | None) -> list[str]:
    """決定要執行哪些階段。only：只跑此階段；start：從此往後；皆 None：全部。"""
    if only:
        if only not in STAGE_KEYS:
            raise ValueError(f"未知階段：{only}")
        return [only]
    if start:
        if start not in STAGE_KEYS:
            raise ValueError(f"未知階段：{start}")
        i = STAGE_KEYS.index(start)
        return STAGE_KEYS[i:]
    return list(STAGE_KEYS)


def run_stages(pid: str, cid: str, start: str | None = None, only: str | None = None,
               options: dict | None = None) -> None:
    """同步執行（內部用）。逐階段跑，更新章節狀態。"""
    project = Project.load(pid)
    ch = project.get_chapter(cid)
    options = options or {}
    plan = plan_stages(start, only)
    ch.log(f"開始執行：{' → '.join(stage_label(k) for k in plan)}")

    for key in plan:
        ch.set_stage(key, "running")
        try:
            result = HANDLERS[key](project, ch, options)
            ch.set_stage(key, "done", result=result)
            ch.log(f"✓ {stage_label(key)} 完成 {result}")
        except Exception as e:  # noqa: BLE001
            ch.set_stage(key, "error", error=str(e))
            ch.log(f"✗ {stage_label(key)} 失敗：{e}")
            ch.log(traceback.format_exc().splitlines()[-1])
            raise


def run_stages_async(pid: str, cid: str, start: str | None = None, only: str | None = None,
                     options: dict | None = None) -> bool:
    """背景執行緒啟動。回傳是否成功觸發（False 表示該章節已在執行中）。"""
    k = _key(pid, cid)
    with _lock:
        if k in _running:
            return False
        _running.add(k)

    def _worker() -> None:
        try:
            run_stages(pid, cid, start=start, only=only, options=options)
        except Exception:  # noqa: BLE001 — 狀態已記錄
            pass
        finally:
            with _lock:
                _running.discard(k)

    threading.Thread(target=_worker, daemon=True).start()
    return True
