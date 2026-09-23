"""文件溯源码注入与提取。

下载时在文件副本中写入「下载者 ID + 时间」标记，每个下载者的副本都不同：
- ZIP 系（zip/apk/jar/docx/xlsx/pptx/epub 等）：写入 ZIP comment
  （不影响使用，不破坏 APK v2 签名，解压重打包会丢失）
- PNG 角色卡（含 chara tEXt 块，如 SillyTavern 卡）：注入独立 tEXt 元数据块
  （不动像素、不重编码、不丢 chara 数据，导入角色卡软件不受影响）
- JSON / JSON 角色卡：解析后写入 extensions.trc 字段
  （不破坏 JSON 结构，V2 卡写入 data.extensions.trc）
- 文本类（txt/md/py 等）：末尾追加零宽字符编码（肉眼不可见）
- 图片类（png/jpg/webp/bmp，非角色卡）：右下角半透明水印（需 Pillow）

标记 payload 格式：TRC|<用户ID>|<unix时间戳>
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import zipfile
import zlib

log = logging.getLogger("repo-bot")

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL = True
except ImportError:  # 未安装 Pillow 时跳过图片水印
    _PIL = False

TRACE_PREFIX = "TRC|"
_TRACE_RE = re.compile(r"TRC\|(\d+)\|(\d+)")
_TRACE_BYTES_RE = re.compile(rb"TRC\|(\d+)\|(\d+)")

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
PNG_SIG = b"\x89PNG\r\n\x1a\n"
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


# ───────────────────── PNG 角色卡（tEXt 元数据块） ─────────────────────


def _iter_png_chunks(data: bytes):
    """遍历 PNG 块，产出 (类型, 数据, 块起始偏移)。"""
    off = len(PNG_SIG)
    while off + 8 <= len(data):
        length = int.from_bytes(data[off:off + 4], "big")
        ctype = data[off + 4:off + 8]
        cdata = data[off + 8:off + 8 + length]
        if off + 12 + length > len(data):
            break
        yield ctype, cdata, off
        off += 12 + length
        if ctype == b"IEND":
            break


def _png_is_chara_card(data: bytes) -> bool:
    """是否为 PNG 角色卡：存在 keyword 为 chara 的 tEXt/zTXt 块。"""
    if not data.startswith(PNG_SIG):
        return False
    for ctype, cdata, _ in _iter_png_chunks(data):
        if ctype in (b"tEXt", b"zTXt"):
            if cdata.split(b"\x00", 1)[0].strip().lower() == b"chara":
                return True
    return False


def _inject_png_chunk(data: bytes, payload: str) -> bytes | None:
    """在 IEND 前插入 keyword=trc 的 tEXt 块。不重编码图像，保留全部原有块。"""
    iend_type = data.rfind(b"IEND")
    if iend_type < 4:
        return None
    cdata = b"trc\x00" + payload.encode("utf-8")
    chunk = (
        len(cdata).to_bytes(4, "big")
        + b"tEXt"
        + cdata
        + (zlib.crc32(b"tEXt" + cdata) & 0xFFFFFFFF).to_bytes(4, "big")
    )
    return data[:iend_type - 4] + chunk + data[iend_type - 4:]


# ───────────────────── JSON 角色卡 / JSON 文件 ─────────────────────


def _inject_json(text: str, payload: str) -> str | None:
    """向 JSON 对象注入 extensions.trc 字段（V2 卡写入 data.extensions）。"""
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    node = obj.get("data") if isinstance(obj.get("data"), dict) else obj
    ext = node.setdefault("extensions", {})
    if not isinstance(ext, dict):
        return None
    ext["trc"] = payload
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ───────────────────── 图片水印 ─────────────────────


# 超过此像素数跳过水印：避免大图处理数十秒 / 撑爆内存
MAX_WATERMARK_PIXELS = 30_000_000
# 水印输出超过此大小则放弃注入：保证文件始终能送达（Discord 上限约 25MB）
MAX_WATERMARK_OUTPUT = 20 * 1024 * 1024


def _load_font(size: int):
    """加载字体。返回 (字体, 是否已是目标大小)。

    依次尝试常见系统字体 → Pillow 可缩放默认字体（≥10.1，需 freetype）→
    位图默认字体（固定小字号，调用方负责放大）。
    """
    for path in (
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ):
        try:
            return ImageFont.truetype(path, size), True
        except (OSError, IOError):
            continue
    try:
        return ImageFont.load_default(size=size), True
    except (TypeError, ImportError):
        # 老版本 Pillow（无 size 参数）或未编译 freetype
        try:
            return ImageFont.load_default(), False
        except Exception:
            return None, False


def _inject_image(data: bytes, user_id: int, user_name: str, ts: int) -> bytes | None:
    if not _PIL:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        if getattr(img, "is_animated", False):  # 动图跳过，避免破坏动画
            return None
        fmt = (img.format or "").upper()
        if fmt not in {"PNG", "JPEG", "JPG", "BMP", "WEBP", "GIF"}:
            return None
        w, h = img.size
        if w < 80 or h < 80 or w * h > MAX_WATERMARK_PIXELS:
            return None
        img = img.convert("RGBA")

        date = time.strftime("%m-%d %H:%M", time.localtime(ts))
        # 系统字体不一定支持中文，用户名含非 ASCII 时只打水印 ID
        name_part = f"{user_name} " if user_name.isascii() else ""
        text = f"{name_part}ID:{user_id} {date}"
        target = max(14, min(w, h) // 32)

        loaded = _load_font(target)
        if loaded[0] is None:
            return None
        font, _ = loaded

        # 先在独立贴图上绘制文字（含描边），再贴到右下角
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = probe.textbbox((0, 0), text, font=font)
        tw, th = max(bbox[2] - bbox[0], 1), max(bbox[3] - bbox[1], 1)
        pad = max(2, target // 8)
        tile = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
        tdraw = ImageDraw.Draw(tile)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            tdraw.text((pad + dx, pad + dy), text, font=font, fill=(0, 0, 0, 170))
        tdraw.text((pad, pad), text, font=font, fill=(255, 255, 255, 200))
        # 位图默认字体不会随 size 放大：整体放大贴图保证水印可见
        if th < target * 0.6:
            scale = target / th
            tile = tile.resize(
                (max(int(tile.width * scale), 1), max(int(tile.height * scale), 1)),
                Image.LANCZOS,
            )

        margin = max(6, min(w, h) // 60)
        x = max(w - tile.width - margin, 0)
        y = max(h - tile.height - margin, 0)
        img.alpha_composite(tile, (x, y))

        bio = io.BytesIO()
        if fmt in ("JPEG", "JPG"):
            img.convert("RGB").save(bio, format="JPEG", quality=90)
        elif fmt == "BMP":
            img.convert("RGB").save(bio, format="BMP")
        elif fmt == "GIF":
            img.convert("P", palette=Image.ADAPTIVE).save(bio, format="GIF")
        elif fmt == "WEBP":
            img.save(bio, format="WEBP", quality=92)
        else:
            # compress_level=1：重编码提速数倍（默认 6 对大 PNG 极慢）
            img.save(bio, format="PNG", compress_level=1)
        out = bio.getvalue()
        if len(out) > MAX_WATERMARK_OUTPUT:
            log.warning("水印输出过大（%d B），跳过注入以保证送达", len(out))
            return None
        return out
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

    # PNG 角色卡：注入 tEXt 元数据块（不动像素、不打可见水印、不丢 chara 数据）
    if data.startswith(PNG_SIG) and _png_is_chara_card(data):
        out = _inject_png_chunk(data, payload)
        if out is not None:
            return out, True

    # JSON / JSON 角色卡：注入 extensions.trc 字段（保持 JSON 可解析）
    if ext == ".json":
        try:
            out_text = _inject_json(data.decode("utf-8"), payload)
        except UnicodeDecodeError:
            out_text = None
        if out_text is not None:
            return out_text.encode("utf-8"), True

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

    # 明文标记：PNG tEXt 块、JSON 字段、ZIP comment 等都是明文 TRC|id|ts
    m = _TRACE_BYTES_RE.search(data)
    if m:
        return ("hit", m.group(1).decode(), m.group(2).decode())

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
        # PNG 角色卡走元数据块注入而非水印，无标记时提示"未命中"而非"看右下角"
        if data.startswith(PNG_SIG) and _png_is_chara_card(data):
            return ("none",)
        return ("image",)
    return ("none",)
