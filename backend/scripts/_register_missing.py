import json
catalog = json.load(open('app/data/_osgeo_catalog.json', encoding='utf-8'))
maps = json.load(open('app/data/maps.json', encoding='utf-8'))

# 已注册的 tile（p 开头，服务器对应 mp）
reg = {}
for g, items in maps.items():
    for it in items:
        reg[it['tile']] = g

added = {}
for group, items in catalog.items():
    for it in items:
        # osgeo.cn 的 m030b 对应瓦片图层 mp030b / 注册的 p030b
        pid = 'p' + it['id'][1:]  # m030b -> p030b
        if pid in reg or it['id'] in reg:
            continue
        # 新图：title 清理
        title = it['title'].strip()
        tile = pid  # 沿用 p 前缀注册惯例（后端 _norm_tile 会补 mp）
        maps.setdefault(group, []).append({
            'tile': tile, 'title': title,
            'period': group, 'primary': False,
        })
        added.setdefault(group, []).append((tile, title))

for g, lst in added.items():
    print(g, '+', len(lst))
    for t, ti in lst:
        print('   ', t, ti)
print('total added:', sum(len(v) for v in added.values()))
print('new total:', sum(len(v) for v in maps.values()))
json.dump(maps, open('app/data/maps.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
