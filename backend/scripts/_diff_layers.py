import json, re
layers = json.load(open('app/data/_wmts_layers.json', encoding='utf-8'))
# mp 图层里标题有哪些样式
mp = [l for l in layers if re.match(r'^mp[0-9a-f]{4}$', l['id'])]
print('mp+4hex 总数:', len(mp))
titled = [l for l in mp if l['title'] != l['id']]
print('有真实标题:', len(titled))
for l in titled[:30]:
    print(l['id'], '|', l['title'])
print('---')
# 标题为空的可能是 history-map.osgeo.cn 页面的图
# 再看 history-map 页面的图（p 开头四位在服务器上对应 mp）
# 已注册 151 张全部在服务器上，那《中国历史地图集》304 幅中剩下 153 幅的 id 是什么？
# 看 history-map.osgeo.cn 页面的图源
