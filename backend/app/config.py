"""全域設定。所有外部服務位址、模式開關集中於此，可用環境變數覆寫。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# 專案根目錄 (novel2video/)
ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    """在讀取任何設定前，把專案根目錄的 .env 載入 os.environ。
    已存在的真實環境變數優先（不覆寫），所以命令列 export 仍能蓋過 .env。
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        # 去掉行內註解與引號
        val = val.split(" #", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()

DATA_DIR = Path(os.getenv("N2V_DATA_DIR", ROOT / "data"))
WORKFLOW_DIR = ROOT / "backend" / "workflows"


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LLMSettings:
    # OpenAI 相容端點 (DeepSeek / Ollama / OpenAI 皆可)。留空或 mock=True 時走離線假資料。
    base_url: str = os.getenv("N2V_LLM_BASE_URL", "https://api.openai.com/v1")
    api_key: str = os.getenv("N2V_LLM_API_KEY", "")
    model: str = os.getenv("N2V_LLM_MODEL", "gpt-4o-mini")
    # 讀取逾時（秒）。本地 7B 產整章分鏡 JSON 可能很久，預設放寬到 300。
    timeout: float = float(os.getenv("N2V_LLM_TIMEOUT", "300"))
    # 分鏡分批：每批送 N 段給 LLM（降低單次生成過久/逾時與 JSON 失敗）。<=0 表示整章一次送。
    storyboard_batch: int = int(os.getenv("N2V_STORYBOARD_BATCH", "4"))
    # 角色擷取分批：每批送 N 段給 LLM 抽角色再聯集（避免長章節截斷漏掉後段角色）。<=0 整章一次送。
    character_batch: int = int(os.getenv("N2V_CHARACTER_BATCH", "12"))
    mock: bool = _b("N2V_LLM_MOCK", True)


@dataclass
class SDSettings:
    # 本地 Stable Diffusion，改用 HuggingFace diffusers 直接在本機推理（不再走 A1111 WebUI）。
    # mock=True 時用 Pillow 佔位圖；mock=False 時懶加載 diffusers pipeline 生圖。
    model: str = os.getenv("N2V_SD_MODEL", "stabilityai/stable-diffusion-2-1")
    # pipeline 類型：auto（依 model 名稱含 "xl" 自動判斷）/ sd / sdxl
    pipeline: str = os.getenv("N2V_SD_PIPELINE", "auto")
    # 外掛 VAE 模型 id 或本地路徑。SDXL 用 fp16 時必要（原生 VAE 會出黑圖），
    # 留空且為 SDXL+fp16 時自動套用 madebyollin/sdxl-vae-fp16-fix。
    vae: str = os.getenv("N2V_SD_VAE", "")
    width: int = int(os.getenv("N2V_SD_WIDTH", "768"))
    height: int = int(os.getenv("N2V_SD_HEIGHT", "1344"))  # 9:16 直式
    steps: int = int(os.getenv("N2V_SD_STEPS", "30"))
    guidance_scale: float = float(os.getenv("N2V_SD_CFG", "7.5"))
    # 運行設備：auto / cuda / mps / cpu
    device: str = os.getenv("N2V_SD_DEVICE", "auto")
    # 低顯存時把模型分層 offload 到 CPU（需 accelerate；僅 cuda 生效，會取代 .to(device)）
    cpu_offload: bool = _b("N2V_SD_CPU_OFFLOAD", False)
    # 全域種子（留空＝隨機）。各角色/鏡頭會再帶自己的種子以求一致性。
    seed: int | None = (int(os.environ["N2V_SD_SEED"]) if os.getenv("N2V_SD_SEED") else None)
    mock: bool = _b("N2V_SD_MOCK", True)


@dataclass
class ComfySettings:
    # 本地 ComfyUI
    base_url: str = os.getenv("N2V_COMFY_BASE_URL", "http://127.0.0.1:8188")
    # 圖生影 workflow 模板 (ComfyUI 匯出的 API 格式 json)
    workflow: str = os.getenv("N2V_COMFY_WORKFLOW", "svd_i2v.json")
    poll_interval: float = float(os.getenv("N2V_COMFY_POLL", "2"))
    poll_timeout: float = float(os.getenv("N2V_COMFY_TIMEOUT", "600"))
    mock: bool = _b("N2V_COMFY_MOCK", True)


@dataclass
class VideoSettings:
    width: int = 1080
    height: int = 1920
    fps: int = 24
    # 每個鏡頭預設秒數 (旁白較長時自動延長)
    default_clip_seconds: float = 4.0


@dataclass
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    sd: SDSettings = field(default_factory=SDSettings)
    comfy: ComfySettings = field(default_factory=ComfySettings)
    video: VideoSettings = field(default_factory=VideoSettings)

    def public_dict(self) -> dict:
        """給前端顯示用，隱去 api_key。"""
        d = asdict(self)
        d["llm"]["api_key"] = "***" if self.llm.api_key else ""
        return d


settings = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)
