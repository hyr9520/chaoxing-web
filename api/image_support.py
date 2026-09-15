# -*- coding: utf-8 -*-
"""题目配图下载与编码（2026-09-13 新增）。

背景：部分题目（尤其作业、简答题）题干里带老师上传的图片 —— 图片里才是
真正的题目内容（电路图、表格、函数图像、化学式等）。decode.py 已经把
<img src="..."> 原样保留进题干，AI 只看到一串 URL，自然答不出来。

本模块负责：把题干里的 <img src> 抓出来 → 带登录 cookies 下载 → 转成
data URL（base64）。视觉模型（agnes-3.0-flash 等）可直接吃 data URL。

设计约束：
- 下载必须带 cookies（超星图片常需登录态；直连会 403 或返回登录页）。
- 图片有体积上限，超限的图直接跳过（避免撑爆请求体 / 触发限流）。
- 失败必须静默降级：下载不到图就当没有图，走原来的纯文本链路，
  绝不因为图片问题让整道题丢失。
"""
import base64
import re
from typing import List, Optional, Tuple

from api.logger import logger

# <img src="http://...png" ...> / <img src='...'> / <img src=...>
# 注意：src 值必须整体匹配到闭合引号（或到空白），否则 data URL / 含 ":" "/"
# 的长地址会被截断，且删标签时会残留引号（实测 '">题干' 这类脏字符）。
_IMG_SRC_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))",
    re.IGNORECASE,
)
# 删标签用（整体吃掉 <img ...>）
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)

# 单图上限 4MB（超星题目配图通常在几百 KB 内；过大的是异常页面/整页截图）
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# 只接受 http(s) 与 data: 开头的地址
_ALLOWED_SCHEME_RE = re.compile(r"^(https?:|data:)", re.IGNORECASE)

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
}

# 一次请求最多处理几张图（题目配图一般 1~2 张，多了是异常）
MAX_IMAGES_PER_QUESTION = 4


def extract_image_urls(text: str) -> List[str]:
    """从题干文本里按出现顺序提取所有图片地址（去重、保序）。"""
    if not text:
        return []
    seen = set()
    urls = []
    for m in _IMG_SRC_RE.finditer(text):
        url = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if not url or url in seen:
            continue
        if not _ALLOWED_SCHEME_RE.match(url):
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= MAX_IMAGES_PER_QUESTION:
            break
    return urls


def _guess_mime(url: str, resp) -> str:
    ctype = ""
    try:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    except Exception:
        ctype = ""
    if ctype.startswith("image/"):
        return ctype
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower() if "." in url else ""
    return _MIME_BY_EXT.get(ext, "image/png")


def _download_one(session, url: str) -> Optional[str]:
    """下载单张图并返回 data URL；失败返回 None（静默降级）。"""
    # 已经是 data URL：直接放行
    if url.lower().startswith("data:"):
        return url
    if session is None:
        return None
    try:
        resp = session.get(url, timeout=20, headers={
            # 带 Referer 降低被防盗链拦截的概率
            "Referer": "https://mooc1.chaoxing.com/",
        })
        if resp.status_code != 200:
            logger.warning(f"题目配图下载失败（HTTP {resp.status_code}）：{url[:120]}")
            return None
        raw = resp.content or b""
        if not raw:
            logger.warning(f"题目配图内容为空：{url[:120]}")
            return None
        if len(raw) > MAX_IMAGE_BYTES:
            logger.warning(
                f"题目配图过大（{len(raw) / 1024 / 1024:.1f}MB > "
                f"{MAX_IMAGE_BYTES / 1024 / 1024:.0f}MB），已跳过：{url[:120]}")
            return None
        mime = _guess_mime(url, resp)
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        logger.warning(f"题目配图下载异常（{e}）：{url[:120]}")
        return None


def build_image_data_urls(session, title: str) -> Tuple[List[str], str]:
    """把题干里的图片全部下载成 data URL。

    Returns:
        (data_urls, stripped_title)
        - data_urls: 可直接塞进多模态消息的 data URL 列表（可能为空）
        - stripped_title: 去掉 <img> 标签后的纯文本题干（视觉模型也要文字上下文）
    """
    urls = extract_image_urls(title)
    stripped = _IMG_TAG_RE.sub("", str(title or "")).strip()
    if not urls:
        return [], stripped
    out = []
    for u in urls:
        d = _download_one(session, u)
        if d:
            out.append(d)
    if out:
        logger.info(f"题目配图已获取 {len(out)} 张，将随题干一起提交给视觉模型")
    return out, stripped
