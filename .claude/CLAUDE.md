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
- **角色擷取分批**：`_extract_characters` 依 `N2V_CHARACTER_BATCH`（預設 12）分批掃完整章內文、
  聯集去重（修掉舊版 `full[:6000]` 截斷會漏後段角色的問題）；`_normalize_card` 補欄位、aliases 強制成 list。
- **分鏡分批**：`run_storyboard` 依 `N2V_STORYBOARD_BATCH`（預設 8）把段落切批分送 LLM，
  `_normalize_shot` 以實際段落為準補齊 id/segment_index 與缺漏欄位（LLM 殘缺/亂序也能一段一鏡頭）。
  單批 LLM 逾時或回傳壞 JSON 時退回啟發式（只降級該批、不讓整步崩）；
  `_loads_loose` 容忍 ```json``` 圍欄、前後雜訊與尾逗號。
- **立繪 seed 一致性**：`sd_first_frame` 從專案池查角色 seed，含該角色的首幀沿用，降低成像偏移。
- **提示詞去重**：CLIP 上限 77 token，立繪/首幀 prompt 用 `_dedupe_prompt`（逗號去重＋截斷、重點在前），
  避免風格詞重複堆疊把角色描述擠掉而被截斷。
- **單項編輯/重生**：角色卡、鏡頭可逐一編輯（PUT）；單一立繪走 `orch.run_task_async`（key `pid:char:slug`），
  單一首幀/片段＝刪該檔後 `run_stages_async(only=…)`，靠階段既有的「跳過已存在」只補這一個。

## API

```
GET  /api/settings                                   # 公開設定（隱去 api_key）+ 階段清單
GET  /api/projects                                   # 專案列表
POST /api/projects                                   # 建立 {name, novel_text?}（給 text 則建第 1 章）
GET  /api/projects/{pid}                             # 專案詳情（章節摘要 + 角色數）
DELETE /api/projects/{pid}                           # 刪除專案（有章節執行中則 409）
POST /api/projects/{pid}/chapters                    # 新增章節 {title, novel_text}
GET  /api/projects/{pid}/chapters/{cid}              # 章節詳情（各階段狀態、檔案可用性）
DELETE /api/projects/{pid}/chapters/{cid}            # 刪除章節（執行中則 409）
PUT  /api/projects/{pid}/chapters/{cid}/shots/{sid}        # 編輯單一鏡頭欄位
POST /api/projects/{pid}/chapters/{cid}/shots/{sid}/frame  # 重生單一首幀（刪檔+跑 sd_first_frame only）
POST /api/projects/{pid}/chapters/{cid}/shots/{sid}/clip   # 重生單一片段（刪檔+跑 comfy_video only）
POST /api/projects/{pid}/chapters/{cid}/novel        # 更新章節內文（text 或 file 上傳）
POST /api/projects/{pid}/chapters/{cid}/run          # {} 全跑 / {only} / {start} / {options:{regenerate}}
GET  /api/projects/{pid}/chapters/{cid}/artifact/{k} # read_novel|storyboard 的 JSON
GET  /api/projects/{pid}/chapters/{cid}/file/{path}  # 章節內檔案（frames/clips/output…，防目錄穿越）
GET  /api/projects/{pid}/chapters/{cid}/logs         # 章節 logs + running（前端輪詢）
GET  /api/projects/{pid}/characters                  # 專案共用角色卡（含 portrait_available/regenerating）
PUT  /api/projects/{pid}/characters/{name}           # 編輯單一角色卡欄位
POST /api/projects/{pid}/characters/{name}/regenerate # 重生單一角色立繪（背景，is_running_key 追蹤）
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

**SD diffusers**：`sd_client.py` 懶加載 pipeline，在**模組層級快取**（`_get_pipe`），跨階段
（首幀、立繪）共用同一個，不重複載入。device `auto` 依序選 cuda > mps > cpu。
- **SD ↔ SDXL 切換**：`_is_sdxl` 依 `N2V_SD_PIPELINE`（auto/sd/sdxl）或 model 名稱含 `xl` 自動選用
  `StableDiffusionPipeline` 或 `StableDiffusionXLPipeline`。換 SDXL 記得把尺寸改成 1024 系。
- **VAE（SDXL 必要）**：`N2V_SD_VAE` 可掛外掛 VAE；`_resolve_vae` 在 SDXL+fp16 且未指定時自動套
  `madebyollin/sdxl-vae-fp16-fix`（原生 SDXL VAE 在 fp16 會出黑圖）。cache key 含 pipeline 型別與 VAE，切換會重載。
- **單檔權重**：`N2V_SD_MODEL` / `N2V_SD_VAE` 可指向 A1111/WebUI 風格的單檔 `.safetensors`/`.ckpt`。
  `_is_single_file` 偵測副檔名，改走 `from_single_file`（`from_pretrained` 只吃 diffusers 目錄，給單檔會報「not a valid JSON」）。
  - `from_single_file` 需向 HF 抓架構 config；某些環境（公司網路/缺中介憑證）SSL 驗不過，`_setup_ssl()` 用
    `truststore` 改走 OS 憑證庫（對應 git 的 schannel 修法，裝在 requirements-diffusers）。
  - SDXL 單檔 VAE 會額外帶 `config=madebyollin/sdxl-vae-fp16-fix`，否則套到 SD1.5 預設、scaling 不對。
- **SDXL 黑圖**：SDXL+fp16 時 VAE 解碼會數值溢位出全黑/NaN 圖。`_get_pipe` 設 `vae.config.force_upcast=True`，
  讓 pipeline 解碼時自動把 VAE 與 latents 一起轉 fp32（單檔 VAE 載入後此旗標預設可能為關，故手動補上）。
- **低顯存**：`N2V_SD_CPU_OFFLOAD=true` 時呼叫 `enable_model_cpu_offload()`（需 accelerate，僅 cuda 生效）；
  它自管設備搬移，故與 `.to(device)` 互斥（程式碼二擇一）。

**ComfyUI workflow 模板**：把 ComfyUI「Save (API Format)」匯出的 json 放進 `backend/workflows/`，
並把要替換的值改成佔位字串，`comfyui_client._real` 執行時自動替換：
- `%IMAGE%`（LoadImage 的 `image`，上傳首幀後換成伺服器檔名）
- `%PROMPT%`（提示詞文字，換成鏡頭動態 `camera, motion`）
- `%DURATION%`（每鏡頭秒數；連同左右引號換成整數，故模板寫 `"value": "%DURATION%"`）
內附兩個模板：`svd_i2v.json`（SVD）、`ltx2_i2v.json`（LTX-2.3 圖生影；用 `N2V_COMFY_WORKFLOW` 切換，
需在 ComfyUI 先裝好對應自訂節點與模型）。輸出檔由 `_find_output` 掃所有節點欄位、優先取影片副檔名。

## 慣例與注意事項

- **語言**：程式碼註解、log、API 訊息、UI 皆用**繁體中文**；SD/ComfyUI 提示詞用**英文**。
- **影片規格**：直式 9:16，預設 1080×1920 @ 24fps；鏡頭秒數依語音長度估算（`_estimate_duration`，3~12s）。
- **comfy_prompt 內容**：含 scene/characters/camera/motion/mood + voice_tone；`stages_media._video_prompt`
  把場景＋人物＋運鏡＋要唸的台詞與語氣組成提示，餵給 LTX 等帶語音的圖生影模型。
- **合成保留音軌**：`_concat` 全部片段都有音軌時才併音軌（`_has_audio` 以 ffprobe 偵測），
  字幕燒錄帶 `-c:a copy`；mock 推鏡無聲則走純影像路徑。LTX 生成的語音因此能保留到成片。
- **分鏡前整合段落**：`_merge_segments` 依 `N2V_STORYBOARD_MERGE_CHARS`（預設 200）把短段落併到約 N 字一鏡頭。
- **mock 限制**：角色名為離線啟發式推測（`utils/text.extract_names`），真實 LLM 模式才會正確擷取。
- **字幕計時**：目前用旁白/對白長度估時，非逐字時間軸；要精準需接 TTS。
- **狀態檔寫入**：`_atomic_write_json` 用 `.tmp` + `replace` 原子寫入，Windows 上 replace 偶發
  WinError 5 會重試。
- **名稱清理**：專案/章節名一律過 `clean_name`（移除 `< > : " / \ | ? *` 與控制字元、壓空白、去頭尾點）。
- **刪除**：`Project.delete` / `remove_chapter` 用 `_rmtree`（先刪 state.json 讓清單立即反映，再重試刪資料夾）。
- **無測試、無 git**：此專案目前不是 git repo，也沒有測試套件。改完後手動跑 mock 流水線驗證即可。
- **修改 LLM 相關**：本專案以 OpenAI 相容端點為主；若要接 Anthropic/Claude，先查 `claude-api` 技能再動手。
