# SolidWorks 三视图建模

把三视图变成 SolidWorks 实体模型的工具集。读图、出尺寸表、写规格仍然由模型完成；这个插件负责把规格落到 SolidWorks 里，并把结果导回来供比对。

## 工具

| 工具 | 作用 |
|---|---|
| `sw_model` | 按 JSON 规格建模，导出四向视图，可选第一角三视图工程图 |
| `sw_view` | 对已有 `.SLDPRT` 导出指定方向视图 |
| `sw_drawing` | 由零件生成主/俯/左三视图工程图 PNG |
| `sw_doctor` | 自检 Python、pywin32、SW 版本与模板 |

## 规格格式

单位一律毫米。模型空间右手系：X 向右、Y 向上、Z 朝观察者。

```json
{
  "name": "bracket",
  "steps": [
    {"op": "boss", "plane": "front", "rect": [-50, -30, 50, 30], "depth": 12},
    {"op": "cut",  "face": "top",   "rect": [-40, 2, -20, 10],  "depth": 5},
    {"op": "cut",  "face": "top",   "circle": [30, 6, 4],       "through": true}
  ]
}
```

- 视图与基准面：主视图 → `plane:"front"`（u=X, v=Y，厚度走 Z）；俯视图 → `plane:"top"`（u=X, v=Z）；侧视图 → `plane:"right"`（u=Z, v=Y）。
- 形状键：`rect:[u1,v1,u2,v2]`、`circle:[u,v,r]`、`circles:{"r":r,"at":[[u,v],…]}`、`polyline:[[u,v],…]`、`slot:[u1,v1,u2,v2,width]`、`polygon:[cu,cv,r,sides,rot]`。
- 其它键：`depth`、`through`、`both`、`reverse`、`draft`、`offset`、`optional`。
- 实体面用 `face:"top|bottom|front|back|right|left"`，插件会插入偏移基准面，坐标仍按该视图的二维系给。

## 运行前提

- Windows 上装有 SolidWorks（开发验证于 2026 SP04.1），且没有模态对话框占用。
- Python 3.10+ 且安装 pywin32；解释器按 `config.pythonExe` → `DSH_SW_PYTHON` → DSH 自带运行时 → `PATH` 的顺序探测。
- 建模会真实拉起 SolidWorks 窗口；简单件约 5～15 秒，复杂件更久。

## 已知限制

- 圆角不可用：SolidWorks 2026 的 `FeatureFillet3` 实参与 pywin32 类型不匹配。倒角可用。
- 不支持钣金、装配体、螺纹、工程图尺寸标注、阵列/镜像。
- 工程图视图由 SolidWorks 自动排布为第一角投影、1:1 比例，仅用于比对，不是正式图纸。
