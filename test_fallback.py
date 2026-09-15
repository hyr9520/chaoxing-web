# -*- coding: utf-8 -*-
"""AI 模型自动降级测试（2026-09-13 新增）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.answer import AI

PASS, FAIL = [], []


def check(name, got, expect):
    ok = got == expect
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f": got={got!r} expect={expect!r}"))


print("=" * 68)
print("[1] 模型降级链构建")
check("内置规则 agnes 3.0 -> 2.5",
      AI._build_model_chain('agnes-3.0-flash', {'endpoint': 'https://apihub.agnes-ai.cn/v1'}),
      ['agnes-3.0-flash', 'agnes-2.5-flash'])
check("非 agnes endpoint 不加备用",
      AI._build_model_chain('gpt-4o', {'endpoint': 'https://api.openai.com/v1'}),
      ['gpt-4o'])
check("显式配置优先（逗号分隔）",
      AI._build_model_chain('m1', {'endpoint': '', 'fallback_models': 'm2, m3'}),
      ['m1', 'm2', 'm3'])
check("显式配置去重",
      AI._build_model_chain('m1', {'endpoint': '', 'fallback_models': 'm1,m2'}),
      ['m1', 'm2'])
check("显式配置支持中文逗号/分号",
      AI._build_model_chain('m1', {'endpoint': '', 'fallback_models': 'm2；m3、m4'}),
      ['m1', 'm2', 'm3', 'm4'])
check("空主模型", AI._build_model_chain('', {'endpoint': ''}), [])


print("\n[2] 致命错误判定（决定要不要降级）")
class NotFoundError(Exception): pass
class PermissionDeniedError(Exception): pass
class AuthenticationError(Exception): pass
class APITimeoutError(Exception): pass
class APIConnectionError(Exception): pass
class RateLimitError(Exception): pass

check("NotFoundError -> 降级", AI._is_model_fatal(NotFoundError("x")), True)
check("PermissionDeniedError -> 降级", AI._is_model_fatal(PermissionDeniedError("x")), True)
check("AuthenticationError -> 降级", AI._is_model_fatal(AuthenticationError("x")), True)
check("超时不降级（换模型无益）", AI._is_model_fatal(APITimeoutError("timeout")), False)
check("网络错误不降级", AI._is_model_fatal(APIConnectionError("conn")), False)
check("限流不降级", AI._is_model_fatal(RateLimitError("429")), False)
check("消息含 model not found -> 降级",
      AI._is_model_fatal(Exception("The model `x` does not exist")), True)
check("消息含余额不足 -> 降级",
      AI._is_model_fatal(Exception("insufficient balance 余额不足")), True)
check("消息含权限不足 -> 降级",
      AI._is_model_fatal(Exception("无权限访问该模型")), True)
check("普通异常不降级",
      AI._is_model_fatal(Exception("unexpected error")), False)


print("\n[3] 切换行为")
ai = AI.__new__(AI)
ai.model = 'agnes-3.0-flash'
ai._model_chain = ['agnes-3.0-flash', 'agnes-2.5-flash']
ai._model_idx = 0
check("首次切换成功", ai._switch_to_fallback(), True)
check("切换后模型变了", ai.model, 'agnes-2.5-flash')
check("已到底再切换失败", ai._switch_to_fallback(), False)
check("到底后模型不变", ai.model, 'agnes-2.5-flash')

ai2 = AI.__new__(AI)
ai2.model = 'solo'
ai2._model_chain = ['solo']
ai2._model_idx = 0
check("无备用档时切换失败", ai2._switch_to_fallback(), False)
check("无备用档不报错且模型不变", ai2.model, 'solo')


print("\n" + "=" * 68)
print(f"结果: 通过 {len(PASS)} / 失败 {len(FAIL)}")
for f in FAIL:
    print(f"  - {f}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
