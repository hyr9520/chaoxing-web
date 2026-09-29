"""
Captcha API for Chaoxing

本模块用于通过CX验证码，提供包括验证码获取、识别、验证等接口。
使用了开源的验证码识别库[DdddOcr](https://github.com/sml2h3/ddddocr)

Author: skreon
Email: 1340554713@qq.com
Date: 2025-06-05
Version: 1.0.0
"""

__author__ = "skreon 1340554713@qq.com"
__version__ = "1.0.0"

from random import randint
from typing import Optional

from loguru import logger
from requests import session

try:
    from ddddocr import DdddOcr

    HAS_DDDDOCR = True
except ImportError:
    DdddOcr = None
    HAS_DDDDOCR = False


def ocr_init() -> Optional[DdddOcr]:
    """
    初始化OCR对象

    Returns: DdddOcr对象
    """
    if not HAS_DDDDOCR:
        logger.warning("未检测到 ddddocr 依赖，自动验证码识别将不可用。如遇403限制请在浏览器端手动完成验证。")
        return None
    try:
        return DdddOcr(show_ad=False)
    except Exception as e:
        logger.warning(f"ddddocr 初始化失败: {e}，自动验证码识别将不可用")
        return None


_MISSING = object()


class CxCaptcha:
    """
    CxCaptcha 类用于处理学习任务中出现的验证码

    该类提供了获取、识别和提交验证码的方法，使用 requests 库进行 HTTP 请求，
    并利用 DdddOcr 进行验证码识别。

    Attributes:
        host (str): 超星平台的主机地址。
        api (dict): 包含获取和提交验证码的 API 路径。
        user_agent (str): 用户代理字符串。
        cookies (str): 会话 cookies。
        s (requests.Session): 用于管理会话的请求对象。
    """

    host = 'https://mooc1.chaoxing.com'
    api = {
        'get': '/processVerifyPng.ac',
        'submit': '/html/processVerify.ac'
    }

    def __init__(self, user_agent: str, cookies: str, ocr: Optional[DdddOcr] = _MISSING,
                 limiter=None):
        """
        初始化 CxCaptcha 实例。

        Args:
            user_agent (str): 用户代理字符串。
            cookies (str): 会话 cookies。
            ocr (DdddOcr, optional): 已初始化的 DdddOcr 对象。如果未提供则自动初始化；如果显式传入 None 则禁用 OCR。据DdddOcr官方说明，每次初始化和初始化后的首次识别速度都非常慢，所以推荐传入一个现成的DdddOcr对象实现复用。
            limiter (RateLimiter, optional): 限流器（2026-09-24 加）。**强烈建议传入**：
                验证码流程内部会发 3 个 HTTP 请求（getCaptcha + submitCaptcha，
                加上重试共 9 个），原来这些请求**完全不受限流约束**，
                实测 4.7 秒内打出 9 个请求 —— 这正是账号被风控的放大器之一。
        """

        self.user_agent = user_agent
        self.cookies = cookies
        self.s = session()
        self.s.headers.update({
            'User-Agent': self.user_agent,
            'Cookie': self.cookies,
            'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8'
        })
        self.s.verify = False
        self.limiter = limiter

        self.ocr = ocr_init() if ocr is _MISSING else ocr

    def _throttle(self):
        """验证码流程内的请求限流（2026-09-24）。

        验证码被拦下来说明平台已经在盯这个账号，此刻**更应该放慢**而不是猛打。
        限流器由调用方注入（与视频上报共用同一个），保证"上报+验证码"整体
        受同一个速率约束，而不是各自为政。
        """
        if self.limiter is not None:
            self.limiter.limit_rate(random_time=True, random_max=1.5)

    def getCaptcha(self) -> Optional[bytes]:
        """
        获取验证码图片。

        Returns:
            Optional[bytes]: 返回验证码图片的二进制数据，如果获取失败则返回 None。
        """
        api = self.host + self.api['get']
        random_t = randint(0, 2147483647)

        self._throttle()
        res = self.s.get(api, params={'t': random_t})
        if res.status_code == 200 and res.headers['Content-Type'] == 'image/png':
            return res.content
        else:
            # 提供的Cookies或UA存在问题，导致未能正常获取验证码内容
            return None

    def submitCaptcha(self, cap_token: str) -> bool:
        """
        提交验证码以完成验证。

        Args:
            cap_token (str): 验证码 token。

        Returns:
            bool: 如果提交成功并重定向，则返回 True；否则返回 False。
        """
        api = self.host + self.api['submit']
        params = {
            'ucode': cap_token,
            'app': 0
        }
        self._throttle()
        res = self.s.get(api, params=params)
        if res.status_code == 302:
            return True
        else:
            return False

    def recognition(self, img: bytes) -> str:
        """
        使用 DdddOcr 对验证码图片进行识别。

        Args:
            img (bytes): 验证码图片的二进制数据.

        Returns:
            str: 返回识别出的验证码字符串。
        """
        if not self.ocr:
            logger.error("ddddocr 实例未成功初始化，无法执行验证码识别")
            raise RuntimeError("ddddocr is not available")
        res = self.ocr.classification(img)
        return res

    def try_pass(self) -> bool:
        """
        尝试通过验证码验证流程。

        该方法会自动获取验证码、识别并提交。

        Returns:
            bool: 如果验证码成功通过验证，则返回 True；否则返回 False。
        """
        try:
            cap_img = self.getCaptcha()
            if not cap_img:
                return False
            cap_token = self.recognition(cap_img)
            return self.submitCaptcha(cap_token)
        except Exception as e:
            logger.error(f"验证码自动验证时发生异常: {e}")
            return False
