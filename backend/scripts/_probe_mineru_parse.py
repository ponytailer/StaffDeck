"""MinerU 核心功能探针：本地 PDF → 上传 → 轮询 → 下载 zip → 抽 full.md。

用法：uv run --with pymupdf,requests python scripts/_probe_mineru_parse.py [pdf路径]
只做云端解析测试，不改任何业务数据。各阶段计时。
"""
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import requests

PDF_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Users/hs/Downloads/珍岛销售智能体合同.pdf")
BASE = "https://mineru.net"
OUT_DIR = Path("/tmp/mineru_result")

AUTH_MODES = ("bearer_token", "bearer_access_key", "bearer_ak_sk", "x_eb_signature")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in Path(".env").read_text().splitlines():
        line = line.strip()
        if line.startswith("MINERU") or line.startswith("MINUERU"):
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"')
    return env


def auth_headers(mode: str, env: dict[str, str]) -> dict[str, str]:
    ak = env.get("MINERU_ACCESS_KEY", "")
    sk = env.get("MINERU_SECRET_KEY", "")
    if mode == "bearer_token":
        token = env.get("MINUERU_TOKEN") or env.get("MINERU_TOKEN") or ""
        if not token:
            raise RuntimeError("env 缺少 MINUERU_TOKEN")
        return {"Authorization": f"Bearer {token}"}
    if mode == "bearer_access_key":
        return {"Authorization": f"Bearer {ak}"}
    if mode == "bearer_ak_sk":
        return {"Authorization": f"Bearer {ak}:{sk}", "X-Eb-Access-Key": ak, "X-Eb-Secret-Key": sk}
    if mode == "x_eb_signature":
        import hashlib
        import hmac

        ts = str(int(time.time()))
        string_to_sign = f"{ts}"
        sign = hmac.new(sk.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()
        return {"X-Eb-Access-Key": ak, "X-Eb-Signature": sign, "X-Eb-Time-Stamp": ts}
    raise ValueError(mode)


def diagnose_pdf() -> None:
    import pymupdf

    doc = pymupdf.open(PDF_PATH)
    text_chars = sum(len(p.get_text().strip()) for p in doc)
    img_pages = sum(1 for p in doc if p.get_images())
    print(
        f"[文件] {PDF_PATH.name} {PDF_PATH.stat().st_size/1e6:.1f}MB "
        f"{doc.page_count}页 文字层{doc.page_count and text_chars}字符 含图页{img_pages}"
    )
    doc.close()


def request_upload_urls(mode: str, env: dict[str, str], filename: str) -> tuple[str, str]:
    """返回 (batch_id, 预签名上传URL)。"""
    url = f"{BASE}/api/v4/file-urls/batch"
    payload = {
        "enable_formula": True,
        "enable_table": True,
        "language": "ch",
        "files": [{"name": filename, "is_ocr": True, "data_id": "probe-contract"}],
        "model_version": "vlm",
    }
    resp = requests.post(url, headers={**auth_headers(mode, env), "Content-Type": "application/json"}, json=payload, timeout=30)
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    print(f"[申请上传链接 mode={mode}] http={resp.status_code} code={body.get('code')} msg={body.get('msg')}")
    if resp.status_code == 200 and body.get("code") == 0:
        data = body["data"]
        return data["batch_id"], data["file_urls"][0]
    raise RuntimeError(f"申请上传链接失败: {resp.status_code} {body or resp.text[:200]}")


def upload_file(upload_url: str) -> float:
    data = PDF_PATH.read_bytes()
    started = time.time()
    resp = requests.put(upload_url, data=data, timeout=300)
    elapsed = time.time() - started
    print(f"[上传] http={resp.status_code} 耗时 {elapsed:.1f}s")
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"上传失败: {resp.status_code} {resp.text[:200]}")
    return elapsed


def poll_result(mode: str, env: dict[str, str], batch_id: str, timeout_s: int = 600) -> dict:
    url = f"{BASE}/api/v4/extract-results/batch/{batch_id}"
    started = time.time()
    poll_count = 0
    while time.time() - started < timeout_s:
        poll_count += 1
        resp = requests.get(url, headers=auth_headers(mode, env), timeout=30)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code != 200 or body.get("code") != 0:
            raise RuntimeError(f"轮询失败: {resp.status_code} {body or resp.text[:200]}")
        item = body["data"]["extract_result"][0]
        state = item.get("state")
        progress = (item.get("extract_progress") or {}).get("extracted_pages")
        print(f"  poll#{poll_count} state={state} progress={progress} 已等待 {time.time()-started:.0f}s")
        if state == "done":
            item["poll_elapsed"] = time.time() - started
            return item
        if state == "failed":
            raise RuntimeError(f"解析失败: {item.get('err_msg')}")
        time.sleep(5)
    raise RuntimeError(f"轮询超时 {timeout_s}s")


def download_and_extract(item: dict) -> Path:
    zip_url = item["full_zip_url"]
    started = time.time()
    resp = requests.get(zip_url, timeout=300)
    print(f"[下载结果] {len(resp.content)/1e6:.2f}MB 耗时 {time.time()-started:.1f}s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    zf.extractall(OUT_DIR)
    md_candidates = [p for p in OUT_DIR.rglob("*.md")]
    if not md_candidates:
        raise RuntimeError(f"zip 中无 md 文件: {zf.namelist()}")
    return md_candidates[0]


def main() -> int:
    print(f"=== MinerU 云端解析探针 ===")
    diagnose_pdf()
    env = load_env()
    if not (env.get("MINUERU_TOKEN") or env.get("MINERU_TOKEN") or env.get("MINERU_ACCESS_KEY")):
        print("!! .env 缺少 MINUERU_TOKEN / MINERU_ACCESS_KEY")
        return 1

    total_started = time.time()
    last_error: Exception | None = None
    for mode in AUTH_MODES:
        try:
            submit_started = time.time()
            batch_id, upload_url = request_upload_urls(mode, env, PDF_PATH.name)
            submit_elapsed = time.time() - submit_started
            print(f"[batch_id] {batch_id}  提交阶段耗时 {submit_elapsed:.1f}s")
            upload_elapsed = upload_file(upload_url)
            item = poll_result(mode, env, batch_id)
            item["upload_elapsed"] = upload_elapsed
            md_path = download_and_extract(item)
            md_text = md_path.read_text(encoding="utf-8")
            print(
                f"\n=== 成功（auth={mode}）===\n"
                f"上传耗时 {item.get('upload_elapsed', 0):.1f}s | 云端解析 {item.get('poll_elapsed', 0):.1f}s | "
                f"端到端 {time.time()-total_started:.1f}s\n"
                f"markdown {len(md_text)} 字符，输出目录 {OUT_DIR}"
            )
            print("---- markdown 前 800 字 ----")
            print(md_text[:800])
            print("---- end ----")
            return 0
        except Exception as exc:
            last_error = exc
            print(f"[auth={mode} 失败] {exc}\n")
    print(f"全部鉴权方式失败，最后错误: {last_error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
