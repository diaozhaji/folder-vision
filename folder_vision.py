#!/usr/bin/env python3
"""
folder_vision.py — 文件夹图片批量识别流水线
扫描 → 预处理(统一PNG/长边2048) → 串行识别 → SQLite断点续跑 → 汇总

用法:
    python folder_vision.py <目录> [-p 提示词] [-o 输出md] [--recursive] [--db db路径]
    python folder_vision.py <目录> --summary-only    # 只汇总不识别
    python folder_vision.py <目录> --clear-db        # 清空识别记录(db)后退出
"""
import argparse
import base64
import io
import json
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ============ 配置（可用环境变量覆盖，见 README） ============
# FV_LMSTUDIO_BASE: LM Studio 服务地址（默认本机 1234 端口）
# FV_MODEL: 视觉模型名（需已在 LM Studio 中加载）
LMSTUDIO_BASE = os.environ.get("FV_LMSTUDIO_BASE", "http://localhost:1234")
LMSTUDIO_URL = f"{LMSTUDIO_BASE}/v1/chat/completions"
MODELS_URL = f"{LMSTUDIO_BASE}/v1/models"
MODEL = os.environ.get("FV_MODEL", "qwen2.5-vl-7b")
MAX_TOKENS = 8192
TEMPERATURE = 0.1
TIMEOUT = 300          # 单张超时(秒)
RETRIES = 3            # 单张重试次数
MAX_EDGE = 2048        # 长边像素
IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

DEFAULT_PROMPT = (
    "请仔细观察这张设计稿图片，输出结构化识别结果，格式为JSON：\n"
    '{"type": "图片类型(封面/内页/海报等)", "title": "主标题文字", '
    '"texts": ["其他可见文字"], "style": "设计风格描述", '
    '"colors": ["主要配色"], "notes": "其他值得注意的细节"}\n'
    "只输出JSON，不要输出其他内容。"
)


# 显式绕过系统代理(ClashX 会拦 localhost 请求返回 502)
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def ping_models() -> bool:
    """确认 LM Studio 在线且目标模型已加载"""
    try:
        with OPENER.open(MODELS_URL, timeout=5) as r:
            data = json.load(r)
        ids = [m["id"] for m in data.get("data", [])]
        if MODEL in ids:
            return True
        print(f"[FATAL] 模型未加载: {MODEL}。LM Studio 当前在线模型: {ids}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[FATAL] LM Studio 不可达: {e}", file=sys.stderr)
        return False


def normalize_image(path: Path) -> tuple[str, str]:
    """统一转 RGB PNG、长边压缩，返回 (base64, 处理说明)"""
    from PIL import Image
    img = Image.open(path)
    note = []
    if img.mode != "RGB":
        img = img.convert("RGB")
        note.append(f"mode {img.mode}->RGB")
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE))
        note.append(f"resize->{img.size}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return b64, ", ".join(note) if note else "原样"


def call_vision(b64: str, prompt: str) -> dict:
    """单次请求，返回 {content, reasoning, finish, tokens, elapsed}"""
    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{b64}"
                }}
            ]
        }],
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
    }
    body = json.dumps(payload).encode()
    t0 = time.time()
    req = urllib.request.Request(
        LMSTUDIO_URL, data=body,
        headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=TIMEOUT) as r:
        resp = json.load(r)
    ch = resp["choices"][0]
    msg = ch["message"]
    usage = resp.get("usage", {})
    return {
        "content": (msg.get("content") or "").strip(),
        "reasoning": (msg.get("reasoning_content") or "")[:2000],
        "finish": ch.get("finish_reason", ""),
        "tokens": usage.get("completion_tokens", 0),
        "elapsed": round(time.time() - t0, 1),
    }


def clear_db(db_path: Path) -> None:
    """删除识别记录db（下次运行自动重建），先报原记录条数"""
    if not db_path.exists():
        print(f"db不存在，无需清空: {db_path}")
        return
    n = "?"
    try:
        conn = sqlite3.connect(db_path)
        n = conn.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        conn.close()
    except sqlite3.Error:
        pass
    db_path.unlink()
    for suffix in ("-journal", "-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
    print(f"已清空识别记录: {db_path}（共 {n} 条）")


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS results (
        file TEXT PRIMARY KEY, status TEXT, content TEXT,
        reasoning TEXT, finish TEXT, tokens INTEGER,
        elapsed REAL, error TEXT, updated REAL)""")
    conn.commit()
    return conn


def scan(folder: Path, recursive: bool) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in IMG_EXTS)


def run_identify(conn, files: list[Path], prompt: str) -> None:
    done = {r[0] for r in conn.execute("SELECT file FROM results WHERE status='done'")}
    todo = [f for f in files if str(f) not in done]
    print(f"待识别 {len(todo)} / {len(files)} 张（已跳过 {len(files)-len(todo)} 张）")

    for i, f in enumerate(todo, 1):
        fname = str(f)
        print(f"[{i}/{len(todo)}] {f.name} ...", flush=True)
        row = {"file": fname, "status": "error", "content": "", "reasoning": "",
               "finish": "", "tokens": 0, "elapsed": 0, "error": "", "updated": time.time()}
        try:
            b64, note = normalize_image(f)
            if note:
                print(f"    预处理: {note}")
        except Exception as e:
            row["error"] = f"预处理失败: {e}"
            print(f"    ✗ {row['error']}")
        else:
            last_err = None
            for attempt in range(1, RETRIES + 1):
                try:
                    r = call_vision(b64, prompt)
                    if r["finish"] == "length" or not r["content"]:
                        # 假阴性：思维链吃光token，content为空。加大max_tokens重试
                        row["error"] = f"finish={r['finish']} content为空(第{attempt}次)"
                        print(f"    ⚠ {row['error']}")
                        continue
                    row.update(status="done", content=r["content"],
                               reasoning=r["reasoning"], finish=r["finish"],
                               tokens=r["tokens"], elapsed=r["elapsed"], error="")
                    print(f"    ✓ {r['elapsed']}s, {r['tokens']} tokens")
                    break
                except urllib.error.HTTPError as e:
                    last_err = f"HTTP {e.code}: {e.read()[:200].decode(errors='ignore')}"
                    print(f"    ✗ 第{attempt}次 {last_err}")
                    time.sleep(2 * attempt)
                except Exception as e:
                    last_err = str(e)
                    print(f"    ✗ 第{attempt}次 {last_err}")
                    time.sleep(2 * attempt)
            else:
                row["error"] = row["error"] or last_err or "未知错误"
                print(f"    ✗ 最终失败: {row['error']}")

        conn.execute("""INSERT OR REPLACE INTO results
            (file,status,content,reasoning,finish,tokens,elapsed,error,updated)
            VALUES (:file,:status,:content,:reasoning,:finish,:tokens,:elapsed,:error,:updated)""", row)
        conn.commit()


def run_summary(conn, out_path: Path) -> None:
    rows = list(conn.execute(
        "SELECT file,status,content,error,elapsed FROM results ORDER BY file"))
    done = [r for r in rows if r[1] == "done"]
    lines = [
        "# 图片批量识别汇总报告",
        "",
        f"- 总数: {len(rows)}，成功: {len(done)}，失败: {len(rows)-len(done)}",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 模型: {MODEL}",
        "",
    ]
    if rows and rows[0][3]:
        lines += ["## 失败清单", ""]
        for r in rows:
            if r[1] != "done":
                lines.append(f"- **{Path(r[0]).name}**: {r[3]}")
        lines.append("")
    lines += ["## 逐张识别结果", ""]
    for r in rows:
        if r[1] != "done":
            continue
        lines.append(f"### {Path(r[0]).name}")
        lines.append(f"*(耗时 {r[4]}s)*")
        lines.append("")
        lines.append(r[2])
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n汇总报告已写入: {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", help="图片目录（--clear-db 配合 --db 时可省略）")
    ap.add_argument("-p", "--prompt", default=DEFAULT_PROMPT, help="识别提示词")
    ap.add_argument("-o", "--output", default=None, help="汇总输出md路径")
    ap.add_argument("--db", default=None, help="SQLite路径(默认在目录下 .vision.db)")
    ap.add_argument("--recursive", action="store_true", help="递归子目录")
    ap.add_argument("--summary-only", action="store_true", help="只汇总不识别")
    ap.add_argument("--clear-db", action="store_true", help="清空识别记录(删除db)后退出")
    args = ap.parse_args()

    if args.clear_db:
        if args.db:
            db_path = Path(args.db).expanduser().resolve()
        elif args.folder:
            db_path = Path(args.folder).expanduser().resolve() / ".vision.db"
        else:
            sys.exit("--clear-db 需要指定目录或 --db")
        clear_db(db_path)
        return

    if not args.folder:
        sys.exit("缺少图片目录参数（用 --clear-db 清记录时也需指定目录或 --db）")
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"目录不存在: {folder}")

    db_path = Path(args.db).expanduser() if args.db else folder / ".vision.db"
    conn = init_db(db_path)

    if not args.summary_only:
        if not ping_models():
            sys.exit(1)
        files = scan(folder, args.recursive)
        if not files:
            sys.exit("目录中没有图片")
        t0 = time.time()
        run_identify(conn, files, args.prompt)
        print(f"\n识别阶段完成，总耗时 {round(time.time()-t0,1)}s")

    out = Path(args.output).expanduser() if args.output else folder / "识别汇总报告.md"
    run_summary(conn, out)
    conn.close()


if __name__ == "__main__":
    main()
