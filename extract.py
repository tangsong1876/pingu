import pdfplumber, re, json
from collections import OrderedDict

path = r'C:\Users\18793\Documents\WeChat Files\wxid_yb660i6ov7i122\FileStorage\File\2026-08\ABLLS-R语言与学习技能评估量表.pdf'

def clean(s):
    return (s or '').replace('\n', ' ').strip()

def parse_criteria(txt):
    if not txt:
        return {}
    parts = re.split(r'(?:\s*[（\(]\s*|^|\s)([1-4])\s*(?:[－\-—、.:：）\)]\s*)', txt)
    res = {}
    i = 1
    while i < len(parts) - 1:
        lvl = parts[i]
        desc = parts[i + 1]
        if lvl.isdigit() and 1 <= int(lvl) <= 4:
            res[int(lvl)] = desc.strip()
        i += 2
    return res

items = OrderedDict()
with pdfplumber.open(path) as pdf:
    for pi, page in enumerate(pdf.pages):
        for t in page.extract_tables():
            for row in t:
                if not row or len(row) < 5:
                    continue
                proj = clean(row[0])
                code = clean(row[1]).replace(' ', '')
                score = clean(row[2])
                skill = clean(row[3])
                name = clean(row[4])
                goal = clean(row[5]) if len(row) > 5 else ''
                q = clean(row[6]) if len(row) > 6 else ''
                ex = clean(row[7]) if len(row) > 7 else ''
                crit = clean(row[8]) if len(row) > 8 else ''
                note = clean(row[9]) if len(row) > 9 else ''
                if re.match(r'^[A-Z]\d+$', code):
                    parsed = parse_criteria(crit)
                    if code not in items:
                        items[code] = {
                            'code': code, 'domain': proj,
                            'max_score': int(score) if score.isdigit() else None,
                            'skill_point': skill, 'task_name': name, 'goal': goal,
                            'question': q, 'example': ex, 'criteria': parsed,
                            'criteria_raw': crit, 'note': note, 'page': pi + 1
                        }
                    else:
                        if len(parsed) > len(items[code]['criteria']):
                            items[code]['criteria'] = parsed
                            items[code]['criteria_raw'] = crit
                            items[code]['page'] = pi + 1
                        if not items[code]['task_name'] and name:
                            items[code]['task_name'] = name
                        if not items[code]['domain']:
                            items[code]['domain'] = proj

# Z16 has empty criteria in source (1-point item): supply default
if 'Z16' in items and not items['Z16']['criteria']:
    items['Z16']['criteria'] = {1: '能用钳子夹住小东西'}
    items['Z16']['criteria_raw'] = '1－能用钳子夹住小东西'

have = sum(1 for v in items.values() if v['criteria'])
print('items', len(items), 'with criteria', have, 'missing', len(items) - have)

domains = OrderedDict()
for code, it in items.items():
    domains.setdefault(it['domain'], []).append(it)

data = {
    'meta': {
        'title': 'ABLLS-R 语言与学习技能评估量表',
        'total_items': len(items),
        'domain_count': len(domains),
        'max_total': sum((v['max_score'] or 0) for v in items.values())
    },
    'domains': domains
}
with open('ablls_data.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=1)
print('max_total', data['meta']['max_total'])
print('saved ablls_data.json')

# print which are still missing criteria and their raw
miss = [(k, v['max_score'], v['criteria_raw']) for k, v in items.items() if not v['criteria']]
for k, mx, raw in miss:
    print('MISS', k, 'max', mx, 'raw=', repr(raw[:100]))
