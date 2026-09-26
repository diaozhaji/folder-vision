# folder-vision

文件夹图片批量识别流水线：扫描 → 预处理（统一 PNG / 长边压缩）→ 调用本地视觉大模型串行识别 → SQLite 断点续跑 → 汇总报告。

基于 LM Studio 的 OpenAI 兼容接口，**图片不离开你的局域网**，适合批量整理设计稿、截图、素材等场景。

## 功能特性

- **多格式支持**：png / jpg / jpeg / webp / bmp，自动统一为 RGB PNG、长边压缩到 2048px，节省 token
- **断点续跑**：识别进度存入 SQLite（默认 `.vision.db`），中断后重跑自动跳过已完成图片
- **失败重试**：单张自动重试 3 次，思维链吃光 token 的假阴性会自动加大配额重试
- **结构化输出**：内置默认提示词，输出 JSON（类型/标题/文字/风格/配色），也可用 `-p` 自定义
- **一键清空**：`--clear-db` 删除识别记录，重新全量识别

## 环境要求

- Python 3.10+
- [LM Studio](https://lmstudio.ai/) 已启动并加载一个**视觉模型**（如 Qwen2.5-VL、Gemma 3 等）
- `pip install pillow`

## 安装

```bash
git clone https://github.com/diaozhaji/folder-vision.git
cd folder-vision
pip install -r requirements.txt
```

## 快速开始

```bash
# 识别 ~/designs 目录下的所有图片
python folder_vision.py ~/designs

# 递归子目录 + 自定义提示词
python folder_vision.py ~/designs --recursive -p "提取这张图的标题和正文文字"

# 指定远程 LM Studio 地址和模型
python folder_vision.py ~/designs --base-url http://192.168.1.100:1234 --model qwen2.5-vl-7b

# 只汇总已识别结果（不调用模型）
python folder_vision.py ~/designs --summary-only

# 清空历史识别记录
python folder_vision.py ~/designs --clear-db
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FV_LMSTUDIO_BASE` | `http://localhost:1234` | LM Studio 服务地址（远程机器请改为对应 IP） |
| `FV_MODEL` | `qwen2.5-vl-7b` | 视觉模型名，需与 LM Studio 中已加载的模型 ID 一致 |

示例（远程 LM Studio）：

```bash
export FV_LMSTUDIO_BASE=http://192.168.1.100:1234
export FV_MODEL=qwen2.5-vl-7b
python folder_vision.py ~/designs
```

## 命令行参数

| 参数 | 说明 |
|---|---|
| `folder` | 图片目录（位置参数） |
| `-p, --prompt` | 识别提示词（默认输出结构化 JSON） |
| `-o, --output` | 汇总报告输出路径（默认目录下 `识别汇总报告.md`） |
| `--db` | SQLite 路径（默认目录下 `.vision.db`） |
| `--recursive` | 递归扫描子目录 |
| `--summary-only` | 只汇总不识别 |
| `--clear-db` | 清空识别记录后退出 |
| `--base-url` | LM Studio 服务地址（默认环境变量 `FV_LMSTUDIO_BASE` 或 `http://localhost:1234`） |
| `--model` | 视觉模型名（默认环境变量 `FV_MODEL` 或 `qwen2.5-vl-7b`） |

命令行参数优先级高于环境变量，可混用：

```bash
python folder_vision.py ~/designs --base-url http://192.168.1.100:1234 --model qwen2.5-vl-7b
```

## 工作原理

```
扫描图片目录 → 预处理（转RGB/压缩） → 逐张调用 LM Studio 视觉模型
     → 结果写入 SQLite（断点续跑） → 生成 Markdown 汇总报告
```

- 已识别（`status=done`）的图片会被跳过，不会重复消耗 token
- 单张失败自动重试 3 次；汇总报告附带失败清单便于排查
