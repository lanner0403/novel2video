# CLAUDE.md — 小說轉影片流水線（Novel → Reel）

把小說自動轉成直式短影片的本地化流水線。七個階段，每階段的輸入/輸出都**落地存檔**，
因此可整條跑、可單獨跑某一步、也可從任一階段往後續跑（斷點續傳）。

> 預設三個外部服務（LLM / SD / ComfyUI）都是 **mock 模式**，無需 API key、GPU、模型
> 即可把整條流水線跑通，產出一支帶字幕的成片。先用 mock 驗證流程，再逐步換真實服務。

## 核心資料模型（重要）

**專案 → 多個獨立章節；角色卡是專案層級、跨章共用。**

- 一個**專案**底下可手動逐章新增多個**章節**，每章一個獨立工作區（資料夾彼此分開）。
- `read_novel / storyboard / sd_first_frame / comfy_video / subtitles / compose` 都是**章節層級**，
  各章 artifact 互不干擾。
- `character_cards` 寫入**專案層級共用角色池** `characters.json`：本章新角色才生成，
  既有角色預設沿用；UI 可勾選，取消勾選者重生（`run` 時帶 `options.regenerate`）。
- 產角色卡時同步呼叫 SD 產生**角色立繪** `characters/{slug}.png`，並為每個角色記錄固定 `seed`；
  含該角色的鏡頭首幀會沿用其 seed，降低成像偏移。

## 技術棧

- **後端**：Python 3.10+、FastAPI、uvicorn、httpx、Pillow。背景執行緒跑流水線。
- **前端**：單一 `frontend/index.html`（原生 JS），由後端一起服務，無建置步驟。
- **SD 出圖**：本地 **HuggingFace diffusers**（`clients/sd_client.py`），非 A1111 WebUI。
  真實出圖需 `pip install -r backend/requirements-diffusers.txt`（torch 等，數 GB，故獨立成檔）。
- **外部依賴**：`ffmpeg`（必須，用於 mock 推鏡與合成）；可選的本地 ComfyUI、任何 OpenAI 相容 LLM 端點。

## 執行方式

```bash
./run.sh                 # 一鍵：裝依賴 + 啟動，開 http://127.0.0.1:8000
# 或手動：
cd backend
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Windows 用 `run.bat`。前端操作：「＋ 新增專案」貼上小說 → 「▶ 全部執行」或對單一階段按「只跑這步 / 從這裡往後」。範例小說在 `samples/`。

## 流水線七階段

| key | 階段 | 產物 | 服務 | 程式碼 |
|-----|------|------|------|--------|
| `read_novel` | 讀取小說 | `segments.json` | — | `stages_text.run_read_novel` |
| `character_cards` | 角色卡產生 | `characters.json` | LLM | `stages_text.run_character_cards` |
| `storyboard` | 分鏡產生 | `storyboard.json` | LLM | `stages_text.run_storyboard` |
| `sd_first_frame` | SD 生成首幀 | `frames/*.png` | Stable Diffusion | `stages_media.run_sd_first_frame` |
| `comfy_video` | ComfyUI 生成影片 | `clips/*.mp4` | ComfyUI | `stages_media.run_comfy_video` |
| `subtitles` | 字幕加載 | `subtitles/full.srt` | — | `stages_media.run_subtitles` |
| `compose` | 影片合成 | `output/final.mp4` | ffmpeg | `stages_media.run_compose` |

階段順序定義在 `pipeline/project.py` 的 `STAGES`，調度在 `pipeline/orchestrator.py` 的 `HANDLERS`。
**新增/調整階段時，這兩處要同步改。** 階段處理函式簽名統一為 `(project, chapter, options) -> dict`。

## 架構

```
backend/app/
├─ main.py              # FastAPI 路由（專案/章節/角色）+ 靜態前端掛載
├─ config.py            # 設定（dataclass + .env 覆寫，含 *_MOCK 開關）
├─ clients/             # 三個外部服務客戶端，每個都內建 mock
│  ├─ llm_client.py     #   OpenAI 相容 chat completions（mock → mock_builder 假資料）
│  ├─ sd_client.py      #   本地 diffusers txt2img（mock → Pillow 佔位圖；pipeline 模組層級快取）
│  └─ comfyui_client.py #   ComfyUI 圖生影（mock → ffmpeg Ken Burns 推鏡）
├─ pipeline/
│  ├─ project.py        # Project（專案+共用角色池+章節索引）、Chapter（每章獨立工作區）、slugify
│  ├─ orchestrator.py   # plan_stages / run_stages（背景執行緒、以 pid:cid 防重複觸發）
│  ├─ stages_text.py    # 階段 1-3（含角色池合併 + 立繪生成）
│  └─ stages_media.py   # 階段 4-7（含 _ff_path：修正 Windows 字幕路徑跳脫）
└─ utils/text.py        # 分段 / 對白擷取 / 角色名啟發式偵測
```

## 資料夾結構

```
data/projects/{pid}/
├─ state.json            # 專案：name、chapters[]、logs
├─ characters.json       # 專案層級共用角色卡（含 name/sd_prompt/seed/portrait）
├─ characters/{slug}.png # 角色立繪
└─ chapters/{cid}/       # 每章獨立
   ├─ state.json         #   章節：title、各階段狀態、logs
   ├─ novel.txt segments.json storyboard.json
   └─ frames/ clips/ subtitles/ output/
```

## 核心概念

- **斷點續傳**：階段 4、5 會跳過已存在的 `frames/*.png` 與 `clips/*.mp4`，只補沒做的鏡頭。
- **上游缺檔**：`Chapter.read_json/read_text` 缺檔時拋出明確錯誤（「請先執行對應階段」）。
- **執行模型**：`run_stages_async` 在 daemon thread 跑，`_running` set + lock 以 `pid:cid` 為單位
  防同一章節重複觸發（不同章節可並行）；前端輪詢章節 view 與 `logs` 取得進度。
- **角色池合併**：`stages_text.run_character_cards` 把本章抽到的角色併入專案池；
  `options.regenerate` 名單中的角色（即使本章沒抽到、但已在池中）會用既有卡重生並換新 seed。
- **立繪 seed 一致性**：`sd_first_frame` 從專案池查角色 seed，含該角色的首幀沿用，降低成像偏移。

## API

```
GET  /api/settings                                   # 公開設定（隱去 api_key）+ 階段清單
GET  /api/projects                                   # 專案列表
POST /api/projects                                   # 建立 {name, novel_text?}（給 text 則建第 1 章）
GET  /api/projects/{pid}                             # 專案詳情（章節摘要 + 角色數）
POST /api/projects/{pid}/chapters                    # 新增章節 {title, novel_text}
GET  /api/projects/{pid}/chapters/{cid}              # 章節詳情（各階段狀態、檔案可用性）
POST /api/projects/{pid}/chapters/{cid}/novel        # 更新章節內文（text 或 file 上傳）
POST /api/projects/{pid}/chapters/{cid}/run          # {} 全跑 / {only} / {start} / {options:{regenerate}}
GET  /api/projects/{pid}/chapters/{cid}/artifact/{k} # read_novel|storyboard 的 JSON
GET  /api/projects/{pid}/chapters/{cid}/file/{path}  # 章節內檔案（frames/clips/output…，防目錄穿越）
GET  /api/projects/{pid}/chapters/{cid}/logs         # 章節 logs + running（前端輪詢）
GET  /api/projects/{pid}/characters                  # 專案共用角色卡（含 portrait_available）
GET  /api/projects/{pid}/file/{path}                 # 專案層級檔案（如 characters/{slug}.png）
```

## Mock ↔ 真實服務切換

複製 `.env.example` 為 `.env`，把對應 `N2V_*_MOCK` 改 `false` 並填位址。三個服務各自獨立，
可單獨切換方便分段除錯。關鍵環境變數（完整清單見 `config.py` 與 `.env.example`）：

```bash
N2V_LLM_MOCK=false   N2V_LLM_BASE_URL=...  N2V_LLM_API_KEY=...  N2V_LLM_MODEL=...
N2V_SD_MOCK=false    N2V_SD_MODEL=stabilityai/stable-diffusion-2-1  N2V_SD_DEVICE=auto  # 需裝 diffusers
N2V_COMFY_MOCK=false N2V_COMFY_BASE_URL=http://127.0.0.1:8188   N2V_COMFY_WORKFLOW=svd_i2v.json
```

**SD diffusers**：`sd_client.py` 懶加載 `StableDiffusionPipeline`，pipeline 在**模組層級快取**
（`_get_pipe`），跨階段（首幀、立繪）共用同一個，不重複載入。device `auto` 依序選 cuda > mps > cpu。

**ComfyUI workflow 模板**：把 ComfyUI「Save (API Format)」匯出的 json 放進 `backend/workflows/`，
並把 LoadImage 的 `image` 改成 `%IMAGE%`、CLIPTextEncode 的 `text` 改成 `%PROMPT%`，後端執行時自動替換。

## 慣例與注意事項

- **語言**：程式碼註解、log、API 訊息、UI 皆用**繁體中文**；SD/ComfyUI 提示詞用**英文**。
- **影片規格**：直式 9:16，預設 1080×1920 @ 24fps，每鏡頭預設 4 秒（見 `config.VideoSettings`）。
- **mock 限制**：角色名為離線啟發式推測（`utils/text.extract_names`），真實 LLM 模式才會正確擷取。
- **字幕計時**：目前用旁白/對白長度估時，非逐字時間軸；要精準需接 TTS。
- **狀態檔寫入**：`Project.save` 用 `.tmp` + `replace` 原子寫入，避免半寫壞檔。
- **無測試、無 git**：此專案目前不是 git repo，也沒有測試套件。改完後手動跑 mock 流水線驗證即可。
- **修改 LLM 相關**：本專案以 OpenAI 相容端點為主；若要接 Anthropic/Claude，先查 `claude-api` 技能再動手。
