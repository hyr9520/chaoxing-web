# -*- coding: utf-8 -*-
"""图片支持模块的功能测试（离线，不联网）。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api.image_support import extract_image_urls, build_image_data_urls

# 1. URL 提取
t1 = '【听力题】<img src="https://p.ananas.chaoxing.com/img1.png">Cet-4-3.1.mp3'
u = extract_image_urls(t1)
assert u == ["https://p.ananas.chaoxing.com/img1.png"], u
print("[PASS] 单图提取:", u)

# 2. 多图 + 去重 + 保序
t2 = '<img src="https://a.com/a.png"><img src="https://a.com/b.jpg"><img src="https://a.com/a.png">'
u2 = extract_image_urls(t2)
assert u2 == ["https://a.com/a.png", "https://a.com/b.jpg"], u2
print("[PASS] 多图去重保序:", u2)

# 2b. 相对路径（无法直接下载）应被过滤
assert extract_image_urls('<img src="a.png">') == []
print("[PASS] 相对路径过滤")

# 3. 非 http/data 协议被过滤
t3 = '<img src="javascript:alert(1)"><img src="file:///c:/x.png">'
assert extract_image_urls(t3) == [], extract_image_urls(t3)
print("[PASS] 非法协议过滤")

# 4. 无图 → 空
assert extract_image_urls("纯文字题目") == []
print("[PASS] 无图返回空")

# 5. data URL 直通
d = "data:image/png;base64,iVBORw0KGgo="
imgs, stripped = build_image_data_urls(None, f'<img src="{d}">题干文字')
assert imgs == [d], imgs
assert stripped == "题干文字", repr(stripped)
print("[PASS] data URL 直通 + 题干去标签:", repr(stripped))

# 6. session=None 时外部 URL 静默跳过
imgs2, stripped2 = build_image_data_urls(None, '<img src="https://x.com/a.png">余下文字')
assert imgs2 == [], imgs2
assert stripped2 == "余下文字", repr(stripped2)
print("[PASS] 无会话静默降级")

# 7. 无引号 src 也能提取
u3 = extract_image_urls('<img src=https://a.com/x.png alt="">')
assert u3 == ["https://a.com/x.png"], u3
print("[PASS] 无引号 src:", u3)

# 8. 单题图片数上限
many = "".join(f'<img src="https://a.com/{i}.png">' for i in range(10))
assert len(extract_image_urls(many)) == 4, len(extract_image_urls(many))
print("[PASS] 单题图片数上限 4")

print("\n全部通过")
