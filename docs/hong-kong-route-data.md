# 香港景点间路线耗时

`dataset/hong-kong-routes.json` 按起点和终点 ID 存储有方向的路线。覆盖现有 20 个景点两两之间的 380 条路线，以及香港喜来登酒店与景点之间的 40 条路线，排除同地点往返。

| 字段 | 含义 |
| --- | --- |
| `walk_only_seconds` | 全程纯步行耗时 |
| `transit_total_seconds` | 公共交通方案从实际离开起点到抵达终点的全程耗时，含步行接驳及行程内等候 |
| `transit_walk_seconds` | 同一公共交通方案中所有步行分段的耗时之和 |
| `transit_modes` | 方案使用的公共交通方式 |
| `uses_mtr_subway`、`mtr_lines` | 选中方案是否使用港铁重铁及线路代码 |
| `transit_selection` | 标记从引擎调试结果补回的公共交通候选 |

三项耗时单位均为秒。查询未返回方案时，对应耗时为 `null`。公共交通以香港时间 2026-11-11 10:00 为统一的最早出发时间，从 OpenTripPlanner 返回的候选中取最早抵达方案。数据使用社区班次估算与现有景点参考坐标，适用于同一条件下的离线比较。它不含实时延误，也未逐一核实景点入口。将公共交通全程耗时用于顺路程度时，步行接驳已计入其中。

原先缺失的 64 条使用引擎的 LIST_ALL 调试结果补充。从搜索时间窗口内包含交通工具的候选中选最早抵达的一条。这些方案被引擎的常规筛选规则排除，主要涉及与步行方案的比较，可能较步行耗时更长。数据表标记为 `debug_candidate_filtered_by_otp`。其余 316 条使用常规候选，比较时应注意两种筛选口径。

新增的酒店路线中，14 条同样通过调试候选补充。酒店坐标采用 OpenStreetMap 酒店参考点，未核实为入口。酒店与香港岛或大屿山景点之间的纯步行耗时按跨海规则保留为 `null`。 [酒店参考坐标](https://mapcarta.com/32271172)

## 数据来源与复现

- 道路网络由 Geo2Day 提供香港 OpenStreetMap 提取文件，© OpenStreetMap contributors。 [道路提取文件](https://geo2day.com/asia/china/hong_kong.html) · [OpenStreetMap 版权](https://www.openstreetmap.org/copyright)
- 班次使用 Wheels Hong Kong Community GTFS 的 `20261110` 版。该版本含港铁重铁与巴士等方式。使用单一社区班次包，避免与运输署包中的重叠线路重复导入。上游贡献包括香港特区政府、相关数据提供机构及 DATA.GOV.HK。 [社区 GTFS 与完整署名](https://github.com/wheelstransit/hongkong-community-gtfs#data-source-attribution) · [DATA.GOV.HK 使用条件](https://data.gov.hk/en/terms-and-conditions)
- 因 OpenTripPlanner 对超长班距记录报错，生成时移除了 `frequencies.txt` 中班距不少于 43200 秒的 262 条记录。这些班次未纳入路线搜索，数据覆盖因此不完整。JSON 中记录原始包和处理后包的 SHA-256。
- 结果表以 ODbL 1.0 提供。原始数据的 URL 与 SHA-256 记在 JSON 的 `provenance` 中。源文件地址会更新，复现同一版结果时需比对哈希。 [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/)

准备 Java 21、OpenTripPlanner 2.8.0 和两个输入文件。在项目根目录执行下列命令。第一条命令启动本地路由服务，后两条在另一终端运行。 [OpenTripPlanner 2.8.0 下载说明](https://docs.opentripplanner.org/en/v2.8.0/Getting-OTP/)

```bash
java -Xmx5G -jar otp-shaded-2.8.0.jar --build --serve --port 18118 --bindAddress 127.0.0.1 /path/to/graph

python3 -c 'import json; print(json.dumps(json.load(open("dataset/hong-kong-routes.json"))["provenance"]))' > /tmp/hk-route-provenance.json
python3 scripts/generate_hk_routes.py --endpoint http://127.0.0.1:18118/otp/gtfs/v1 \
  --departure 2026-11-11T10:00:00+08:00 --provenance /tmp/hk-route-provenance.json --include-accommodations \
  --output /tmp/hong-kong-routes.json
```

图目录应只包含 `hong-kong.osm.pbf` 和经过上述过滤的 `hk-community-gtfs.zip`。对最新原始包复现时，应先使用 CSV 工具移除 `frequencies.txt` 中 `headway_secs >= 43200` 的行，再重新打包其余文件。可加 `--pair hk_museum_of_art hong_kong_disneyland` 只查询一对景点。
