# -*- coding: utf-8 -*-
import os


def data_dir() -> str:
    """多实例数据目录（2026-09-14）。

    一份代码可同时跑多个账号实例：用环境变量 CK_DATA_DIR 把
    cookies / 题库缓存 / 学习库 / 界面配置分开存，账号之间互不可见。
    不设该变量时 = 项目根目录（原行为，单实例用户完全无感知）。
    """
    d = (os.environ.get("CK_DATA_DIR")
         or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # 手动指定了一个还没建的目录时，直接写文件会报错，这里兜一下
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


class GlobalConst:
    AESKey = "u2oh6Vu^HWe4_AES"
    COOKIES_PATH = os.path.join(data_dir(), "cookies.txt")

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="118", "Google Chrome";v="118", "Not=A?Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"'
    }
    VIDEO_HEADERS = {
        "Referer": "https://mooc1.chaoxing.com/ananas/modules/video/index.html?v=2025-0725-1842",
    }
    AUDIO_HEADERS = {
        "Referer": "https://mooc1.chaoxing.com/ananas/modules/audio/index_new.html?v=2025-0725-1842",
    }

    THRESHOLD = 1
