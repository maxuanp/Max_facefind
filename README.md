# facefind

当前版本：**1.0.1**

按「人脸 + 人名」从公开图片站点收集照片，在本地做人脸比对、构图筛选、去重与超分，并提供一套本地网页界面管理任务、复核、排除库与存储。

> 本项目只在本机运行，不做云端上传。请只处理你有权使用的素材，并遵守站点条款与当地法律（肖像权、著作权、个人信息保护等）。仓库不包含任何真实人脸、账号 cookie、运行日志或索引数据。

- [1. 能做什么](#1-能做什么)
- [2. 环境要求](#2-环境要求)
- [3. Windows 快速开始（推荐）](#3-windows-快速开始推荐)
- [4. 手动安装与运行](#4-手动安装与运行)
- [5. GPU 配置与 CPU 回退](#5-gpu-配置与-cpu-回退)
- [6. 参考照怎么放](#6-参考照怎么放)
- [7. 登录小红书 / 微博](#7-登录小红书--微博)
- [8. 跑任务：数量、预算与关键词](#8-跑任务数量预算与关键词)
- [9. 阈值含义与校准](#9-阈值含义与校准)
- [10. 构图档位](#10-构图档位)
- [11. 超分档位与显存](#11-超分档位与显存)
- [12. 去重与排除库](#12-去重与排除库)
- [13. 存储与清理](#13-存储与清理)
- [14. 托盘、重启与退出](#14-托盘重启与退出)
- [15. Docker（含 GPU 与登录硬限制）](#15-docker含-gpu-与登录硬限制)
- [16. 目录结构](#16-目录结构)
- [17. 常见故障](#17-常见故障)
- [18. 许可与第三方模型](#18-许可与第三方模型)

---

## 1. 能做什么

- 多站点搜索：百度图片（默认）、微博、小红书（后两者需要登录态）。
- 人脸比对：InsightFace（buffalo_l / antelopev2），按参考照给候选打相似度分。
- 准入分层：`accept` 直接收录 / `review` 待复核 / 丢弃，并记录拒绝原因。
- 构图过滤：只收全身或完整上半身（YOLO pose，可选模型；缺失时保守放行）。
- 去重：SHA-256 精确命中 + pHash 近重复 + 人脸 embedding 近重复；跨运行索引。
- 排除库：扫描你已有的图库，命中即跳过；支持目录监听自动增量。
- 超分：Real-ESRGAN / SwinIR / HAT 多档，可选 GFPGAN 面部修复，分块推理与显存保护。
- 本地 UI：任务运行、结果图片墙、待复核、排除库、存储、账号状态、设置、日志。
- Windows 托盘宿主：启动 / 停止 / 重启 / 打开界面 / 查看日志 / 退出，UI 里也有「重启服务」「退出程序」。

## 2. 环境要求

| 项 | 最低 | 推荐 |
|---|---|---|
| 系统 | Windows 10 64 位 | Windows 11 |
| Python | 3.10 | 3.11 |
| 内存 | 8 GB | 16 GB+ |
| 磁盘 | 2 GB（代码+模型），另加图片空间 | SSD |
| 显卡 | 可选（CPU 可跑） | NVIDIA，6 GB+ 显存 |
| 浏览器 | Chrome 或 Edge（Playwright 也可用自带 Chromium） | 最新版 Chrome |

GPU 需要 NVIDIA 驱动；onnxruntime-gpu 与 CUDA/cuDNN 的版本对应关系见第 5 节。

## 3. Windows 快速开始（推荐）

1. 安装 Python 3.11，勾选 **Add python.exe to PATH**。
2. 解压发行包，双击 **`启动.bat`**。首次运行会自动：
   - 创建 `.venv` 并安装依赖（检测到 `nvidia-smi` 时，按它的 CUDA 版本自动选 `cu124` 或 `cu128` 那一套）；
   - 从 `config.example.yaml` 生成 `config.yaml`；
   - 下载 InsightFace `buffalo_l` 与基础超分权重到 `models/`；
   - 启动托盘宿主并打开 `http://127.0.0.1:8765/`。
3. 把参考照放进 `faces/<人名>/`（规则见第 6 节），在 UI「概览」里新增人员。
4. 在「账号状态」里登录小红书/微博（可选），在「任务运行」里勾选站点、填关键词、开始。
5. 结果在 `downloads/<人名>/`，待复核在「待复核」页处理。

> 不想用托盘：命令行执行 `.venv\Scripts\python -m facefind --config config.yaml ui`，或给 `启动.bat` 传 `--no-tray`。

## 4. 手动安装与运行

```bash
# 1) 虚拟环境
py -3 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip

# 2) 基础依赖（CPU 可用）
.venv\Scripts\python -m pip install -r requirements.txt

# 3) GPU 用户追加：按 nvidia-smi 的 CUDA Version 二选一
#    CUDA 12.0 ~ 12.7 → cu124；CUDA 12.8+ / RTX 50 系 → cu128
.venv\Scripts\python -m pip uninstall -y onnxruntime

# 3a) 驱动 CUDA 12.0 ~ 12.7（大多数 30/40 系）
.venv\Scripts\python -m pip install -r requirements-gpu-cu124.txt

# 3b) 驱动 CUDA 12.8+ / RTX 50 系（Blackwell）
# .venv\Scripts\python -m pip install -r requirements-gpu-cu128.txt

# 4) 配置与模型
copy config.example.yaml config.yaml
.venv\Scripts\python scripts\fetch_models.py --insightface buffalo_l --sr base

# 5) 自检（Python / 包 / GPU / 浏览器 / 网络）
.venv\Scripts\python -m facefind --config config.yaml probe

# 6) 启动界面（托盘）或无宿主模式
.venv\Scripts\python -m facefind.supervisor
.venv\Scripts\python -m facefind --config config.yaml ui
```

常用命令：

| 命令 | 作用 |
|---|---|
| `python -m facefind probe` | 环境勘察：Python / 包 / GPU / 浏览器 / 网络 |
| `python -m facefind faces` | 检查参考照：发现规则、人脸检测告警 |
| `python -m facefind run --person 张三 --queries "张三 高清" --count 8` | 命令行跑一个人 |
| `python -m facefind login xiaohongshu` | 打开有头浏览器登录窗口 |
| `python -m facefind calibrate 张三 --limit 100` | 导出最近候选相似度并给阈值建议 |
| `python -m facefind stats` | 查看跨运行索引统计 |
| `python -m facefind export --all` | 为每人生成缩略图总览 HTML |

## 5. GPU 配置与 CPU 回退

### 5.1 版本对应（装错会报 `CUDA error 126` 或 DLL not found）

| 依赖文件 | 适用驱动 / 显卡 | torch | onnxruntime-gpu |
|---|---|---|---|
| `requirements-gpu-cu124.txt` | CUDA 12.0 ~ 12.7，驱动 ≥ 550（30/40 系多数） | `2.6.0+cu124` | `1.23.2`（CUDA 12.x + cuDNN 9） |
| `requirements-gpu-cu128.txt` | CUDA 12.8 及以上（含 13.x），驱动 ≥ 570（含 RTX 50 系） | `2.7.1+cu128` | `1.23.2`（CUDA 12.x + cuDNN 9） |

判断方法：直接运行 `nvidia-smi`，看右上角 `CUDA Version: 12.4` 这样的字段（它是驱动支持的 CUDA 上限，不是已安装的 toolkit 版本）：

- `12.0 ~ 12.7` → 装 `requirements-gpu-cu124.txt`
- `12.8` 或更高 → 装 `requirements-gpu-cu128.txt`
- 命令不存在或输出里没有 CUDA Version → 不装 GPU 依赖，按 CPU 跑

两套文件都锁定 `+cu124` / `+cu128` 本地版本号，确保 torch 与 onnxruntime-gpu 留在 CUDA 12 主版本内，避免 pip 从 PyPI 解析出 CUDA 13 的 torch，出现「torch 能跑、onnxruntime 找不到 cuDNN」的混搭。

程序启动时会自动把 `CUDA_PATH`、`C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12*` 以及 pip 安装的 `nvidia/*/bin` 加到 `PATH`（见 `facefind/env.py`），所以多数情况下只需要装对 wheel。

### 5.2 CPU 回退

- `config.yaml` 里 `faces.device: auto`（默认）：检测不到 CUDA 就自动用 CPU。
- 强制 CPU：`faces.device: cpu`。
- CPU 下功能完整，只是人脸检测与超分明显变慢；`profile: balanced` + `det_size: 640` 是速度/质量折中。

### 5.3 什么时候该升级到 GPU

- 参考照多于 10 人、每轮候选上千张，或要批量超分时，GPU 收益明显。
- 只是偶尔跑几百张、不超分：CPU 足够，装 GPU 依赖反而容易踩 DLL 版本问题。

## 6. 参考照怎么放

```
faces/
├─ 张三/
│  ├─ 01.jpg
│  ├─ 02.jpg
│  └─ ...
└─ 李四/
   └─ ...
```

要求：

- **一人一个文件夹**，文件夹名就是人名（也是成品目录名）。
- **一张照片只有一张脸**；多人合照会被告警并跳过（可改 `faces.allow_multiple_faces: true`，但不建议）。
- 人脸尽量 **≥ 100 px**；低于 `min_face_px`（默认 80）的候选会被拒。
- **5~10 张**不同角度/光线/发型即可，过多不会更好，反而稀释阈值。
- 避免重度美颜、强滤镜、侧脸超过 45°、遮挡（口罩/墨镜）。
- 放好后运行 `python -m facefind faces` 检查，或在 UI「概览」里看每人的参考照评分。

平铺目录（不想建子文件夹）可在 `data/face_map.yaml` 写「文件名 -> 人名」映射。

## 7. 登录小红书 / 微博

推荐：UI「账号状态」→ 对应站点「打开登录窗口」→ 手动扫码/登录 → 关闭窗口。程序使用的是 **项目私有 profile**（`data/browser_profile`），不会读取或改写你日常浏览器的账号。

也可以放 cookie 文件（只读）：

- `secrets/xhs.txt`：一行，形如 `a1=...; web_session=...; ...`
- `secrets/weibo.txt`：一行，需要包含 `SUB=...`（可带 `SUBP=`）

注意：

- Cookie 会过期。失效表现是「需要登录」或搜索直接空结果，重新登录即可。
- 遇到验证码/滑块/「请求太频繁」时，程序会 **停止该站点**，不会绕过。请等 30~60 分钟、降低并发与 `search.detail_budget` 后再试。
- 小红书与微博的抓取依赖浏览器会话；百度图片不需要登录。

## 8. 跑任务：数量、预算与关键词

在 UI「任务运行」页：

- **站点**：默认只开百度；登录后再勾小红书/微博。
- **关键词**：每人可用多行，一行一个；建议「人名 + 场景/风格」，例如 `张三 高清`、`张三 街拍`。
- **每人数量** `search.count`：最终成功收录数（`review` 不计入）。默认 5。
- **翻页上限** `search.max_pages`：每关键词最多翻多少页/滚动轮次。默认 8。
- **详情页预算** `search.detail_budget`：每人每关键词单轮最多打开多少个小红书笔记详情页。默认 12，是风控节流阀：调大更快也更容易被限；被限时先降它和并发。

什么时候调哪个：

| 现象 | 调整 |
|---|---|
| 每轮几乎不涨、跳过很多 | 先看日志的拒绝原因；若是 `重复`，说明索引已覆盖，换关键词或加大 `count` 无意义 |
| 候选很多但收录少 | 降 `accept_threshold`（更宽松）或换更清晰的参考照 |
| 一直被风控 | 降 `detail_budget`、`max_pages`，站点 `concurrency` 改 1~2，加大 `delay_min_ms` |
| 想更快 | 先确认 GPU 已生效（`probe`）；再提高站点并发；不要盲目加大 `detail_budget` |

## 9. 阈值含义与校准

| 参数 | 默认 | 含义 | 什么时候调 |
|---|---|---|---|
| `accept_threshold` | 0.55 | 相似度 ≥ 该值直接收录 | 误收多 → 调高到 0.60；漏收多且 review 里很多正确 → 降到 0.50 |
| `review_threshold` | 0.35 | 介于 review 与 accept 之间进待复核；低于它直接丢弃 | 想人工多看 → 降到 0.30；不想复核 → 升到 0.45 或关 review |
| `dedupe_threshold` | 0.92 | 人脸 embedding 余弦相似度 ≥ 该值视为同一张脸 | 同一人被当成重复 → 调高到 0.94；漏判重复 → 降到 0.88 |
| `min_face_px` | 80 | 人脸短边像素下限 | 小图多 → 降到 64；只要高清 → 升到 120 |
| `dedupe.phash_threshold` | 8 | pHash 汉明距离 ≤ 该值视为近重复 | 误判重复 → 降到 6；漏判 → 升到 10 |

`similarity_mode`：`max`（默认，取参考照最高分，适合参考照差异大）、`mean`（更严格）、`centroid`（参考照质量高时更稳）。

校准流程：

1. 先跑一轮（或用已有结果）。
2. `python -m facefind calibrate 张三 --limit 100`，看分数分布与建议阈值。
3. 在「待复核」里人工判定，再微调 0.02~0.05，不要一次跳太多。

## 10. 构图档位

`framing.mode`：

| 档位 | 收录 | 适用 |
|---|---|---|
| `prefer_full_body`（默认） | 全身 + 完整上半身 | 大多数场景，平衡召回与质量 |
| `allow_upper_body` | 再放宽到不完整上半身 | 素材稀缺、只想要人脸清晰 |
| `strict_full_body` | 只收全身（髋+膝可见、踝在画面内） | 做穿搭/全身素材库 |

相关参数：

- `min_person_ratio` 0.45：人物框高度占画面比例，过小视为远景/特写。
- `edge_cut_tolerance` 0.03：人物框离画面边缘的容差。
- `framing.model`：默认 `yolov8n-pose.onnx`。**模型缺失时会保守放行**（只记录 `unknown`），不会把图全挡掉。

什么时候调：候选里大量半身/特写被挡 → 改 `allow_upper_body`；反之想要严格全身 → `strict_full_body`，并适当降低 `min_person_ratio` 到 0.35 以避免误杀。

## 11. 超分档位与显存

权重放在 `models/sr/`（文件名必须与 `facefind/sr.py` 一致）。`scripts/fetch_models.py --sr base|all` 可下载基础档。

| 模型 id | 档位 | 显存建议 | 特点 | 何时用 |
|---|---|---|---|---|
| `realesrgan_x4plus` | balanced（默认） | 4~6 GB | 快、纹理自然 | 批量处理首选 |
| `realesrgan_anime` | fast | 4 GB | 插画/二次元 | 只处理动漫素材 |
| `swinir_l_x4` | quality | 6~8 GB | 细节/文字边缘更锐 | 少量精修 |
| `hat_real_gan_x4` | max_quality | 12 GB+ | 细节最强、很慢，强制长边 ≤2048 | 精挑几张 |
| `supir_f4` | optional | 16 GB+ | 生成式，需额外权重与依赖 | 默认不装 |
| `gfpgan`（面部修复） | 附加 | +2 GB | 脸部细节修复 | 人脸糊时开，强度 0.8 左右 |

其他参数（UI「设置」或 `data/ui_settings.yaml`）：

- `scale` 默认 4；`max_side` 默认 4096（HAT 强制 2048）；`tile` 0 = 自动分块。
- `jpeg_quality` 95；`keep_original` 默认保留原图。
- 显存不足的表现：驱动 TDR（画面黑一下）、`CUDA out of memory`。先降 `max_side` 到 2048、`tile` 设为 256~512，或换 `realesrgan`。

## 12. 去重与排除库

三层去重（都在索引里，跨运行有效）：

1. **SHA-256**：完全相同文件直接跳过。
2. **pHash**：`dedupe.phash_threshold`（默认 8），抓近重复/压缩版本。
3. **人脸 embedding**：同一人 `dedupe_threshold`（0.92），抓裁切/滤镜版本。

排除库（只读，不会改你的图库）：

- `exclude.dirs` / `exclude.files` 指向已有图片；命中就跳过下载。
- `watch: true`：启动 UI 后常驻监听，增删图片自动增量更新；`watch_skip` 默认跳过程序自身输出目录。
- `scan_on_run: auto`：监听健康时任务开始不再全量扫描；`always` 每次扫；`never` 不扫。

什么时候调：图库很大且经常变动 → 保持 `watch: true` + `scan_on_run: auto`；发现新图没被排除 → 手动「重建」一次（`exclude.rebuild: true` 或 UI 的「重建排除索引」）。

## 13. 存储与清理

- `downloads/<人名>/`：成品（`accept`）；`downloads/review/<人名>/`：待复核。
- `_raw/<站点>/<人名>/`：原始下载，方便重跑；可在「存储」页按人清理。
- `data/index.db`：跨运行索引（含 URL、指纹、人脸 embedding）。**不要提交/外发**。
- `data/thumbs/`：缩略图缓存，可随时清空，会按需重建。
- `data/recycle/`：被替换/删除的成品移到回收目录（不物理删除），可还原。
- `data/sr/`、`data/sr_preview/`：超分产物与预览。
- `data/logs/`：JSONL 决策日志与运行日志（含真实人名/URL，勿外发）。
- `data/browser_profile/`：登录态（含 Cookies），勿外发。

「存储」页按类别显示占用，可按人清理；清理成品需要完整输入人名确认。程序不会自动删除回收站内容。

## 14. 托盘、重启与退出

Windows 下 `启动.bat` 会启动常驻托盘宿主（`facefind/supervisor.py`），它负责拉起/监控 uvicorn 子进程：

- 托盘菜单：打开界面（默认，双击图标同效）、启动服务、停止服务、重启服务、查看日志目录、退出。
- 图标四态：运行中（绿）、已停止（灰）、启动中（橙）、出错（红）；复用已有实例时显示蓝色。
- 悬停提示：地址、服务 PID、是否在跑任务；出错时带原因。
- 重启 = 优雅停子进程 → 等端口释放 → 再拉起，避免两个进程抢 8765。
- 退出 = 停子进程（含 watcher 与 Playwright 会话）→ 移除托盘图标 → 宿主退出。
- 单实例锁：同一端口重复启动只会复用已有宿主，不会出现两个托盘。
- 日志：`data/logs/supervisor_YYYYMMDD.log`，子进程输出在 `data/logs/ui_stdout.log`。

UI「设置」页也有「重启服务」「退出程序」：会二次确认；若有下载/超分任务在跑，会明确警告并允许取消。确认后页面显示「服务正在重启，N 秒后自动重连」，恢复后自动回到原页面。

`--no-tray`：退回无宿主模式，UI 里这两个按钮会置灰（管理接口返回 409）。

## 15. Docker（含 GPU 与登录硬限制）

### 15.1 构建与启动

```bash
# 准备（容器内也必须有一份 config.yaml）
copy config.example.yaml config.yaml
mkdir data models faces downloads raw secrets

# GPU（Windows 10/11 + WSL2 后端）
docker compose up -d --build

# 纯 CPU
docker build --build-arg BASE=ubuntu:22.04 --build-arg INSTALL_GPU=0 -t facefind:cpu .
docker run -d --name facefind -p 127.0.0.1:8765:8765 ^
  -v "%cd%\config.yaml:/app/config.yaml:ro" ^
  -v "%cd%\data:/app/data" -v "%cd%\models:/app/models" ^
  -v "%cd%\faces:/app/faces" -v "%cd%\downloads:/app/downloads" ^
  -v "%cd%\secrets:/app/secrets" facefind:cpu
```

访问 `http://127.0.0.1:8765/`。端口只发布到回环，避免暴露到局域网。

> GPU 镜像默认用 `requirements-gpu-cu124.txt`，与 `nvidia/cuda:12.4.1-cudnn-runtime` 基础镜像对齐。若把 `BASE` 换成 12.8 的 CUDA 镜像，构建时追加 `--build-arg GPU_REQ=requirements-gpu-cu128.txt`。

### 15.2 Windows 宿主机的 GPU 直通

- **Docker Desktop（WSL2 后端）**：安装最新 NVIDIA Windows 驱动即可，`docker-compose.yml` 里的 `deploy.resources.reservations.devices` 会被识别；`docker run` 用 `--gpus all`。
- **WSL2 内原生 Docker Engine**：先在 WSL 发行版里装 NVIDIA 驱动（不要装 Linux 版驱动，用 Windows 驱动 + WSL 的 `/usr/lib/wsl/lib`），再装 `nvidia-container-toolkit`：

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

最后一条能打印显卡信息，才说明直通成功。

### 15.3 Playwright 在容器里怎么工作

- 镜像里执行了 `playwright install --with-deps chromium`，使用 **自带 Chromium**（容器内一般没有 Chrome/Edge，`browser.channel: ""` 会自动回退到 bundled chromium）。
- 无头模式可用；截图、搜索、详情页抓取都正常。
- 登录态持久化：`data/` 是 volume，`data/browser_profile` 会保留在宿主机。

### 15.4 无 GUI 容器的登录硬限制（重要）

容器里没有桌面，**无法扫码登录小红书/微博**。只有两条可行路径，别指望「在容器里点开登录窗口」：

1. **导出 cookie 文件（推荐）**
   - 在你有桌面的机器上登录站点，从浏览器开发者工具复制 Cookie。
   - 小红书存 `secrets/xhs.txt`：一行 `a1=...; web_session=...; ...`
   - 微博存 `secrets/weibo.txt`：一行，含 `SUB=...`。
   - `secrets/` 已挂载进容器，程序只读。Cookie 过期后重新导出即可。
2. **挂载宿主机已登录的 profile（不保证跨系统可用）**
   - Windows 上先用 UI 登录，生成 `data/browser_profile`，再把整个 `data/` 挂进容器。
   - 注意：Chromium 在 Windows 下用 DPAPI 加密 Cookie，复制到 Linux 容器里通常 **无法解密**，表现为「仍是未登录」。所以这条路只在同系统、同用户环境可靠；跨系统请用方案 1。

如果一定要在容器里扫码：需要自行加 `xvfb` + VNC/noVNC 做虚拟桌面，这不在本仓库默认镜像内，属于额外改造。

### 15.5 端口与数据

- 端口：容器内 `0.0.0.0:8765`，宿主机 `127.0.0.1:8765`。
- 持久化：`data/`（索引、日志、profile、缩略图、回收站）、`models/`、`faces/`、`downloads/`、`raw/`、`secrets/` 全部 volume。
- 模型不随镜像分发：容器首次启动后执行
  `docker exec -it facefind python3 scripts/fetch_models.py --insightface buffalo_l --sr base`。

## 16. 目录结构

```
facefind/
├─ 启动.bat                 # Windows 一键：建 venv / 装依赖 / 下模型 / 托盘启动
├─ start_ui.bat / .ps1      # 已装好环境时的托盘启动脚本
├─ config.example.yaml      # 示例配置（复制为 config.yaml）
├─ requirements*.txt
├─ Dockerfile
├─ docker-compose.yml
├─ docker/entrypoint.sh
├─ scripts/fetch_models.py
├─ facefind/                # 主程序
│  ├─ cli.py  server.py  supervisor.py
│  ├─ face.py framing.py quality.py dedupe.py
│  ├─ pipeline.py index.py exclude.py watcher.py
│  ├─ browser.py  search/（baidu/weibo/xiaohongshu）
│  └─ sr.py export.py settings.py ...
├─ ui/                      # 本地界面（index.html / app.js / style.css）
├─ tools/model_bench.py     # 离线评测脚本（可选）
└─ （运行后生成）
   ├─ config.yaml
   ├─ faces/<人名>/*
   ├─ downloads/<人名>/*
   ├─ data/{index.db,logs,browser_profile,thumbs,recycle,sr...}
   └─ models/{insightface,sr,yolov8n-pose.onnx}
```

## 17. 常见故障

**端口被占用**

- 现象：启动失败，提示「端口 8765 已被其它程序占用」。
- 处理：`netstat -ano | findstr :8765` 找到 PID；或换端口 `python -m facefind ui --port 8766`；托盘模式用 `--port`。
- 若提示「已在运行，复用现有实例」，说明端口上就是 facefind，直接用即可。

**CUDA error 126 / `cublasLt64_*.dll` 找不到**

- 原因：onnxruntime-gpu 与 CUDA/cuDNN 版本不匹配，或 DLL 不在 PATH。
- 处理：先确认装的是与本机 `nvidia-smi` 版本匹配的那套——CUDA 12.0~12.7 用 `requirements-gpu-cu124.txt`，12.8+ 用 `requirements-gpu-cu128.txt`，两套都把 `onnxruntime-gpu` 锁在 1.23.2（CUDA 12.x + cuDNN 9）；再装 `pip install nvidia-cudnn-cu12`、设置 `CUDA_PATH`；仍不行就 `faces.device: cpu` 先跑通。

**cookie 失效 / 一直要求登录**

- 重新「打开登录窗口」扫码；或更新 `secrets/xhs.txt` / `secrets/weibo.txt`。
- 确认没有把浏览器 `channel` 指到不存在的程序。

**遇到 captcha / 滑块 / 访问频繁**

- 程序会停止该站点任务，不会重试绕过。等 30~60 分钟，降低并发与 `detail_budget`，换网络环境后再试。
- 反复触发时优先减少小红书详情页打开次数（`detail_budget` 12 → 6）。

**页面 404 / 白屏 / 缩略图 404**

- `ui/index.html` 找不到：通常是 `config.yaml` 放错目录导致项目根目录判断错误，或从其他目录启动。请从项目根运行，或确认 config 文件在根目录。
- 缩略图 404：原文件被移动/删除；清空 `data/thumbs/` 让程序重建，或刷新索引。
- 图表/总览 HTML 打不开：`downloads/<人名>/overview.html` 里的链接是本地绝对路径，换机器后失效，重新生成即可。

**`insightface` 安装失败**

- Windows 需要 Visual C++ Build Tools（C++ 生成工具）与较新 pip；或改用 Python 3.11 并 `pip install insightface==0.7.3`。

**Playwright 报找不到浏览器**

- 没装 Chrome/Edge 时执行 `python -m playwright install chromium`；或 `browser.channel` 指定已安装的 `chrome` / `msedge`。

**跑完 0 张**

- 先 `python -m facefind faces` 看参考照是否合格；再看日志里的拒绝原因（`无脸`/`人脸过小`/`构图不合格`/`相似度低`/`重复`）。
- 「重复」占多数不是故障：索引里已有大量该人图片，换关键词或调大 `count` 也不会增加。

**托盘图标消失 / 双击没反应**

- 看 `data/logs/supervisor_*.log`；退出后用 `启动.bat` 重新拉起。
- 重启失败会保留托盘并显示「出错」原因（端口被占 / 依赖缺失）。

## 18. 许可与第三方模型

- 本仓库代码：MIT（见 `LICENSE`）。
- **InsightFace 官方模型仅限非商业研究用途**，商用需自行取得授权或替换模型。
- Ultralytics YOLO（姿态模型，可选）：AGPL-3.0 或商业许可。
- Real-ESRGAN / GFPGAN / SwinIR / HAT 等权重各有许可，使用前请阅读对应仓库说明。
- 模型与数据不随仓库分发；`data/`、`faces/`、`downloads/`、`models/`、`secrets/` 均已在 `.gitignore` 中排除。
