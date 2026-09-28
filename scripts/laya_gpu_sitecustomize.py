"""Tope de VRAM y atención compatible con Pascal, antes de cargar Laya."""
import os

_fraction = (os.environ.get("LAYA_GPU_MEM_FRACTION") or "").strip()
if _fraction:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.set_per_process_memory_fraction(float(_fraction))
            torch.backends.cuda.enable_flash_sdp(False)
            torch.backends.cuda.enable_mem_efficient_sdp(False)
            torch.backends.cuda.enable_math_sdp(True)
    except Exception:
        pass
