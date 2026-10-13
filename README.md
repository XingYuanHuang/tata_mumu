# 巡访助手 - 群消息监控 + MuMu多实例巡访

微信群/QQ群消息 → UID自动识别 → MuMu多实例自动巡访领礼包。

## 整体流程

```
微信群 ──┐
         ├──→ 消息监控 → 识别UID → 主号领取（搜索→访问→领礼盒）
QQ群 ────┘
                          ↑
Excel名单 → 小号查找（只搜索不领取）→ 发现可领取→同步给主号
```

- **主号**（1~N个）：收到UID后执行完整巡访领取流程
- **小号**（0~1个）：循环读Excel，只查找不领取，发现可领取的UID同步给主号
- 支持只开1个主号、只开1个小号、或任意组合
- 每个通道内部串行，通道间并行

## 巡访状态机（复用塔塔助手exe的10个状态）

```
search     找朋友页面 → 点搜索框 → 输入UID → 回车搜索
  ↓
results    搜索结果（一个匹配）→ 点固定位置头像
  ↓
profile    玩家资料页 → 点拜访按钮
  ↓
home_gift  发现礼盒 → 主号：点击领取
           小号(scan_only)：发现礼盒即返回，不点击，同步给主号
home_empty 无礼盒=空旗（连续两次确认）→ 返回找朋友
  ↓
choose     选择祝福弹窗 → 点选择按钮
  ↓
claimed    领取结果页 → 关闭 → 返回找朋友 → 下一个UID
honors     荣誉墙（无领取界面）→ 返回找朋友
```

- 模板配置（阈值+搜索区域）复用exe的 `template_specs.json`
- 每步操作采用轮询等待（非固定等待），和exe一致
- 每次点击有3次重试，超时12秒

## 小号 scan_only 模式

小号只查找不领取，发现可领取的UID后同步给主号：

| 小号查找结果 | 行为 | Excel标记 |
|------------|------|----------|
| available（可领取） | 同步UID给主号领取 | 标记跳过 |
| empty（已抢完） | 不同步 | 标记跳过 |
| claimed（已领过） | 不同步 | 标记跳过 |
| error（出错） | 不同步 | 不标记，下次重试 |

## 自动发现MuMu实例

和塔塔助手exe一样，**不需要手动配置IPC文件**：

1. 自动扫描 `ProgramFiles\Netease\MuMuPlayer-12.0` 安装目录
2. 读取 `vms/*/configs/vm_config.json` 获取每个实例的ADB端口
3. 自动生成IPC配置并连接MuMu原生SDK
4. 检查多应用保活是否关闭
5. 连接成功后实时推送截图到前端

点"自动检测环境"按钮一步到位：自动发现 → 连接 → 检测微信/QQ → 显示结果。

## 文件结构

| 文件 | 说明 |
|------|------|
| `server.py` | FastAPI后端，WebSocket推送+REST接口+截图推送 |
| `visit_engine.py` | 巡访执行器，MuMu SDK截图+OpenCV匹配+10状态机+重试 |
| `task_handler.py` | 多通道单线程队列（主号+小号），UID去重 |
| `adb_controller.py` | ADB多实例管理，动态切换主号/小号角色 |
| `mumu_discover.py` | MuMu自动发现，扫描安装目录，生成IPC配置 |
| `monitor.py` | 微信消息读取（Windows UIAutomation） |
| `qq_monitor.py` | QQ消息读取（Windows UIAutomation） |
| `excel_reader.py` | Excel UID循环读取，按查找结果标记 |
| `config.py` | 全局配置（UID位数、实例端口、角色、Excel路径） |
| `index.html` | Web面板（参考塔塔助手exe风格） |
| `requirements.txt` | 依赖清单 |
| `visit_assets/` | 11个模板图片+template_specs.json |
| `code/fishing_v023/` | 塔塔助手源码（复用mumu_io.py、windows_io.py） |

## 安装

```bash
pip install -r requirements.txt
```

依赖：uiautomation, psutil, fastapi, uvicorn, websockets, openpyxl, opencv-python-headless, numpy, python-multipart

## 配置

### UID位数

编辑 `config.py`：

```python
UID_LENGTH = 8  # 游戏UID位数，按实际改
```

### 关闭MuMu多应用保活

MuMu设置 → 高级设置 → 关闭"多应用保活" → 重启模拟器。每个实例都要关。

### IPC配置

**不需要手动配置**。启动后点"自动检测环境"会自动扫描MuMu安装目录并连接。如果自动发现失败，也可以在右侧模拟器画面区域手动输入IPC路径。

## 运行

```bash
python server.py
```

浏览器打开 `http://localhost:8000`

## Web面板

参考塔塔助手exe风格（紫色主题、白色卡片、步骤式引导）。

**顶部**：自动检测环境、连接全部ADB、停止全部

**左侧**：
1. 连接状态 — 微信/QQ/ADB状态灯 + 实例卡片（角色下拉框）+ 检测报告
2. 消息监控 — 启停按钮 + Excel读取 + 速度选择(快速/标准/慢速) + 统计 + 进度条
3. 消息流 — 实时滚动，UID高亮
4. 巡访任务列表 — UID来源/通道/状态/执行结果

**右侧**：
1. 模拟器画面 — 每实例独立预览框 + IPC配置输入框（备选） + 实时截图（1.5秒刷新）
2. 运行日志 — 实时滚动
3. 巡访引擎 — 显示已初始化的实例

### Excel名单管理

点"查看名单"按钮弹出窗口：
- 拖拽上传Excel/CSV/TXT文件，自动提取UID列
- 显示每个UID的查找状态（待查找/可领取/已抢完/已领过/失败）
- 当前正在查找的UID高亮标记
- 统计：总数、已查找、待查找、可领取

支持格式：xlsx/xlsm/csv/tsv/txt，自动识别UID列（优先找表头uid/id，找不到扫描数字列）

## 速度选择

| 选项 | 值 | 说明 |
|------|-----|------|
| 快速 | 0.16秒 | 网络好、加载快 |
| 标准速度 | 0.32秒 | 默认 |
| 慢速稳定 | 0.65秒 | 网络差、加载慢 |

控制每步操作之间的等待间隔，和exe一致。

## 自动检测

点"自动检测环境"按钮：
1. 自动扫描MuMu安装目录，发现所有实例
2. 自动生成IPC配置并连接MuMu原生SDK
3. 检测微信/QQ窗口是否打开
4. 检测每个实例：ADB连接 + 巡访引擎 + 模板加载
5. 显示总体就绪判断

## visit_assets/ 模板文件

| 文件 | exe配置 | 用途 |
|------|---------|------|
| search_icon.png | 0.89 / [516,516,548,550] | 找朋友页搜索框 |
| gift.png | 0.87 / [38,41,111,112] | 礼盒图标 |
| visit.png | 0.89 / [127,998,193,1032] | 拜访按钮 |
| close.png | 0.88 / [275,984,310,1017] | 关闭按钮 |
| ok.png | 0.89 / [254,910,319,946] | 确认按钮 |
| honors.png | 0.88 / [221,18,366,65] | 荣誉墙标题 |
| results_title.png | 0.88 / [235,575,352,602] | 结果页标题 |
| suggest_title.png | 0.89 / [202,575,384,602] | 建议标题 |
| best_title.png | 0.89 / [222,704,365,731] | 最佳运气标题 |
| choice_title.png | 0.89 / [245,704,341,731] | 选择祝福标题 |
| choose_button.png | 0.88 / [130,901,198,936] | 选择按钮 |
| template_specs.json | — | exe提取的模板配置 |

thumb.png 和 empty_flag.png 不需要：头像用固定坐标点击，空旗用"连续两次无礼盒"逻辑判断。

## Windows调试步骤

```
1. pip install -r requirements.txt
2. 关闭MuMu多应用保活
3. python server.py
4. 浏览器打开 http://localhost:8000
5. 点"自动检测环境" → 自动发现MuMu实例并连接
6. 右侧模拟器画面实时显示，确认哪个是主号1/主号2/小号
7. 点"查看名单"拖入Excel文件
8. 启动消息监控 → 微信群发UID → 看是否自动巡访
9. 看日志里的巡访状态和结果，匹配失败调整阈值或缩放基准
```

## 技术栈

- **后端**：Python 3.12, FastAPI, WebSocket
- **消息读取**：Windows UIAutomation（uiautomation库）
- **模拟器操作**：MuMu原生SDK（nemu_* DLL，后台截图不占鼠标）
- **图像识别**：OpenCV模板匹配
- **前端**：单页HTML，参考塔塔助手exe风格

## 已知限制

- 仅支持Windows
- 微信/QQ窗口不能最小化
- 模板图片从exe提取，exe更新后可能需要重新提取
- MuMu分辨率变化后需确认坐标缩放基准（visit_engine.py里的549.0/1033.0）
- 坐标缩放基准549x1033是从exe的template_specs.json最大box坐标推断的，实际MuMu分辨率不同时自动缩放，匹配率低则需调整
