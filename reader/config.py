from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields, field
from pathlib import Path


def app_root() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def data_root() -> Path:
    root = Path(os.environ.get("READ_TRANSLATE_DATA", str(app_root() / "data")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class Settings:
    api_url: str = "https://api.openai.com/v1"
    model: str = ""
    target_language: str = "简体中文"
    json_mode: bool = False
    max_requests: int = 100
    batch_characters: int = 2400
    input_price: float = 0.0
    output_price: float = 0.0
    budget: float = 0.0
    novel_font_size: int = 20
    reading_direction: str = "rtl"
    fit_mode: str = "width"
    comic_read_mode: str = "single"
    comic_wheel_paging: bool = True
    comic_auto_interval: float = 10.0
    comic_auto_scroll_speed: float = 60.0
    theme: str = "dark"
    background_color: str = "#101722"
    geometry: str = ""
    frameless: bool = False
    topmost: bool = False
    font_path: str = "builtin"
    glossary: str = ""
    text_replace_enabled: bool = False
    text_replace_case_sensitive: bool = True
    text_replace_rules: list[dict] = field(default_factory=list)
    gpu: bool = True
    comic_scope: str = "dialogue"
    comic_layout: str = "auto"
    comic_style: str = "reference"
    comic_cleanup_strength: str = "standard"
    comic_engine: str = "classic"
    comic_ocr: str = "48px"
    upscale_engine: str = "realcugan"
    upscale_ratio: int = 2
    upscale_mode: str = "only"
    upscale_tile: int = 256
    mtu_root: str = ""
    color_engine: str = "klein"
    color_comfy_url: str = "http://127.0.0.1:8188"
    color_klein_model: str = "flux-2-klein-4b-fp8.safetensors"
    color_klein_encoder: str = "qwen_3_4b.safetensors"
    color_klein_vae: str = "flux2-vae.safetensors"
    color_nano_url: str = "https://generativelanguage.googleapis.com/v1beta"
    color_nano_model: str = "gemini-3.1-flash-image"
    color_nano_price: float = 0.0
    color_openai_url: str = "https://api.openai.com/v1"
    color_openai_model: str = "gpt-image-1"
    color_openai_quality: str = "medium"
    color_openai_price: float = 0.0
    color_max_side: int = 1024
    color_denoise_sigma: int = -1
    color_openai_interface: str = 'auto'
    color_network: str = 'direct'
    color_timeout: int = 600
    color_square: bool = True
    color_prompt: str = ''
    color_history_pages: int = 0
    color_openai_size: str = 'auto'
    color_nano_size: str = 'auto'
    color_klein_size: str = 'auto'

    def __post_init__(self):
        # Compatibility for old settings and resumable jobs; the retired path
        # must never run again.
        if self.upscale_mode == 'after':
            self.upscale_mode = 'before'

    @classmethod
    def load(cls, root: Path | None = None) -> "Settings":
        path = (root or data_root()) / "settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{key: value for key, value in raw.items() if key in {field.name for field in fields(cls)}})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, root: Path | None = None) -> None:
        write_json((root or data_root()) / "settings.json", asdict(self))


_session_key = ""


def get_api_key() -> str:
    if _session_key:
        return _session_key
    if os.environ.get("READ_TRANSLATE_API_KEY"):
        return os.environ["READ_TRANSLATE_API_KEY"]
    try:
        import keyring
        return keyring.get_password("ReadTranslate", "cloud-translation") or ""
    except Exception:
        return ""


def set_api_key(key: str, persist: bool = False) -> None:
    global _session_key
    if persist:
        import keyring
        if key:
            keyring.set_password("ReadTranslate", "cloud-translation", key)
        elif keyring.get_password("ReadTranslate", "cloud-translation"):
            keyring.delete_password("ReadTranslate", "cloud-translation")
    _session_key = key


_color_session_key = ""


def get_color_api_key() -> str:
    if _color_session_key:
        return _color_session_key
    if os.environ.get('READ_TRANSLATE_COLOR_API_KEY'):
        return os.environ['READ_TRANSLATE_COLOR_API_KEY']
    try:
        import keyring
        return keyring.get_password('ReadTranslate', 'cloud-colorization') or ''
    except Exception:
        return ''


def set_color_api_key(key: str, persist: bool = False) -> None:
    global _color_session_key
    if persist:
        import keyring
        if key:
            keyring.set_password('ReadTranslate', 'cloud-colorization', key)
        elif keyring.get_password('ReadTranslate', 'cloud-colorization'):
            keyring.delete_password('ReadTranslate', 'cloud-colorization')
    _color_session_key = key


_openai_color_session_key = ""


def get_openai_color_api_key() -> str:
    if _openai_color_session_key:return _openai_color_session_key
    if os.environ.get('READ_TRANSLATE_OPENAI_COLOR_API_KEY'):
        return os.environ['READ_TRANSLATE_OPENAI_COLOR_API_KEY']
    try:
        import keyring
        return keyring.get_password('ReadTranslate','openai-colorization') or ''
    except Exception:return ''


def set_openai_color_api_key(key: str, persist: bool = False) -> None:
    global _openai_color_session_key
    if persist:
        import keyring
        if key:keyring.set_password('ReadTranslate','openai-colorization',key)
        elif keyring.get_password('ReadTranslate','openai-colorization'):
            keyring.delete_password('ReadTranslate','openai-colorization')
    _openai_color_session_key=key
