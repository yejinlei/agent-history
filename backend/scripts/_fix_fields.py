import json
maps = json.load(open('app/data/maps.json', encoding='utf-8'))
for g, items in maps.items():
    for it in items:
        if 'slug' not in it:
            it['slug'] = 'm' + it['tile'][1:]
            it.pop('period', None)
            it.pop('primary', None)
            # 保持字段顺序 slug, tile, title
            keys = list(it.keys())
            for k in keys:
                if k not in ('slug', 'tile', 'title'):
                    it.pop(k)
json.dump(maps, open('app/data/maps.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
# 验证
m2 = json.load(open('app/data/maps.json', encoding='utf-8'))
print('pre_qin[0]:', m2['pre_qin'][0])
new = [x for x in m2['pre_qin'] if x['tile'] == 'p030d'][0]
print('new:', new)
print('total:', sum(len(v) for v in m2.values()))
