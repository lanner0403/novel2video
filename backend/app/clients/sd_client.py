"""本地 Stable Diffusion 客戶端，改用 HuggingFace diffusers 直接在本機推理。

- mock=True（預設）：用 Pillow 畫一張帶 prompt 文字的佔位圖，無需 GPU/模型。
- mock=False：懶加載 diffusers StableDiffusionPipeline 生圖（支援 cuda / mps / cpu）。

模型很大，載入一次要數十秒，因此 pipeline 在 **模組層級快取**，
跨階段（首幀、角色立繪）共用同一個 pipeline，不重複載入。
參考 AI_novel 的 diffusers_backend.py 改寫，去除對該專案內部模組的依賴。
"""

from __future__ import annotations

import base64  # noqa: F401 — 保留給未來可能的 base64 來源
import io
import textwrap
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ..config import settings


# ---------- diffusers pipeline 模組層級快取 ----------
_PIPE: Any = None
_PIPE_KEY: tuple | None = None


def _detect_device(requested: str) -> str:
    """auto 時自動挑選最佳設備：cuda > mps > cpu。"""
    if requested != "auto":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _is_sdxl(cfg) -> bool:
    """判斷該用 SDXL pipeline：pipeline=sdxl 強制，auto 時看 model 名稱是否含 'xl'。"""
    mode = (cfg.pipeline or "auto").lower()
    if mode == "sdxl":
        return True
    if mode == "sd":
        return False
    return "xl" in cfg.model.lower()


def _is_single_file(path: str) -> bool:
    """是否為 A1111/WebUI 風格的單一權重檔（需用 from_single_file 載入，而非 from_pretrained）。"""
    return bool(path) and path.lower().endswith((".safetensors", ".ckpt"))


def _load_vae(AutoencoderKL, vae_id: str, dtype):
    """載入外掛 VAE：單檔走 from_single_file，HF repo / diffusers 目錄走 from_pretrained。"""
    if _is_single_file(vae_id):
        return AutoencoderKL.from_single_file(vae_id, torch_dtype=dtype)
    return AutoencoderKL.from_pretrained(vae_id, torch_dtype=dtype)


def _resolve_vae(cfg, sdxl: bool, dtype) -> str:
    """決定要載入的 VAE：明確指定優先；SDXL+fp16 留空時自動套用 fp16-fix 以免黑圖。"""
    import torch
    if cfg.vae:
        return cfg.vae
    if sdxl and dtype == torch.float16:
        return "madebyollin/sdxl-vae-fp16-fix"
    return ""


def _get_pipe(cfg) -> Any:
    """取得（或建立）快取的 diffusers pipeline（依 model 切換 SD / SDXL，必要時掛 VAE）。"""
    global _PIPE, _PIPE_KEY
    device = _detect_device(cfg.device)
    sdxl = _is_sdxl(cfg)
    # MPS/CPU 上 float16 易出黑圖，統一用 float32 較穩定
    import torch
    dtype = torch.float32 if device in ("cpu", "mps") else torch.float16
    vae_id = _resolve_vae(cfg, sdxl, dtype)
    key = (cfg.model, device, sdxl, vae_id)
    if _PIPE is not None and _PIPE_KEY == key:
        return _PIPE

    from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline, AutoencoderKL

    kwargs: dict = {"torch_dtype": dtype}
    if vae_id:
        kwargs["vae"] = _load_vae(AutoencoderKL, vae_id, dtype)

    Pipe = StableDiffusionXLPipeline if sdxl else StableDiffusionPipeline
    if _is_single_file(cfg.model):
        # A1111/WebUI 風格的單檔 .safetensors/.ckpt → from_single_file（from_pretrained 會找不到 config）
        if not sdxl:
            kwargs["load_safety_checker"] = False
        pipe = Pipe.from_single_file(cfg.model, **kwargs)
    else:
        if not sdxl:
            # SDXL pipeline 不接受 safety_checker 參數
            kwargs.update(safety_checker=None, requires_safety_checker=False)
        pipe = Pipe.from_pretrained(cfg.model, **kwargs)

    # 低顯存：model cpu offload 會自行管理設備搬移，故與 .to(device) 互斥，僅 cuda 有意義
    if getattr(cfg, "cpu_offload", False) and device == "cuda":
        pipe.enable_model_cpu_offload()
    else:
        pipe = pipe.to(device)

    pipe.enable_attention_slicing()
    if device == "cuda":
        try:
            pipe.enable_xformers_memory_efficient_attention()
        except Exception:  # noqa: BLE001 — xformers 非必要
            pass

    _PIPE, _PIPE_KEY = pipe, key
    return pipe


class SDClient:
    def __init__(self) -> None:
        self.cfg = settings.sd
        self.mock = self.cfg.mock
        self.device = _detect_device(self.cfg.device) if not self.mock else "mock"

    def txt2img(self, positive: str, negative: str, out_path: Path,
                seed: int | None = None) -> Path:
        """生成一張圖。seed 用於固定角色/鏡頭以降低成像偏移（mock 模式忽略）。"""
        if self.mock:
            return self._mock(positive, out_path)
        return self._diffusers(positive, negative, out_path, seed)

    # ---- diffusers 本地推理 ----
    def _diffusers(self, positive: str, negative: str, out_path: Path,
                   seed: int | None) -> Path:
        import torch

        pipe = _get_pipe(self.cfg)
        use_seed = seed if seed is not None else self.cfg.seed
        generator = None
        if use_seed is not None:
            generator = torch.Generator(device=self.device).manual_seed(int(use_seed))

        with torch.no_grad():
            result = pipe(
                prompt=positive,
                negative_prompt=negative or None,
                width=self.cfg.width,
                height=self.cfg.height,
                num_inference_steps=self.cfg.steps,
                guidance_scale=self.cfg.guidance_scale,
                generator=generator,
            )
        img = result.images[0]
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(out_path)
        return out_path

    # ---- 離線佔位圖 ----
    def _mock(self, prompt: str, out_path: Path) -> Path:
        w, h = self.cfg.width, self.cfg.height
        seed = sum(ord(c) for c in prompt)
        bg = ((seed * 37) % 90 + 30, (seed * 17) % 90 + 30, (seed * 53) % 90 + 40)
        img = Image.new("RGB", (w, h), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([20, 20, w - 20, h - 20], outline=(255, 255, 255), width=3)
        d.text((40, 40), "[SD MOCK]", fill=(255, 210, 90))
        y = 90
        for line in textwrap.wrap(prompt[:400], width=42):
            d.text((40, y), line, fill=(230, 230, 230))
            y += 22
        img.save(out_path)
        return out_path
