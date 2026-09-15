# -*- coding: utf-8 -*-
"""听力题：语音转写 → 作答 → 与平台已确认小问对比 → 组装最终答案。

逻辑（用户 2026-09-11 要求，务必按此执行）：
  1. 音频用 Whisper 转成文字（有原文就不再是猜）
  2. 把【听力原文 + 题干 + 全部选项】交给大模型，按小问顺序输出答案；
     用两个模型交叉验证，一致才算高置信
  3. 与"平台已经判对的小问答案"（learned_answers.json 的 sub_verified）逐位对比：
       - 该小问平台已确认 → **以平台为准**（平台是实测判定，最可信）
       - 该小问平台没确认 → 用 AI 基于原文给出的答案
       - 两边都有但不同 → 用平台值，并记为"冲突"（说明 AI 可能识别/推理有误）
  4. 输出对比报告；加 --write 才写入题库，并在学习库记 asr_verified（标注来源）

用法：
  python solve_listening.py                # 只跑转写+作答+对比，出报告
  python solve_listening.py --write        # 另外把最终答案写入题库
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import requests
import urllib3
from faster_whisper import WhisperModel

urllib3.disable_warnings()

INDEX = "audio_index.json"
TRANSCRIPTS = "transcripts.json"
RESULTS = "listening_solved.json"
MODEL_DIR = "models/fw-base"
WRITE = "--write" in sys.argv
LIMIT = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None

_CFG = json.load(open("ui_config.json", encoding="utf-8"))
# 账号不写死（避免随仓库外泄）：用环境变量 CK_ACCOUNT 指定手机号
_M = [p for p in _CFG["profiles"]
      if p.get("username") == os.environ.get("CK_ACCOUNT", "")]
if not _M:
    raise SystemExit("请用环境变量 CK_ACCOUNT 指定学习通账号（手机号）")
_PROF = _M[0]
API_KEY, API_BASE = _PROF["ai_key"], _PROF["ai_endpoint"].rstrip("/")
MODELS = ["agnes-3.0-flash", "agnes-2.5-flash"]


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


# ---------- 转写 ----------
def transcribe_all(index):
    cache = load_json(TRANSCRIPTS, {})
    files = list(dict.fromkeys(it["file"] for it in index))
    todo = [f for f in files if f not in cache or len(cache.get(f) or "") < 20]
    if not todo:
        print(f"转写已缓存 {len(cache)} 段", flush=True)
        return cache
    print(f"待转写 {len(todo)} 段音频…", flush=True)
    model = WhisperModel(MODEL_DIR, device="cpu", compute_type="int8")
    for i, f in enumerate(todo, 1):
        if not os.path.exists(f):
            print(f"  [{i}] 缺文件 {f}", flush=True)
            continue
        t0 = time.time()
        try:
            segs, info = model.transcribe(f, language="en", beam_size=5,
                                         vad_filter=True,
                                         condition_on_previous_text=False)
            text = " ".join(s.text.strip() for s in segs).strip()
        except Exception as e:
            print(f"  [{i}] 转写失败 {type(e).__name__}: {str(e)[:60]}", flush=True)
            continue
        cache[f] = text
        json.dump(cache, open(TRANSCRIPTS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"  [{i}/{len(todo)}] {os.path.basename(f)[:30]} "
              f"{len(text)}字符 {time.time()-t0:.0f}s", flush=True)
    return cache


# ---------- 作答 ----------
def count_blanks(title):
    """标题里每个小问都带 '1. A) ... 2. A) ...' 形式的选项，据此数小问。

    注意：编号前不一定是空白 —— 实测题干是 '...you have just heard.1. A) ...'，
    数字紧跟在句点后面，所以不能用"前导空白"作为条件（2026-09-11 踩过）。
    """
    nums = re.findall(r"(?<!\d)(\d{1,2})\s*[\.\、]\s*A[\)）\.]", title)
    return len(set(nums)) if nums else 1


def ask(model, transcript, title, n):
    sys_p = (
        "你是英语四级听力解题专家。下面给你听力录音的文字转写（语音识别结果，"
        "可能有少量错误）、以及题目与选项。**必须依据听力原文作答**，不要凭常识猜。\n"
        f"本题组共 {n} 个小问，请按小问顺序输出所选字母，去掉题号连写"
        f"（如 3 个小问依次选 C、A、D 就输出 CAD，长度必须为 {n}）。\n"
        "只输出这串字母，不要解释、空格、标点。"
    )
    user_p = (f"【听力原文】\n{transcript[:7000]}\n\n"
              f"【题目与选项】\n{title[:4000]}\n\n请输出 {n} 个字母。")
    try:
        r = requests.post(
            API_BASE + "/chat/completions",
            headers={"Authorization": "Bearer " + API_KEY,
                     "Content-Type": "application/json"},
            json={"model": model, "temperature": 0,
                  "messages": [{"role": "system", "content": sys_p},
                               {"role": "user", "content": user_p}]},
            timeout=150, verify=False)
        if r.status_code != 200:
            print(f"      [{model}] HTTP {r.status_code}: {r.text[:120]}", flush=True)
            return ""
        txt = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"      [{model}] 异常 {type(e).__name__}: {str(e)[:90]}", flush=True)
        return ""
    cands = re.findall(r"[A-Ha-h]{2,12}", txt)
    if not cands:
        return ""
    return max(cands, key=len).upper()


# ---------- 对比与组装 ----------
def merge_with_platform(title, ai_ans, n, learner):
    """平台已确认的小问优先，其余用 AI 答案；输出对比明细。"""
    sv = {}
    try:
        sv = learner.get_sub_verified(title)
    except Exception:
        pass
    final, detail, same, conflict = [], [], 0, 0
    for i in range(n):
        plat = str(sv.get(str(i)) or "").strip().upper()
        ai = ai_ans[i].upper() if i < len(ai_ans) else ""
        if plat:
            final.append(plat)
            if ai:
                if ai == plat:
                    same += 1
                    detail.append(f"问{i+1}:平台{plat}/AI{ai} 一致")
                else:
                    conflict += 1
                    detail.append(f"问{i+1}:平台{plat}/AI{ai} 冲突→用平台")
            else:
                detail.append(f"问{i+1}:平台{plat}")
        elif ai:
            final.append(ai)
            detail.append(f"问{i+1}:AI{ai}")
        else:
            final.append("")
            detail.append(f"问{i+1}:缺")
    return "".join(final), detail, same, conflict


def main():
    index = load_json(INDEX, [])
    if not index:
        print("audio_index.json 为空，请先跑 fetch_audio.py")
        return
    if LIMIT:
        index = index[:LIMIT]

    from api.learned import LearnedAnswers
    from api.answer import CacheDAO
    learner, cdao = LearnedAnswers(), CacheDAO()

    tr = transcribe_all(index)

    results = load_json(RESULTS, {})
    for i, it in enumerate(index, 1):
        title, f = it["title"], it["file"]
        text = tr.get(f, "")
        if not text:
            print(f"[{i}] 无转写: {title[7:44]}", flush=True)
            continue
        n = count_blanks(title)
        ans = {}
        for md in MODELS:
            a = ask(md, text, title, n)
            if a:
                ans[md] = a
        if not ans:
            print(f"[{i}] 无答案: {title[7:44]}", flush=True)
            continue
        vals = list(ans.values())
        agree = len(set(vals)) == 1
        ai_final = vals[0] if agree else max(vals, key=len)
        final, detail, same, conflict = merge_with_platform(
            title, ai_final, n, learner)
        results[title] = {
            "audio": f, "name": it["name"], "blanks": n,
            "ai_answers": ans, "ai_agree": agree,
            "platform_used": sum(1 for d in detail if "平台" in d),
            "same": same, "conflict": conflict,
            "final": final, "detail": detail,
            "transcript_len": len(text),
        }
        json.dump(results, open(RESULTS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        mark = "AI一致" if agree else "AI不一致"
        print(f"[{i}/{len(index)}] {title[7:46]}", flush=True)
        print(f"      {mark} | AI={ai_final} | 最终={final} | "
              f"平台值{same}一致/{conflict}冲突 | {' '.join(detail)}", flush=True)

    n_ok = sum(1 for v in results.values()
               if v["final"] and "?" not in v["final"] and "" not in v["final"])
    print(f"\n完成 {len(results)} 题（可用最终答案 {n_ok} 题）-> {RESULTS}", flush=True)

    if WRITE:
        w = 0
        recs = load_json("learned_answers.json", {})
        for title, v in results.items():
            final = v.get("final") or ""
            if not final or len(final) != v.get("blanks"):
                continue
            if cdao.get_cache(title) != final:
                cdao.add_cache(title, final)
                w += 1
            e = recs.get(title) or {}
            e["asr_verified"] = final
            e["asr_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            e["asr_source"] = "whisper+2models"
            recs[title] = e
        json.dump(recs, open("learned_answers.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"已写入题库 {w} 题（另在学习库登记 asr_verified 以便溯源）", flush=True)


if __name__ == "__main__":
    main()
