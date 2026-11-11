"""Create an interactive map of itinerary order from saved baseline results."""

import argparse
import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "dataset"


def render_map(rows):
    places = {}
    for filename in ("hong-kong-attractions.json", "hong-kong-accommodations.json"):
        for place in json.loads((DATA / filename).read_text(encoding="utf-8"))["places"]:
            places[place["id"]] = place
    days = []
    for row in rows:
        if row.get("status") != "ok":
            continue
        try:
            answer = json.loads(row.get("output"))
        except (ValueError, TypeError):
            continue
        if not isinstance(answer, dict) or not isinstance(answer.get("days"), list):
            continue
        for index, day in enumerate(answer["days"], 1):
            if not isinstance(day, dict) or not isinstance(day.get("activities"), list):
                continue
            points, missing = [], []
            for number, activity in enumerate(day["activities"], 1):
                if not isinstance(activity, dict):
                    missing.append(f"第 {number} 站（格式异常）")
                    continue
                place_id = activity.get("place_id")
                place = places.get(place_id) if isinstance(place_id, str) else None
                coordinates = place.get("coordinates") if place else None
                if not isinstance(coordinates, dict) or not all(
                    isinstance(coordinates.get(key), (int, float)) and not isinstance(coordinates[key], bool)
                    for key in ("latitude", "longitude")
                ):
                    missing.append(str(place_id or f"第 {number} 站"))
                    points.append(None)
                    continue
                points.append({
                    "number": number, "name": place.get("name_zh") or place_id,
                    "lat": coordinates["latitude"], "lng": coordinates["longitude"],
                    "start": activity.get("start_time"), "end": activity.get("end_time"),
                })
            days.append({"case": str(row.get("case_id", "案例")), "day": index,
                         "date": str(day.get("date") or "日期未确定"),
                         "points": points, "missing": missing})
    payload = json.dumps(days, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>旅行行程地图</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
body{font-family:system-ui,sans-serif;margin:0;color:#1c2937;background:#f4f6f8}
main{max-width:1100px;margin:auto;padding:20px}
select{font-size:16px;padding:8px;max-width:100%;margin:8px 0}
#map{height:65vh;min-height:350px;border-radius:10px;background:#e8edf1}
.note{line-height:1.6;color:#475569}
.marker{background:#155eef;color:white;font-weight:bold;border:2px solid white;border-radius:50%;width:28px;height:28px;text-align:center;line-height:28px}
</style></head><body><main><h1>每日行程地图</h1>
<p class="note">数字代表访问顺序，虚线连接相邻景点，仅用于展示顺序。底图需要联网；地点坐标为资料库参考点。</p>
<label for="day">选择行程</label> <select id="day"></select>
<p id="status" class="note"></p><div id="map"></div><ol id="stops"></ol></main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const days = PAYLOAD;
const select = document.getElementById('day'), status = document.getElementById('status'), stops = document.getElementById('stops');
if (typeof L === 'undefined') {
  status.textContent = '地图组件加载失败，请检查网络连接。';
} else if (!days.length) {
  status.textContent = '没有可展示的行程。';
} else {
  const map = L.map('map').setView([22.3,114.17], 11);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom:19, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'
  }).addTo(map);
  const layer = L.layerGroup().addTo(map);
  days.forEach((day,i) => {const option=document.createElement('option'); option.value=i; option.textContent=day.case+' · 第 '+day.day+' 天 · '+day.date; select.appendChild(option);});
  function show() {
    layer.clearLayers(); stops.replaceChildren(); const day=days[Number(select.value)], bounds=[];
    status.textContent=day.missing.length ? '缺少坐标，无法显示：'+day.missing.join('、') : '点击数字可查看景点和时间。';
    let previous=null;
    day.points.forEach(point => {
      if (!point) {previous=null; return;}
      const coordinate=[point.lat,point.lng]; bounds.push(coordinate);
      const marker=L.marker(coordinate,{icon:L.divIcon({className:'marker',html:String(point.number),iconSize:[32,32]})}).addTo(layer);
      const popup=document.createElement('span'); popup.textContent=point.name+' · '+(point.start||'时间未确定')+'–'+(point.end||'时间未确定'); marker.bindPopup(popup);
      if(previous) L.polyline([previous,coordinate],{color:'#e89b00',dashArray:'7 7',weight:4}).addTo(layer);
      previous=coordinate;
      const item=document.createElement('li'); item.textContent=point.number+'. '+point.name+'（'+(point.start||'时间未确定')+'–'+(point.end||'时间未确定')+'）'; stops.appendChild(item);
    });
    if(bounds.length===1) map.setView(bounds[0],15); else if(bounds.length>1) map.fitBounds(bounds,{padding:[35,35],maxZoom:15}); else map.setView([22.3,114.17],11);
  }
  select.addEventListener('change',show); show();
}
</script></body></html>
""".replace("PAYLOAD", payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.results.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    with args.output.open("x", encoding="utf-8") as output:
        output.write(render_map(rows))
    print(f"地图：{args.output}")


if __name__ == "__main__":
    main()
