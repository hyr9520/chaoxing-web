# -*- coding: utf-8 -*-
"""超星**滑块**验证码自动通过（手机端 touch 协议）—— 2026-09-27 实测跑通。

⚠️ 与 api/captcha.py（skreon，ddddocr 字符验证码）不同：本模块专治**滑块拼图**。

背景（真机实测结论）：
  · 进入考试前会弹超星自研验证码 SDK（captcha.chaoxing.com/load-i.min.js）
  · SDK 在 `.cx_rightBtn` 上只绑了 `mousedown` + `touchstart`；本页面是 phone 协议，
    **mouse 路线完全无响应（拼图块纹丝不动）**，必须用 **touch 事件**
    （CDP `Input.dispatchTouchEvent`）才 1:1 跟手。
  · 校验接口 `/captcha/v1/check/verification/result` 返回
    `{"error":0,"msg":"ok","result":true/false}`；通过后 SDK 回调 data.validate，
    页面写入 `#captchavalidate`，再由页面拼进 start 请求。
  · 实测位置精度需 ±3px 内，且轨迹要"加速→减速 + 冲过头 + 回拉"（太完美会被拒）。

**不需要任何第三方打码 API。**

依赖：playwright / opencv-python / numpy / requests（实例环境已具备）
"""
from __future__ import annotations

import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import urllib3

urllib3.disable_warnings()

try:
    from loguru import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:  # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
window.chrome = window.chrome || { runtime:{}, loadTimes:()=>{}, csi:()=>{} };
"""


def available() -> bool:
    """运行环境是否具备自动过滑块的能力。"""
    return bool(HAS_PLAYWRIGHT and cv2 is not None)


# ------------------------------------------------------------------ 图像

def _match(scene: np.ndarray, tpl: np.ndarray, mask: np.ndarray) -> int:
    """在 scene 里找 tpl，返回最佳匹配的左边缘 x。"""
    res = cv2.matchTemplate(scene.astype(np.float32), tpl.astype(np.float32),
                            cv2.TM_SQDIFF, mask=mask)
    _, _, mnl, _ = cv2.minMaxLoc(res)
    return int(mnl[0])


def _fetch_img(url: str) -> Optional[np.ndarray]:
    try:
        r = requests.get(url, headers={"User-Agent": UA,
                                       "Referer": "https://mooc1-api.chaoxing.com/"},
                         timeout=20, verify=False)
        if r.status_code != 200 or len(r.content) < 400:
            return None
        return cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_UNCHANGED)
    except Exception:
        return None


def _load_pair(bg_url: str):
    """下载背景图(big) + 拼图块(small)，裁出模板与中心 mask。

    约定：captcha-b.chaoxing.com/slide/big/XXX.jpg ↔ /slide/small/XXX.jpg
    """
    small_url = re.sub(r"/slide/big/", "/slide/small/", bg_url)
    big = _fetch_img(bg_url)
    sm = _fetch_img(small_url)
    if big is None or sm is None:
        return None
    if big.ndim == 2:
        big = cv2.cvtColor(big, cv2.COLOR_GRAY2BGR)
    if sm.ndim == 3 and sm.shape[2] == 4:
        a = sm[:, :, 3]
        ys, xs = np.where(a > 10)
        if len(xs) == 0:
            return None
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        tpl = sm[y0:y1, x0:x1, :3].copy()
        m0 = sm[y0:y1, x0:x1, 3].copy()
    else:
        tpl = sm[:, :, :3].copy() if sm.ndim == 3 else cv2.cvtColor(sm, cv2.COLOR_GRAY2BGR)
        m0 = np.full(tpl.shape[:2], 255, np.uint8)
    hh, ww = m0.shape
    core = np.zeros_like(m0, np.uint8)
    cv2.rectangle(core, (int(ww * .2), int(hh * .2)), (int(ww * .8), int(hh * .8)), 255, -1)
    use_mask = (m0 * (core > 0)).astype(np.uint8)
    return big, tpl, use_mask


# ------------------------------------------------------------------ 轨迹

def _physics_track(distance: float) -> List[float]:
    """物理模型轨迹：前 ~2/3 加速、后 ~1/3 减速（和为 distance）。"""
    tr: List[float] = []
    cur, v = 0.0, 0.0
    mid = distance * random.uniform(0.6, 0.75)
    t = random.uniform(0.13, 0.22)
    guard = 0
    while cur < distance and guard < 300:
        guard += 1
        a = random.uniform(1.6, 2.6) if cur < mid else -random.uniform(2.6, 4.2)
        v0 = v
        v = v0 + a * t
        mv = v0 * t + 0.5 * a * t * t
        if mv <= 0.5:
            mv = random.uniform(0.8, 1.8)
        cur += mv
        tr.append(mv)
    s = sum(tr)
    return [x * distance / s for x in tr] if s > 0 else [distance]


def _cookies_from_header(cookie_header: str) -> List[Dict[str, str]]:
    out = []
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out.append({"name": k.strip(), "value": v.strip(),
                        "domain": ".chaoxing.com", "path": "/"})
    return out


# ------------------------------------------------------------------ 主流程

def solve(cover_url: str, cookie_header: str = "", tries: int = 6,
          headless: bool = True, total_timeout: float = 180.0) -> Dict[str, Any]:
    """打开考试封面页并自动通过滑块，返回票据。

    Args:
        cover_url: 考试封面页 URL（带全部参数）
        cookie_header: "a=1; b=2" 形式的 Cookie（与主程序同一会话）
        tries: 最多尝试次数（每次失败 SDK 自动刷新新图）
        headless: 无头/有头
        total_timeout: 整体超时，避免拖死考试流程

    Returns:
        {"ok": bool, "validate": str, "tries": int, "msg": str}
    """
    if not available():
        return {"ok": False, "validate": "", "tries": 0,
                "msg": "缺少 playwright / opencv-python，无法自动过滑块"}
    if not cover_url:
        return {"ok": False, "validate": "", "tries": 0, "msg": "缺少封面页 URL"}

    t0 = time.time()
    tried = 0
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900}, user_agent=UA,
                locale="zh-CN", timezone_id="Asia/Shanghai", has_touch=True)
            ctx.add_init_script(_STEALTH)
            ck = _cookies_from_header(cookie_header)
            if ck:
                ctx.add_cookies(ck)
            page = ctx.new_page()
            page.goto(cover_url, wait_until="load", timeout=60000)
            time.sleep(2)

            # 主动弹出验证码；回调只设标志位 —— **绝不触发页面自己的 start 跳转**
            page.evaluate("""() => { window.__passed = false; window.__validate = '';
                try { showCXCaptcha(function(){
                        window.__passed = true;
                        var e = document.querySelector('#captchavalidate');
                        window.__validate = e ? (e.value || '') : '';
                    }); } catch (e) { window.__capErr = String(e); } }""")
            time.sleep(5)

            def get_validate() -> str:
                try:
                    r = page.evaluate("""() => ({ p: !!window.__passed,
                        v: ((document.querySelector('#captchavalidate')||{}).value||'') })""")
                except Exception:
                    return ""
                return (r.get("v") or "") if r.get("p") else ""

            v = get_validate()
            if v:
                browser.close()
                return {"ok": True, "validate": v, "tries": 0, "msg": "已带票据"}

            cdp = ctx.new_cdp_session(page)

            def touch(kind: str, pts: List[Dict[str, float]]) -> None:
                cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": pts})

            for i in range(tries):
                if time.time() - t0 > total_timeout:
                    break
                tried = i + 1
                bg_url = page.evaluate("""() => { const bg=document.querySelector('#cx_imgBg');
                    if(!bg) return ''; const s=getComputedStyle(bg).backgroundImage||'';
                    const m=s.match(/url\\(["']?(.*?)["']?\\)/); return m?m[1]:''; }""")
                if not bg_url:
                    time.sleep(1.0)
                    continue
                pair = _load_pair(bg_url)
                if pair is None:
                    time.sleep(0.5)
                    continue
                big, tpl, use_mask = pair
                gx = _match(big, tpl, use_mask)
                shot = cv2.imdecode(np.frombuffer(page.screenshot(), np.uint8), cv2.IMREAD_COLOR)
                px = _match(shot, tpl, use_mask)
                rect = page.evaluate("""() => { const e=document.querySelector('#cx_imgBg');
                    const r=e.getBoundingClientRect(); return {x:r.x,y:r.y}; }""")
                sb = page.evaluate("""() => { const e=document.querySelector('#eject .cx_rightBtn');
                    if(!e) return null; const r=e.getBoundingClientRect();
                    return {x:r.x,y:r.y,w:r.width,h:r.height}; }""")
                if not sb:
                    time.sleep(0.5)
                    continue
                target = rect["x"] + gx
                dx = target - px
                sx, sy = sb["x"] + sb["w"] / 2, sb["y"] + sb["h"] / 2
                logger.info(f"🔐 滑块验证：缺口x={gx} 拼图块x={px} → 拖动 {dx:.0f}px"
                            f"（第 {tried}/{tries} 次）")

                # ① 主拖动（少拖 4%，给微调留空间）
                touch("touchStart", [{"x": sx, "y": sy}])
                time.sleep(random.uniform(0.08, 0.15))
                cur = sx
                for mv in _physics_track(dx * 0.96):
                    cur += mv
                    touch("touchMove", [{"x": cur, "y": sy + random.uniform(-0.6, 0.6)}])
                    time.sleep(random.uniform(0.008, 0.020))
                time.sleep(random.uniform(0.08, 0.16))

                # ② 视觉闭环：量出还差多少（消除触摸与显示的系统偏差）
                shot = cv2.imdecode(np.frombuffer(page.screenshot(), np.uint8), cv2.IMREAD_COLOR)
                now = _match(shot, tpl, use_mask)
                delta = target - now

                # ③ overshoot：故意冲过头（人类特征）
                over = random.uniform(6, 13)
                right = delta + over
                if right > 0:
                    steps = random.randint(3, 6)
                    for _ in range(steps):
                        cur += right / steps
                        touch("touchMove", [{"x": cur, "y": sy + random.uniform(-0.5, 0.5)}])
                        time.sleep(random.uniform(0.014, 0.030))
                # ④ 回拉：回到「目标 + 小误差」——刻意不精确
                err = random.uniform(-3, 3)
                left = over - err
                if left > 0:
                    steps = random.randint(4, 8)
                    for _ in range(steps):
                        cur -= left / steps
                        touch("touchMove", [{"x": cur, "y": sy + random.uniform(-0.5, 0.5)}])
                        time.sleep(random.uniform(0.018, 0.038))
                # ⑤ 微抖动
                for _ in range(random.randint(1, 3)):
                    cur += random.uniform(-1.5, 1.5)
                    touch("touchMove", [{"x": cur, "y": sy + random.uniform(-0.4, 0.4)}])
                    time.sleep(random.uniform(0.012, 0.030))
                time.sleep(random.uniform(0.25, 0.5))
                touch("touchEnd", [])
                time.sleep(3.5)

                v = get_validate()
                if v:
                    logger.info(f"🔐 滑块验证通过（第 {tried} 次尝试）")
                    browser.close()
                    return {"ok": True, "validate": v, "tries": tried, "msg": "ok"}
                time.sleep(1.0)

            browser.close()
    except Exception as e:      # 任何异常都不能拖死考试流程
        logger.warning(f"自动过滑块异常（不影响其他功能）：{type(e).__name__}: {e}")
        return {"ok": False, "validate": "", "tries": tried,
                "msg": f"{type(e).__name__}: {e}"}

    return {"ok": False, "validate": "", "tries": tried,
            "msg": f"{tries} 次尝试均未通过（位置或行为校验未过）"}
