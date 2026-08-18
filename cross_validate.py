# -*- coding: utf-8 -*-
"""
交叉验证：把现有 age_map.json 的每个领域估算月龄范围，
与用户提供的 WebABLLS 典型发育时间线（参考引入月龄）逐项比对，
找出偏差并据此校准，重新生成 age_map.json。
"""
import json

def norm(s): return "".join(s.split())

# 参考时间线 -> 各领域“引入月龄”（领域开始成为评估重点的大致月龄）
REF_INTRO = {
    "配合与强化物的效能": 6,      # 6-12个月
    "视觉表现": 6,
    "接受性语言": 18,             # 18-24个月
    "模仿": 6,                    # 6-12个月（基础动作模仿）
    "语言模仿": 18,               # 18-24个月（单音模仿）
    "提要求": 6,                  # 6-12（哭闹/手势）; 18-24（口语）
    "命名、描述": 18,             # 18-24（基础命名）
    "互动语言": 30,               # 30-36（简单对话）
    "主动语言": 30,               # 30-36（自发语言）
    "用词和语法": 24,
    "休闲娱乐": 30,               # 30-36（平行游戏）
    "社会互动": 30,               # 30-36（基础社交）
    "群体指令/集体教学中的表现": 36,
    "教室常规/理解并遵守教学过程中的常规": 36,
    "泛化响应/泛化": 24,
    "数学技能": 36,
    "书写": 48,
    "穿衣/穿着": 24,
    "吃饭/饮食": 12,
    "梳洗/修饰": 24,
    "如厕": 24,
    "粗大动作": 0,
    "精细动作": 6,
}

# 校准后的领域月龄区间 (lower, upper)，lower 对齐参考引入月龄
CALIBRATED = {
    "配合与强化物的效能": (0, 48),
    "视觉表现": (6, 48),
    "接受性语言": (12, 60),
    "模仿": (6, 36),
    "语言模仿": (12, 36),
    "提要求": (6, 48),
    "命名、描述": (18, 60),
    "互动语言": (30, 72),
    "主动语言": (30, 48),
    "用词和语法": (24, 72),
    "休闲娱乐": (24, 48),
    "社会互动": (24, 60),
    "群体指令/集体教学中的表现": (36, 72),
    "教室常规/理解并遵守教学过程中的常规": (36, 72),
    "泛化响应/泛化": (24, 60),
    "数学技能": (36, 78),
    "书写": (48, 84),
    "穿衣/穿着": (24, 60),
    "吃饭/饮食": (12, 48),
    "梳洗/修饰": (24, 60),
    "如厕": (24, 48),
    "粗大动作": (0, 48),
    "精细动作": (6, 60),
}
CAL_MIN_FLOOR = {norm(k): v for k, v in {
    "接受性语言": 12, "命名、描述": 18, "用词和语法": 24, "群体指令/集体教学中的表现": 36,
    "教室常规/理解并遵守教学过程中的常规": 36, "泛化响应/泛化": 24, "数学技能": 36,
    "书写": 48, "穿衣/穿着": 24, "梳洗/修饰": 24, "如厕": 24, "精细动作": 6,
}.items()}

data = json.load(open("ablls_data.json", encoding="utf-8"))
old = json.load(open("age_map.json", encoding="utf-8"))

# ---- 1) 统计现有 age_map 每个领域 min/max ----
cur = {}
for dom, items in data["domains"].items():
    ages = [old[it["code"]] for it in items if it["code"] in old]
    if ages:
        cur[norm(dom)] = (min(ages), max(ages), len(ages))

print("="*96)
print("交叉验证表：现有估算范围 vs WebABLLS 参考引入月龄")
print("="*96)
print(f"{'领域':<26}{'现有范围(月)':<18}{'参考引入':<10}{'判定':<6}说明")
print("-"*96)
for dom, items in data["domains"].items():
    nk = norm(dom)
    lo, hi, n = cur[nk]
    intro = REF_INTRO.get(nk)
    if intro is None:
        flag, note = "—", "参考未单列"
    elif lo <= intro <= intro + 12:   # 首项目不应晚于参考引入+12月
        flag, note = "✓", "基本吻合"
    elif lo > intro:
        flag, note = "⚠偏晚", f"首项目{lo} > 参考{intro}"
    else:
        flag, note = "⚠偏早", f"首项目{lo} < 参考{intro}"
    print(f"{dom[:25]:<26}{f'{lo}-{hi}':<18}{str(intro)+'月':<10}{flag:<6}{note}")

# ---- 2) 用校准区间重建 age_map ----
CAL_N = {norm(k): v for k, v in CALIBRATED.items()}
new = {}
for dom, items in data["domains"].items():
    nk = norm(dom)
    lo, hi = CAL_N.get(nk, (0, 60))
    floor = CAL_MIN_FLOOR.get(nk)
    n = len(items)
    for i, it in enumerate(items):
        age = lo if n == 1 else round(lo + (hi - lo) * (i / (n - 1)))
        if floor is not None and age < floor:
            age = floor
        new[it["code"]] = age

json.dump(new, open("age_map.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

print("\n" + "="*96)
print("校准后范围（已写入 age_map.json）")
print("="*96)
print(f"{'领域':<26}{'校准范围(月)':<18}{'首项目':<10}末项目")
print("-"*96)
for dom, items in data["domains"].items():
    nk = norm(dom)
    lo, hi = CAL_N.get(nk, (0, 60))
    floor = CAL_MIN_FLOOR.get(nk)
    first = max(lo, floor) if floor else lo
    print(f"{dom[:25]:<26}{f'{lo}-{hi}':<18}{str(first)+'月':<10}{hi}月")
print("\n完成。校准依据：WebABLLS 典型发育时间线（参考引入月龄）。")
