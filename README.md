# 小說轉影片 · 流水線控制台（Novel → Reel）

![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![diffusers](https://img.shields.io/badge/SD-diffusers-FFD21E?logo=huggingface&logoColor=black)
![last commit](https://img.shields.io/github/last-commit/lanner0403/novel2video)
![repo size](https://img.shields.io/github/repo-size/lanner0403/novel2video)

把小說自動轉成短影片的流水線。參考 [`tyxben/AI_novel`](https://github.com/tyxben/AI_novel) 的分階段 +
斷點續傳設計，但改成你要的**本地化路線**：首幀用**本地 diffusers（Stable Diffusion）**、影片用**本地 ComfyUI**。

**專案 → 多個獨立章節**：建立專案後可手動逐章新增小說，每章一個獨立工作區（資料夾分開），
但**角色卡跨章共用**（列出並可勾選，未勾選的角色才重新生成）；產角色卡時會同步用 SD 生成**角色立繪**可預覽。

七個階段（皆以**章節**為單位執行），**可整條跑、可單獨跑某一步、也可從任一階段往後續跑**：

| # | 階段 | 產物 | 後端服務 |
|---|------|------|----------|
| 1 | 讀取小說 | `segments.json`（分段） | — |
| 2 | 角色卡產生 | `characters.json` + `characters/*.png` 立繪（**專案層級共用**） | LLM + SD |
| 3 | 分鏡產生 | `storyboard.json`（首幀 prompt、ComfyUI 動作/鏡頭/轉場、旁白/對話） | LLM |
| 4 | SD 生成首幀 | `frames/*.png` | diffusers（本地 SD） |
| 5 | ComfyUI 生成影片 | `clips/*.mp4` | ComfyUI（圖生影） |
| 6 | 字幕加載 | `subtitles/full.srt` | — |
| 7 | 影片合成 | `output/final.mp4` | ffmpeg |

---

## 快速開始（離線即可跑）

預設三個外部服務都是 **mock 模式**，不需任何 API key、GPU、SD、ComfyUI，
就能把整條流水線跑通並產出一支帶字幕的成片（SD 首幀用佔位圖、ComfyUI 用 ffmpeg 推鏡代替）。
適合先驗證流程與前端，再逐步換上真實服務。

```bash
# 需求：Python 3.10+、ffmpeg
./run.sh
# 開啟 http://127.0.0.1:8000
```

或手動：

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

操作：左側「＋ 新增專案」→ 上方「＋ 新增章節」逐章貼上內文 → 選一章按「▶ 本章全部執行」，
或對單一階段按「只跑這步 / 從這裡往後」。角色卡分頁可預覽立繪、勾選沿用/重生。範例小說放在 `samples/`。

---

## 切換成真實本地服務

複製 `.env.example` 為 `.env`，把對應 `*_MOCK` 改為 `false` 並填好位址：

```bash
# LLM（角色卡 / 分鏡）— 任何 OpenAI 相容端點（DeepSeek / OpenAI / Ollama）
N2V_LLM_MOCK=false
N2V_LLM_BASE_URL=https://api.deepseek.com/v1
N2V_LLM_API_KEY=sk-xxx

# 本地 Stable Diffusion（HuggingFace diffusers，直接在本機推理）
# 需先安裝較重的相依：pip install -r backend/requirements-diffusers.txt
N2V_SD_MOCK=false
N2V_SD_MODEL=stabilityai/stable-diffusion-2-1
N2V_SD_DEVICE=auto      # auto / cuda / mps / cpu

# 本地 ComfyUI
N2V_COMFY_MOCK=false
N2V_COMFY_BASE_URL=http://127.0.0.1:8188
N2V_COMFY_WORKFLOW=svd_i2v.json
```

> SD 出圖已改用 **HuggingFace diffusers**（不再走 A1111 WebUI）。模型懶加載、跨階段共用同一個
> pipeline。第一次出圖會自動下載模型（數 GB），請確保網路或先把模型放進 HuggingFace 快取。

### ComfyUI workflow 模板

把你在 ComfyUI 用 **Save (API Format)** 匯出的 workflow 放進 `backend/workflows/`，
並把其中兩個值改成佔位字串，後端會在執行時自動替換：

- 載入圖片節點（LoadImage）的 `image` → `%IMAGE%`
- 正向提示詞節點（CLIPTextEncode）的 `text` → `%PROMPT%`

`backend/workflows/svd_i2v.json` 是一個 SVD 圖生影的示意模板，可直接改成 Wan2.1 / AnimateDiff 等。

> 三個服務各自獨立：可以只把 LLM 換成真實、SD/ComfyUI 仍用 mock，反之亦然，方便分段除錯。

---

## 流水線的「可獨立 / 從某點開始」是怎麼做到的

每個階段都把輸入/輸出**落地存檔**到該章節資料夾，並在章節 `state.json` 記錄各階段狀態。
因此：

- **單獨執行某階段**：只要它需要的上游 artifact 已存在即可（缺檔會明確提示先跑哪一步）。
- **從某階段往後**：例如改了分鏡，從第 3 步重跑到第 7 步。
- **斷點續傳**：第 4、5 步會跳過已存在的 `frames/clips`，只補沒做的鏡頭。
- **章節獨立**：各章 artifact 互不干擾；角色卡則是專案層級共用，跨章沿用同一套角色與立繪。

API（以章節為單位）：

```
POST /api/projects/{pid}/chapters/{cid}/run
  {}                                      # 全部跑
  {"only": "storyboard"}                  # 只跑分鏡
  {"start": "sd_first_frame"}             # 從 SD 首幀一路跑到合成
  {"only": "character_cards",
   "options": {"regenerate": ["林楓"]}}   # 重生指定角色的卡與立繪
```

---

## 架構

```
novel2video/
├─ backend/
│  ├─ app/
│  │  ├─ main.py              # FastAPI 路由（專案/章節/角色）+ 靜態前端
│  │  ├─ config.py            # 設定（環境變數覆寫）
│  │  ├─ clients/             # 三個外部服務客戶端（皆含 mock）
│  │  │  ├─ llm_client.py     #   LLM（OpenAI 相容）
│  │  │  ├─ sd_client.py      #   本地 diffusers txt2img（pipeline 模組層級快取）
│  │  │  └─ comfyui_client.py #   ComfyUI 圖生影
│  │  ├─ pipeline/
│  │  │  ├─ project.py        # Project（專案+共用角色池）、Chapter（每章獨立工作區）
│  │  │  ├─ orchestrator.py   # 調度（單一 / 從某點 / 全部，以 pid:cid 為單位）
│  │  │  ├─ stages_text.py    # 階段 1-3（含角色池合併 + 立繪）
│  │  │  └─ stages_media.py   # 階段 4-7
│  │  └─ utils/text.py        # 分段 / 對白 / 角色名擷取
│  ├─ workflows/svd_i2v.json  # ComfyUI workflow 模板
│  ├─ requirements.txt        # 核心相依（mock 模式即可跑）
│  └─ requirements-diffusers.txt  # 本地 SD 真實出圖才需要（torch 等，較重）
├─ frontend/index.html        # 單檔控制台（章節 + 角色立繪 + 膠卷流水線 UI）
├─ samples/                   # 範例小說
├─ .env.example
└─ run.sh
```

前端是單一 HTML 檔（原生 JS），由後端一起服務，無需建置步驟。

---

## 注意

- 字幕目前用旁白/對話長度估時；要更精準可接 TTS 取得逐字時間軸（參考來源專案的 `edge-tts` 作法）。
- mock 模式的角色名為離線啟發式推測；真實 LLM 模式會正確擷取。
