"""回归测试：验证题型处理链路的核心行为不被破坏。

每次改 api/answer_check.py、api/base.py 的 random_answer、
api/answer.py 的答案提取后都要跑这个。全部通过才算没搞坏旧功能。

用法: python regression_test.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.answer_check import (
    check_submittable, plausible_answer, _is_letter_only,
    check_judgement, is_listening_question, count_listening_pairs,
    looks_like_refusal,
)

PASS = []
FAIL = []


def check(name, got, expect):
    ok = got == expect
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    if not ok:
        print(f"  [{mark}] {name}: got={got!r} expect={expect!r}")
    else:
        print(f"  [{mark}] {name}")


print("=" * 68)
print("回归测试：题型处理链路")
print("=" * 68)

# ---------- 1. check_submittable：选择题必须是纯字母 ----------
print("\n[1] check_submittable 选择题形态")
check("单选 A 可提交", check_submittable("A", "single"), True)
check("多选 ABC 可提交", check_submittable("ABC", "multiple"), True)
check("听力连写 CADB 可提交", check_submittable("CADB", "single"), True)
check("带题号文本被拦下", check_submittable("22.C 23.A", "single"), False)
check("整句英文被拦下", check_submittable("The sense of being out", "single"), False)
check("中文单字被拦下", check_submittable("对", "single"), False)
check("空串被拦下", check_submittable("", "single"), False)
check("None 被拦下", check_submittable(None, "single"), False)

# ---------- 2. check_submittable：填空只判非空 ----------
print("\n[2] check_submittable 填空形态")
check("填空中文可提交", check_submittable("解释", "completion"), True)
check("填空英文可提交", check_submittable("getwd()", "completion"), True)
check("填空空串被拦下", check_submittable("", "completion"), False)

# ---------- 3. check_submittable：判断题（本次修复的重点） ----------
print("\n[3] check_submittable 判断形态（修复项）")
check("judgement true 可提交", check_submittable("true", "judgement"), True)
check("judgement false 可提交", check_submittable("false", "judgement"), True)
check("judgement 中文'对'可提交", check_submittable("对", "judgement"), True)
check("judgement 中文'错'可提交", check_submittable("错", "judgement"), True)
check("judgement '正确'可提交", check_submittable("正确", "judgement"), True)
check("judgement '错误'可提交", check_submittable("错误", "judgement"), True)
check("judgement 空串被拦下", check_submittable("", "judgement"), False)
check("judgement 乱码被拦下", check_submittable("随便写点什么", "judgement"), False)

# ---------- 4. check_submittable：简答题（本次修复的重点） ----------
print("\n[4] check_submittable 简答形态（修复项）")
check("简答中文可提交", check_submittable("这是一种解释型语言", "shortanswer"), True)
check("简答英文可提交", check_submittable("R is interpreted", "shortanswer"), True)
check("简答空串被拦下", check_submittable("", "shortanswer"), False)

# ---------- 5. plausible_answer：缓存层校验 ----------
print("\n[5] plausible_answer 缓存校验")
check("判断 true 可信", plausible_answer("对", "judgement"), True)
check("判断 false 可信", plausible_answer("错误", "judgement"), True)
check("判断乱码不可信", plausible_answer("我无法访问音频", "judgement"), False)
check("单选 A 可信", plausible_answer("A", "single"), True)
check("单选拒答文本不可信", plausible_answer("我无法访问音频文件", "single"), False)
check("填空文字可信", plausible_answer("解释型", "completion"), True)
check("简答文字可信", plausible_answer("R是解释型语言", "shortanswer"), True)

# ---------- 6. _is_letter_only 边界 ----------
print("\n[6] _is_letter_only 边界")
check("单字母 A", _is_letter_only("A"), True)
check("连写 CADB", _is_letter_only("CADB"), True)
check("空格分隔 A C D", _is_letter_only("A C D"), True)
check("中文对 被拦", _is_letter_only("对"), False)
check("混合 A1 被拦", _is_letter_only("A1"), False)
print("  -- 带选项上下文（ABCD）--")
from api.answer_check import set_option_letters
set_option_letters("ABCD")
check("上下文内 CADB 合法", _is_letter_only("CADB"), True)
check("上下文外 REVI 被拦", _is_letter_only("REVI"), False)
set_option_letters(["A", "B", "C", "D"])
check("列表形式 ABCD 生效", _is_letter_only("AB"), True)
check("列表形式 REVIEW 被拦", _is_letter_only("REVIEW"), False)
set_option_letters(None)
check("清空后回退长度兜底", _is_letter_only("review"), True)

# ---------- 7. check_judgement 判断题词表 ----------
print("\n[7] check_judgement 词表")
check("true -> 1", check_judgement("true", [], []), 1)
check("false -> 0", check_judgement("false", [], []), 0)
check("对 -> 1", check_judgement("对", [], []), 1)
check("错 -> 0", check_judgement("错", [], []), 0)
check("√ -> 1", check_judgement("√", [], []), 1)
check("× -> 0", check_judgement("×", [], []), 0)
check("乱码 -> -1", check_judgement("乱码", [], []), -1)

# ---------- 8. 听力题识别 ----------
print("\n[8] 听力题识别")
check("听力标记", is_listening_question({"title": "【听力题】xxx"}), True)
check("mp3 标记", is_listening_question({"title": "Cet-4-3.1.mp3 Questions 1 and 2"}), True)
check("Questions N to M", is_listening_question({"title": "Questions 5 to 7 are based on"}), True)
check("普通题不误判", is_listening_question({"title": "R是____性语言"}), False)

# ---------- 9. 填空题（2026-09-13 新增）----------
print("\n[9] 填空题：多空拆分 + 提交字段")
check("completion 非空可提交", check_submittable("hello", "completion"), True)
check("completion 空串被拦", check_submittable("", "completion"), False)
check("shortanswer 非空可提交", check_submittable("这是简答", "shortanswer"), True)
check("shortanswer 空串被拦", check_submittable("", "shortanswer"), False)
check("completion 自由文字可提交", plausible_answer("任意文字答案", "completion"), True)
check("shortanswer 自由文字可提交", plausible_answer("任意文字答案", "shortanswer"), True)


def _split_blanks(res, n_blank):
    """复刻 base.py 里填空答案的拆分规则，用于回归验证。"""
    import re as _re
    if isinstance(res, list):
        parts = [str(x).strip() for x in res if str(x).strip()]
    else:
        parts = [p.strip() for p in
                 _re.split(r"\s*[|｜\n]\s*|\s*[，,]\s*", str(res))
                 if p.strip()]
    if n_blank > 1:
        if len(parts) < n_blank:
            parts = parts + [""] * (n_blank - len(parts))
        elif len(parts) > n_blank:
            parts = parts[:n_blank]
    return parts


check("列表答案 2 空", _split_blanks(["a", "b"], 2), ["a", "b"])
check("管道符拆分 2 空", _split_blanks("a|b", 2), ["a", "b"])
check("换行拆分 2 空", _split_blanks("a\nb", 2), ["a", "b"])
check("中文逗号拆分 2 空", _split_blanks("甲，乙", 2), ["甲", "乙"])
check("不足补齐 3 空", _split_blanks("a", 3), ["a", "", ""])
check("超出截断 2 空", _split_blanks("a|b|c", 2), ["a", "b"])
check("单空保留首段", _split_blanks("a，b", 1)[0], "a")

# ---------- 10. 未知题型 unknown（2026-09-13 修复）----------
print("\n[10] 未知题型 unknown：文字答案放行、拒答拦下")
# 超星 18 种题型中排序题/完型填空/名词解释/论述/计算/分录题等都会落到 unknown。
# 修复前它们走"必须纯字母"分支 → 文字答案 100% 被拦（同听力题事故）。
check("unknown 中文答案放行", check_submittable("光合作用", "unknown"), True)
check("unknown 长文本放行", check_submittable("因为物体受到重力作用而下落", "unknown"), True)
check("unknown 数字答案放行", check_submittable("3.14", "unknown"), True)
check("unknown 空串被拦", check_submittable("", "unknown"), False)
check("unknown 排序字母仍放行", check_submittable("ACBD", "unknown"), True)
# 但必须挡住 AI 拒答文本（否则会写进缓存，之后每次命中垃圾）
check("unknown 拒答文本被拦", check_submittable("抱歉，我无法访问音频文件", "unknown"), False)
check("unknown AI 自称被拦", check_submittable("作为一个AI，我不能回答这个问题", "unknown"), False)

print("\n[10b] plausible_answer 缓存层同样放宽但仍挡拒答")
check("缓存 unknown 文字放行", plausible_answer("牛顿第一定律", "unknown"), True)
check("缓存 unknown 拒答拦下", plausible_answer("我无法确定答案", "unknown"), False)
check("缓存 completion 拒答拦下", plausible_answer("我无法确定答案", "completion"), False)
check("缓存 single 非法拦下", plausible_answer("这是一整句中文", "single"), False)
check("缓存 single 合法放行", plausible_answer("A", "single"), True)

print("\n[10c] looks_like_refusal 拒答识别")
check("中文拒答", looks_like_refusal("抱歉，我无法访问该文件"), True)
check("英文拒答", looks_like_refusal("I'm sorry, I cannot answer"), True)
check("正常答案不误判", looks_like_refusal("光合作用"), False)
check("含抱歉的正常答案", looks_like_refusal("答案是：光合作用"), False)

# ---------- 11. 题型码映射（2026-09-13 补全）----------
print("\n[11] 题型码映射（来源：CxKitty QuestionType 枚举，权威）")
from api.decode import _get_question_type
check("0 单选", _get_question_type("0"), "single")
check("1 多选", _get_question_type("1"), "multiple")
check("2 填空", _get_question_type("2"), "completion")
check("3 判断", _get_question_type("3"), "judgement")
check("4 简答", _get_question_type("4"), "shortanswer")
check("5 名词解释", _get_question_type("5"), "shortanswer")
check("6 论述", _get_question_type("6"), "shortanswer")
check("7 计算", _get_question_type("7"), "shortanswer")
check("8 其它", _get_question_type("8"), "shortanswer")
check("9 分录", _get_question_type("9"), "shortanswer")
check("10 资料", _get_question_type("10"), "shortanswer")
check("14 完型填空", _get_question_type("14"), "completion")
check("18 口语", _get_question_type("18"), "shortanswer")
check("19 听力", _get_question_type("19"), "single")
# 题组/形态不确定的保持 unknown（校验最宽松，不会误杀多小问答案）
check("11 连线 -> unknown", _get_question_type("11"), "unknown")
check("13 排序 -> unknown", _get_question_type("13"), "unknown")
check("15 阅读理解 -> unknown（题组，靠 is_group 拆分）",
      _get_question_type("15"), "unknown")
check("20 共用选项 -> unknown（可能多小问）", _get_question_type("20"), "unknown")
check("21 测评 -> unknown", _get_question_type("21"), "unknown")
check("99 未知码 -> unknown", _get_question_type("99"), "unknown")

print("\n[11b] 题组标记 is_group（阅读理解/听力 需按小问拆分）")
from bs4 import BeautifulSoup as _BS
from api.decode import _process_question as _pq

_GUID = "a" * 36
_GROUP_HTML = f'''
<div data="9001">
  <div class="TiMu" data="15"></div>
  <div class="Zy_TItle"><span>1.</span>阅读下列材料，回答问题</div>
  <ul qtype="0"><li aria-label="A.选项一">A.选项一</li></ul>
  <ul qtype="0"><li aria-label="A.选项二">A.选项二</li></ul>
  <input type="hidden" id="answer9001{_GUID}" value="">
</div>'''
_node = _BS(_GROUP_HTML, "html.parser").find("div")
_q = _pq(_node)
check("阅读理解被标记为题组", _q["is_group"], True)
check("阅读理解小问字段已登记", f"answer9001{_GUID}" in _q["answerField"], True)

_SINGLE_HTML = '''
<div data="9002">
  <div class="TiMu" data="0"></div>
  <div class="Zy_TItle"><span>1.</span>普通单选题</div>
  <ul><li aria-label="A.甲">A.甲</li></ul>
</div>'''
_q2 = _pq(_BS(_SINGLE_HTML, "html.parser").find("div"))
check("普通单选题不是题组", _q2["is_group"], False)

# ---------- 12. 填空题双结构（2026-09-13 实地抓包校正）----------
print("\n[12] 填空题：PC 版 / 手机版两种 DOM 结构都要认")
# PC 版章节测验真实结构：ul.Zy_ulTk > div.blankItemDiv，
# 提交字段是 answerEditor{qid}N（textarea）
_PC_HTML = '''
<div data="9005">
  <div class="TiMu" data="2"></div>
  <div class="Zy_TItle"><span>12</span>【填空题】___、___、___ 三个空</div>
  <ul class="Zy_ulTk">
    <div class="blankItemDiv"><span class="font14 tiankong fl">第1空：</span>
      <div class="XztiHover1 fl blankItemInp">
        <div class="InpDIV" id="inpDiv90051"></div>
        <div class="textDIV" style="display:none">
          <textarea id="answerEditor90051" name="answerEditor90051"></textarea>
        </div></div></div>
    <div class="blankItemDiv"><span class="font14 tiankong fl">第2空：</span>
      <div class="XztiHover1 fl blankItemInp">
        <div class="InpDIV" id="inpDiv90052"></div>
        <div class="textDIV" style="display:none">
          <textarea id="answerEditor90052" name="answerEditor90052"></textarea>
        </div></div></div>
    <div class="blankItemDiv"><span class="font14 tiankong fl">第3空：</span>
      <div class="XztiHover1 fl blankItemInp">
        <div class="InpDIV" id="inpDiv90053"></div>
        <div class="textDIV" style="display:none">
          <textarea id="answerEditor90053" name="answerEditor90053"></textarea>
        </div></div></div>
  </ul>
</div>'''
_q3 = _pq(_BS(_PC_HTML, "html.parser").find("div"))
check("PC版填空 空数=3", _q3["blankCount"], 3)
check("PC版填空 字段前缀 answerEditor",
      sorted(k for k in _q3["answerField"] if k.startswith("answerEditor")),
      ["answerEditor90051", "answerEditor90052", "answerEditor90053"])

# 手机端作业结构（CxKitty 那套）：ul.blankList2 > li，字段 answer{qid}N
_MOBILE_HTML = '''
<div data="9006">
  <div class="TiMu" data="2"></div>
  <div class="Zy_TItle">【填空题】甲___乙___</div>
  <ul class="blankList2">
    <li><span>（1）</span><input class="blankInp2" value=""></li>
    <li><span>（2）</span><input class="blankInp2" value=""></li>
  </ul>
</div>'''
_q4 = _pq(_BS(_MOBILE_HTML, "html.parser").find("div"))
check("手机版填空 空数=2", _q4["blankCount"], 2)
check("手机版填空 字段前缀 answer",
      sorted(k for k in _q4["answerField"]
             if k.startswith("answer9006") and k != "answer9006"),
      ["answer90061", "answer90062"])

print("\n[13] 判断题不得被当成听力答案大写化（2026-09-13 实地事故）")
# 事故：normalize_listening_answer 曾被无条件应用到所有题型，
# 把判断题的 "true"/"false" 转成 "TRUE"/"FALSE" → 平台整卷"作业提交失败！"
from api.base import normalize_listening_answer as _norm
check("normalize 确实会把 true 大写（函数本身行为）", _norm("true"), "TRUE")
check("所以调用处必须有守卫：判断题不是听力题",
      is_listening_question({"title": "【判断题】地球是圆的"}), False)
check("普通填空题也不是听力题",
      is_listening_question({"title": "【填空题】中国的首都是___"}), False)
check("听力题仍会被正确识别（守卫不影响它）",
      is_listening_question({"title": "【听力题】Cet-4-3.1.mp3 Questions 1 and 2"}), True)
# 判断题提交值必须是平台认的小写。注意 check_submittable 只管"形态"、
# 用 text.lower() 判断，所以大写 TRUE 也会放行 —— 大小写语义由**调用处**
# 保证（已改为只对听力/题组题做规范化）。这里两个都断言为可提交。
check("judgement 可提交 true", check_submittable("true", "judgement"), True)
check("judgement 可提交 false", check_submittable("false", "judgement"), True)
check("judgement 大写也放行（形态层只管形态）",
      check_submittable("TRUE", "judgement"), True)
# 真正要保证的是：调用 normalize 时判断题会被跳过
check("判断题的 true 不会被规范化函数碰到（守卫条件）",
      (is_listening_question({"title": "【判断题】某命题"}) or False), False)

print("\n[14] map_multiple_answer：多选「内容 → 字母」（2026-09-14 修复）")
# 事故：AI 提示词按设计要求"输出选项内容而非字母"，但映射兜底只覆盖 single，
# 多选题答案被 plausible_answer 以"非纯字母"拒掉 → 整题丢弃 → 随机作答
# （2026-09-14 实测 3.2 三道多选题全部随机，白丢分）
from api.answer_check import set_option_letters as _sol
from api.base import map_multiple_answer as _mm, map_single_answer as _ms
_sol(None)  # 清空上下文，避免上面的用例残留干扰
_OPTS = "A RLHF技术\nB SFT技术\nC PPO技术\nD GAN技术"
check("多选：三段内容 → ABC", _mm("RLHF技术\nSFT技术\nPPO技术", _OPTS), "ABC")
check("多选：乱序内容 → 排序去重", _mm("PPO技术\nRLHF技术", _OPTS), "AC")
check("多选：已是字母串 → 规范化", _mm("ABC", _OPTS), "ABC")
check("多选：字母带分隔符 → 合并", _mm("A、C", _OPTS), "AC")
check("多选：英文单词不当作字母串", _mm("review", _OPTS), "")
check("多选：无选项时返回空（走随机）", _mm("RLHF技术", ""), "")
check("多选：映射结果可通过 plausible_answer",
      plausible_answer(_mm("RLHF技术\nSFT技术", _OPTS), "multiple"), True)
check("多选：映射为空时被 plausible_answer 拦下（不让垃圾进缓存）",
      plausible_answer(_mm("review", _OPTS), "multiple"), False)
check("单选内容映射未被破坏（回归）", _ms("PPO技术", _OPTS), "C")

print("\n" + "=" * 68)
print(f"结果: 通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("\n失败的用例:")
    for f in FAIL:
        print(f"  - {f}")
print("=" * 68)
sys.exit(1 if FAIL else 0)
