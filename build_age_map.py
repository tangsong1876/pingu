# -*- coding: utf-8 -*-
"""
根据 ABLLS-R 官方里程碑年龄(Milestone Age)指南,为每个领域估算一个
适用年龄区间(月),再在领域内部按"先易后难"顺序线性插值,得到每个技能项
的估算适用月龄,输出 age_map.json。

⚠️ 这是估算参考值(中文版量表 PDF 无年龄列)。如与实际不符,可直接编辑
age_map.json 中对应 code 的月龄,无需改代码。
"""
import json

# 领域顺序与 ABLLS-R 数据文件一致;值 = (最小估算月龄, 最大估算月龄)
DOMAIN_AGE = {
    "配合与强化物的效能": (0, 36),
    "视觉表现": (6, 48),
    "接受性语言": (6, 54),
    "模仿": (6, 36),
    "语言模仿": (12, 36),
    "提要求": (12, 48),
    "命名、描述": (12, 60),
    "互动语言": (24, 72),
    "主动语言": (12, 36),
    "用词和语法": (24, 72),
    "休闲娱乐": (12, 48),
    "社会互动": (18, 60),
    "群体指令/集体教学中的表现": (36, 72),
    "教室常规/理解并遵守教学过程中的常规": (36, 78),
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
# 个别领域在 ABLLS-R 中整体起始月龄偏高,为避免首项被插值为 0,
# 对下列领域强制下限(仅当插值得出的首项月龄低于此值时使用)。
DOMAIN_MIN_FLOOR = {
    "接受性语言": 6, "命名、描述": 12, "用词和语法": 24, "群体指令/集体教学中的表现": 36,
    "教室常规/理解并遵守教学过程中的常规": 36, "泛化响应/泛化": 24, "数学技能": 36,
    "书写": 48, "穿衣/穿着": 24, "梳洗/修饰": 24, "如厕": 24, "精细动作": 6,
}

def norm(s):
    return "".join(s.split())

# 把映射表 key 也归一化,便于按领域名精确查找
DOMAIN_AGE_N = {norm(k): v for k, v in DOMAIN_AGE.items()}
DOMAIN_MIN_FLOOR_N = {norm(k): v for k, v in DOMAIN_MIN_FLOOR.items()}

data = json.load(open("ablls_data.json", encoding="utf-8"))
age_map = {}
for dom, items in data["domains"].items():
    nk = norm(dom)
    lo, hi = DOMAIN_AGE_N.get(nk, (0, 60))
    floor = DOMAIN_MIN_FLOOR_N.get(nk)
    n = len(items)
    for i, it in enumerate(items):
        if n == 1:
            age = lo
        else:
            age = lo + (hi - lo) * (i / (n - 1))
        age = round(age)
        if floor is not None and age < floor:
            age = floor
        age_map[it["code"]] = age  # 取整到月

json.dump(age_map, open("age_map.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

# 校验输出
print("已生成 age_map.json, 项数:", len(age_map))
# 抽样:看每个领域首尾项月龄
for dom, items in data["domains"].items():
    codes = [it["code"] for it in items]
    print(f"  {dom[:8]:<10} {codes[0]}={age_map[codes[0]]}月 .. {codes[-1]}={age_map[codes[-1]]}月")
