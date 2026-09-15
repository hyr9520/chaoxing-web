try:
    from requests.exceptions import JSONDecodeError
except ImportError:
    from json import JSONDecodeError


class LoginError(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class InputFormatError(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class MaxRollBackExceeded(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class MaxRetryExceeded(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class FontDecodeError(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class PauseInterrupt(Exception):
    """暂停信号：由网页端注入到工作线程，用于中断正在进行的任务。

    放在公共模块是因为它需要跨越两层线程被识别：
      - 外层 _work 线程（web_ui 里跑 JobProcessor.run 的那个）
      - 内层 JobProcessor 的 worker 线程（真正执行 process_chapter 的）
    两者必须用同一个类对象，否则 worker 的异常兜底会把暂停当成任务失败
    （except BaseException 会连它一起吞掉），表现为"点了暂停其实还在刷课"。
    """


class RiskControlError(Exception):
    """风控熔断：平台连续返回 403 / 验证码拦截，判定账号已被风控盯上。

    为什么必须熔断而不是继续重试：视频上报的 403 是**平台级风控信号**，
    本地重试（换会话、刷新令牌、换 rt）不但救不回来，还会让风控计数继续累加
    直至封号。实测的坏情况是"上报 403 → 自动刷验证码失败 → 刷新会话 →
    再上报 403"的循环，跨章节重试会把这个循环放大十几倍。

    熔断后整个任务立即停止（不是跳过当前视频继续下一个），并由上层关闭
    自动恢复，避免重启后马上再次撞上去。
    """
