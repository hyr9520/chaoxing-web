# -*- coding: utf-8 -*-
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api.learned import normalize_title

# 原有行为不变
a = normalize_title('【听力题】<img src="https://x.com/v.png">Cet-4-3.1.mp3 Questions 1')
b = normalize_title('【听力题】 Cet-4-3.1.mp3 Questions 1')
assert a == b, (a, b)
print("[PASS] 原听力题键仍归一:", repr(a))

# URL 带随机 token 时键仍稳定
c = normalize_title('<img src="https://x.com/a.png?token=AAA111">文字题')
d = normalize_title('<img src="https://x.com/a.png?token=BBB222">文字题')
assert c == d, (c, d)
print("[PASS] URL token 变化不影响键:", repr(c))

# 纯图题不塌成空串
e = normalize_title('<img src="https://x.com/1.png">')
assert e == "<img>", repr(e)
print("[PASS] 纯图题键非空:", repr(e))

# 空输入仍返回空
assert normalize_title("") == ""
assert normalize_title(None) == ""
print("[PASS] 空输入返回空串")
print("\n全部通过")
