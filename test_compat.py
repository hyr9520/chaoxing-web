# -*- coding: utf-8 -*-
"""兼容性 / 冲突检测（2026-09-13 新增）。

目的：回答"新加的会不会把原来正常的搞坏"。

做法：把**全部 19 个题型码** × **各种答案形态**跑一遍，检查三个层面
是否自相矛盾、是否出现"静默丢弃"：

  1. check_answer       —— 查询阶段：答案会不会被误杀
  2. check_submittable  —— 填写阶段：答案能不能提交
  3. plausible_answer   —— 缓存阶段：能不能进/出缓存

三条链路对同一份答案的判定必须**一致或不矛盾**：
查询阶段放行的答案，填写阶段不该被判为非法（那等于白查一次）。

另外验证向后兼容：非纯图题的缓存键与旧版完全一致（老缓存不失效）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.answer_check import (
    check_answer, check_submittable, plausible_answer, set_option_letters,
)
from api.decode import _get_question_type
from api.learned import normalize_title

PASS, FAIL, WARN = [], [], []


def check(name, got, expect):
    ok = got == expect
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          ("" if ok else f": got={got!r} expect={expect!r}"))


def warn(msg):
    WARN.append(msg)
    print(f"  [WARN] {msg}")


class _FakeTiku:
    """check_answer 需要的最小 tiku 接口。"""
    is_manual = False
    skip_answer_validation = False
    true_list = []
    false_list = []


print("=" * 70)
print("兼容性 / 冲突检测")
print("=" * 70)

# 题型码 → (内部类型, 这个题型"合理的答案"样例)
CASES = {
    "0": ("single", "A"),
    "1": ("multiple", "ABC"),
    "2": ("completion", "光合作用"),
    "3": ("judgement", "对"),
    "4": ("shortanswer", "因为地心引力"),
    "5": ("shortanswer", "牛顿第一定律"),
    "6": ("shortanswer", "论述如下：第一……"),
    "7": ("shortanswer", "x=3.14"),
    "8": ("shortanswer", "其它作答内容"),
    "9": ("shortanswer", "借：银行存款"),
    "10": ("shortanswer", "资料分析结论"),
    "14": ("completion", "first"),
    "18": ("shortanswer", "口语作答内容"),
    "19": ("single", "CADB"),
}

print("\n[1] 每个题型码：合理答案必须在三条链路上**全部放行**（不能静默丢弃）")
for code, (tname, sample) in CASES.items():
    real = _get_question_type(code)
    # 类型名必须与预期一致（防止我把映射写歪）
    if real != tname:
        warn(f"题型码 {code} 实际映射为 {real}，预期 {tname}")
    set_option_letters("ABCD")
    a1 = check_answer(sample, real, _FakeTiku())
    a2 = check_submittable(sample, real)
    a3 = plausible_answer(sample, real)
    check(f"码{code}({real}) 样例 {sample!r}：查询/提交/缓存 均放行",
          (a1, a2, a3), (True, True, True))
set_option_letters(None)

print("\n[2] unknown / 未映射题型：文字答案必须放行（历史事故点）")
for code in ("11", "13", "15", "20", "21", "99"):
    real = _get_question_type(code)
    check(f"码{code} 映射为 unknown", real, "unknown")
    check(f"码{code} 文字答案可提交", check_submittable("排序后的逻辑说明", real), True)
    check(f"码{code} 文字答案可缓存", plausible_answer("排序后的逻辑说明", real), True)
    check(f"码{code} 空答案仍被拦", check_submittable("", real), False)

print("\n[3] 严格性不能被破坏：选择题的垃圾答案仍须拦下")
set_option_letters("ABCD")
check("单选 中文长句 被拦", check_submittable("这是一整句中文", "single"), False)
check("单选 带题号 被拦", check_submittable("22.C 23.A", "single"), False)
check("单选 review 被拦（选项外字母）", check_submittable("review", "single"), False)
check("单选 A 放行", check_submittable("A", "single"), True)
check("单选 CADB 放行（连写）", check_submittable("CADB", "single"), True)
set_option_letters(None)
check("无选项上下文时 8 字母内放行", check_submittable("ABCD", "single"), True)

print("\n[4] 拒答文本在任何题型下都不能进缓存")
for t in ("single", "multiple", "completion", "judgement", "shortanswer", "unknown"):
    check(f"{t} 拒答文本被拦", plausible_answer("抱歉，我无法访问音频文件", t), False)

print("\n[5] 三条链路不矛盾（查询放行 → 提交也必须放行）")
MUST_AGREE = [
    ("A", "single"), ("ABC", "multiple"), ("对", "judgement"),
    ("光合作用", "completion"), ("牛顿第一定律", "shortanswer"),
    ("任意文字", "unknown"),
]
for ans, t in MUST_AGREE:
    set_option_letters("ABCD")
    q = check_answer(ans, t, _FakeTiku())
    s = check_submittable(ans, t)
    set_option_letters(None)
    if q and not s:
        check(f"{t} {ans!r} 查询放行但提交被拦（矛盾！）", False, True)
    else:
        check(f"{t} {ans!r} 两链路一致", True, True)

print("\n[6] 向后兼容：非纯图题的缓存键与旧实现完全一致")
def _old_normalize(title):
    """旧实现（改动前）：只去掉 <img> 标签。"""
    import re
    t = str(title or "")
    t = re.sub(r"<img\b[^>]*>", "", t, flags=re.IGNORECASE)
    t = t.replace("\xa0", " ")
    t = re.sub(r"(【[^】]*】)\s+", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()

OLD_SAMPLES = [
    "【听力题】<img src=\"https://p.ananas.chaoxing.com/v.png\">Cet-4-3.1.mp3 Questions 1 and 2",
    "【听力题】 Cet-4-3.20-Exercise.mp3 Questions 19 to 21",
    "R是____性语言",
    "1.下列说法正确的是（ ）",
    "<img src=\"https://x.com/a.png\">请计算图中所示电路的电流",
    "中国的首都是____。",
    "",
]
for s in OLD_SAMPLES:
    old, new = _old_normalize(s), normalize_title(s)
    check(f"键兼容：{s[:36]!r}…", new, old)

print("\n[7] 题组标记：只有已知题组类型 + 多小问才置位")
from bs4 import BeautifulSoup as _BS
from api.decode import _process_question as _pq


def _mk(code, n_ul, qid="9001"):
    g = "a" * 36
    uls = "".join(f'<ul qtype="0"><li aria-label="A.o{i}">A.o{i}</li></ul>'
                  for i in range(n_ul))
    hidden = f'<input type="hidden" id="answer{qid}{g}" value="">' if n_ul > 1 else ""
    html = f'''<div data="{qid}">
      <div class="TiMu" data="{code}"></div>
      <div class="Zy_TItle"><span>1.</span>题干</div>
      {uls}{hidden}
    </div>'''
    return _pq(_BS(html, "html.parser").find("div"))


check("15 阅读理解 + 2小问 -> 题组", _mk("15", 2)["is_group"], True)
check("19 听力 + 2小问 -> 题组", _mk("19", 2)["is_group"], True)
check("0 单选 + 2个ul -> 不是题组（防误判）", _mk("0", 2, "9002")["is_group"], False)
check("15 但只有1个ul -> 不是题组", _mk("15", 1, "9003")["is_group"], False)

print("\n[8] TiMu 缺失不再崩溃（改为按 unknown 兜底）")
_HTML_NO_TIMU = '''<div data="9004">
  <div class="Zy_TItle"><span>1.</span>结构异常的题目</div>
  <ul><li aria-label="A.甲">A.甲</li></ul>
</div>'''
try:
    _q = _pq(_BS(_HTML_NO_TIMU, "html.parser").find("div"))
    check("缺 TiMu 时返回 unknown 而不抛异常", _q["type"], "unknown")
except Exception as e:
    check(f"缺 TiMu 抛异常（{type(e).__name__}）", False, True)

print("\n" + "=" * 70)
print(f"结果: 通过 {len(PASS)} / 失败 {len(FAIL)} / 警告 {len(WARN)}")
for f in FAIL:
    print(f"  FAIL - {f}")
for w in WARN:
    print(f"  WARN - {w}")
print("=" * 70)
sys.exit(1 if FAIL else 0)
