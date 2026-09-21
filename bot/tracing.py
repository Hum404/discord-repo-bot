"""文件溯源码注入与提取。

下载时在文件副本中写入「下载者 ID + 时间」标记，每个下载者的副本都不同：
- ZIP 系（zip/apk/jar/docx/xlsx/pptx/epub 等）：写入 ZIP comment
  （不影响使用，不破坏 APK v2 签名，解压重打包会丢失）
- 文本类（txt/md/json/py 等）：末尾追加零宽字符编码（肉眼不可见）
- 图片类（png/jpg/webp/bmp）：右下角半透明水印（需 Pillow，威慑力最强）

标记 payload 格式：TRC|<用户ID>|<unix时间戳>
"""
from __future__ import annotations

import io
import logging
import re
import time
import zipfile

log = logging.getLogger("repo-bot")

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL = True
except ImportError:  # 未安装 Pillow 时跳过图片水印
    _PIL = False

TRACE_PREFIX = "TRC|"
_TRACE_RE = re.compile(r"TRC\|(\d+)\|(\d+)")

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
TEXT_EXTS = {
    ".txt", ".md", ".json", ".py", ".js", ".ts", ".html", ".htm", ".css",
    ".csv", ".log", ".xml", ".yml", ".yaml", ".ini", ".cfg", ".toml",
    ".c", ".h", ".cpp", ".java", ".go", ".rs", ".sh", ".sql", ".bat", ".ps1",
}

# 零宽字符编码表
_ZW0 = "\u200b"  # 零宽空格 = bit 0
_ZW1 = "\u200c"  # 零宽不连字 = bit 1
_ZW_DELIM = "\u200d"  # 零宽连字 = 起止标记


def make_payload(user_id: int, ts: int) -> str:
    return f"{TRACE_PREFIX}{user_id}|{ts}"


# ───────────────────── 零宽字符编解码 ─────────────────────


def _zw_encode(payload: str) -> str:
    bits = "".join(f"{byte:08b}" for byte in payload.encode("utf-8"))
    return _ZW_DELIM + "".join(_ZW1 if b == "1" else _ZW0 for b in bits) + _ZW_DELIM


def _zw_decode(text: str) -> str | None:
    pattern = (
        re.escape(_ZW_DELIM)
        + "([" + re.escape(_ZW0) + re.escape(_ZW1) + "]+)"
        + re.escape(_ZW_DELIM)
    )
    m = re.search(pattern, text)
    if not m:
        return None
    bits = "".join("1" if ch == _ZW1 else "0" for ch in m.group(1))
    if len(bits) % 8:
        return None
    raw = bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8))
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ───────────────────── ZIP comment ─────────────────────


def _inject_zip(data: bytes, payload: str, user_name: str, ts: int) -> bytes | None:
    try:
        bio = io.BytesIO(data)
        with zipfile.ZipFile(bio, "a") as zf:
            human = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            zf.comment = f"【溯源码】{payload} | 下载者: {user_name} | {human}".encode("utf-8")
        return bio.getvalue()
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


def _extract_zip(data: bytes) -> tuple[str, str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            comment = zf.comment.decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    m = _TRACE_RE.search(comment)
    return (m.group(1), m.group(2)) if m else None


# ───────────────────── 图片水印 ─────────────────────


def _load_font(size: int):
    for path in (
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow ≥ 10.1
    except TypeError:
        return ImageFont.load_default()


def _inject_image(data: bytes, user_id: int, user_name: str, ts: int) -> bytes | None:
    if not _PIL:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if getattr(img, "is_animated", False):  # 动图跳过，避免破坏动画
            return None
        fmt = img.format or "PNG"
        img = img.convert("RGBA")
        w, h = img.size
        if w < 80 or h < 80:
            return None

        date = time.strftime("%m-%d %H:%M", time.localtime(ts))
        # 系统字体不一定支持中文，用户名含非 ASCII 时只打水印 ID
        name_part = f"{user_name} " if user_name.isascii() else ""
        text = f"{name_part}ID:{user_id} {date}"

        size = max(14, min(w, h) // 32)
        font = _load_font(size)
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margin = max(6, min(w, h) // 60)
        x, y = w - tw - margin, h - th - margin
        # 黑色描边 + 半透明白字，深浅背景都可读
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0, 160))
        draw.text((x, y), text, font=font, fill=(255, 255, 255, 190))
        out_img = Image.alpha_composite(img, overlay)

        bio = io.BytesIO()
        if fmt in ("JPEG", "JPG", "BMP"):
            out_img.convert("RGB").save(bio, format=fmt, quality=92)
        else:
            out_img.save(bio, format=fmt)
        return bio.getvalue()
    except Exception:
        log.exception("图片水印注入失败")
        return None


# ───────────────────── 对外接口 ─────────────────────


def inject_trace(
    data: bytes,
    filename: str,
    user_id: int,
    user_name: str,
    content_type: str | None = None,
) -> tuple[bytes, bool]:
    """向文件副本注入溯源标记。返回 (处理后的字节, 是否成功注入)。"""
    ts = int(time.time())
    payload = make_payload(user_id, ts)
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

    # ZIP 系：按魔数识别（apk/docx 等本质是 zip）
    if data[:4] in ZIP_MAGICS:
        out = _inject_zip(data, payload, user_name, ts)
        if out is not None:
            return out, True

    # 图片：右下角可见水印
    if _PIL and (ext in IMAGE_EXTS or (content_type or "").startswith("image/")):
        out = _inject_image(data, user_id, user_name, ts)
        if out is not None:
            return out, True

    # 文本：末尾追加零宽编码（按扩展名/类型门禁，避免污染二进制）
    if ext in TEXT_EXTS or (content_type or "").startswith("text/"):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data, False
        if len(text) > 2_000_000:
            return data, False
        return (text + "\n" + _zw_encode(payload)).encode("utf-8"), True

    return data, False


def extract_trace(data: bytes, filename: str):
    """从文件中提取溯源标记。

    返回 ("hit", user_id, timestamp) / ("image",) / ("none",)。
    """
    if data[:4] in ZIP_MAGICS:
        got = _extract_zip(data)
        if got:
            return ("hit", got[0], got[1])

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext in TEXT_EXTS or data[:4] not in ZIP_MAGICS:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if text:
            payload = _zw_decode(text)
            if payload:
                m = _TRACE_RE.search(payload)
                if m:
                    return ("hit", m.group(1), m.group(2))

    if ext in IMAGE_EXTS:
        return ("image",)
    return ("none",)
