#!/usr/bin/env python3
"""rotate.py - StarRailCopilot 账号轮换（通用，YAML 驱动）。

读取 accounts.yaml：global 段（src_dir / venv_python / adb_serial / ...）
+ accounts 段（任意数量，顺序即轮换顺序）。
每个账号：关 app -> 启动 src 档案进程 -> 监控日志到结束 -> 杀进程
-> switch 到下一账号。一轮跑完全部账号后退出（timer 次日再触发）。

用法（102 上）:
    python3 rotate.py [--config accounts.yaml]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE / "accounts.yaml"

POLL_SEC = 60
STALE_SEC = 300
TIMEOUT_SEC = 7200
END_MARKERS = ("Request human takeover", "Close game during wait")

cfg_path = DEFAULT_CONFIG


def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def load_config(config: Path) -> dict:
    import yaml
    with open(config, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def masked_of(a: dict) -> str:
    prefix = str(a.get("phone_prefix") or "")
    suffix = str(a.get("phone_suffix") or "")
    return prefix + "*" * max(0, 11 - len(prefix) - len(suffix)) + suffix


def adb(serial: str, *args: str) -> str:
    r = subprocess.run(["adb", "-s", serial, *args], capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def app_running(serial: str, pkg: str) -> bool:
    try:
        pid = adb(serial, "shell", "pidof", pkg)
    except Exception:  # noqa: BLE001
        return False
    return bool(pid.strip())


def force_stop(serial: str, pkg: str) -> None:
    try:
        adb(serial, "shell", "am", "force-stop", pkg)
    except Exception as exc:  # noqa: BLE001
        log(f"force-stop error: {exc}")
    time.sleep(2)


def hsr_cmd(*args: str, timeout: int = 600) -> dict:
    p = subprocess.run(
        [sys.executable, str(BASE / "hsr_account.py"), *args, "--config", str(cfg_path)],
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": p.stderr.strip() or p.stdout.strip()}


def current_account() -> tuple[str | None, dict]:
    out = hsr_cmd("current", timeout=420)
    if not out.get("ok"):
        log(f"current failed: {out.get('error')}")
        return None, {}
    # current() returns both a freshly re-masked `matched` and the config-level
    # `account` (the user-facing name). The rotation order must compare against
    # the config names, so prefer `account` and fall back to `matched`.
    return out.get("account") or out.get("matched"), out


def switch_account(name: str, uid: str | None) -> bool:
    args = ["switch", name]
    if uid:
        args += ["--uid", uid]
    out = hsr_cmd(*args, timeout=600)
    if not out.get("ok"):
        log(f"switch {name} failed: {out.get('error')}")
        return False
    log(f"switch {name} ok uid={out.get('uid')}")
    return True


def latest_log(log_dir: Path, name: str) -> Path | None:
    files = sorted(log_dir.glob(f"*_{name}.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def run_and_monitor(cfg: dict, name: str, profile: str) -> str:
    """关 app -> 启动 src 档案进程 -> 监控日志到结束 -> 杀进程。返回结束原因。"""
    g = cfg.get("global") or {}
    src_dir = Path(g.get("src_dir") or "/home/user/Desktop/StarRailCopilot")
    venv_py = (src_dir / (g.get("venv_python") or ".venv/bin/python")).resolve()
    log_dir = src_dir / (g.get("log_dir") or "log")
    serial = str(g.get("adb_serial") or "192.168.1.110:16132")
    pkg = str(g.get("package") or "com.miHoYo.hkrpg")
    poll = int(g.get("poll_sec") or POLL_SEC)
    stale = int(g.get("stale_sec") or STALE_SEC)
    timeout = int(g.get("timeout_sec") or TIMEOUT_SEC)

    force_stop(serial, pkg)
    # 记录启动前已有日志文件，只读启动后新建文件，避免读到旧的失败标记
    baseline = {p.resolve() for p in log_dir.glob(f"*_{profile}.txt")}
    script = f"{profile}.py"
    cmd = [str(venv_py), script]
    log(f"start {name} ({script}): {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=src_dir, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    start = time.monotonic()
    last_seen = time.time()
    reason = "timeout"
    try:
        while time.monotonic() - start < timeout:
            time.sleep(poll)
            if proc.poll() is not None:
                reason = f"process_exit_{proc.returncode}"
                log(f"{name} process exited rc={proc.returncode}")
                break
            lf = latest_log(log_dir, profile)
            if lf is not None and lf.resolve() not in baseline:
                now_m = lf.stat().st_mtime
                if now_m > last_seen:
                    last_seen = now_m
                try:
                    tail = lf.read_bytes().decode("utf-8", "replace")[-8000:]
                except OSError as exc:  # noqa: BLE001
                    log(f"read log tail error: {exc}")
                    continue
                hit = [m for m in END_MARKERS if m in tail]
                if hit:
                    reason = "end_marker:" + "|".join(hit)
                    log(f"{name} end marker: {reason}")
                    break
            if time.time() - last_seen > stale:
                reason = "stale_no_update"
                log(f"{name} log stale (no update {stale}s)")
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log(f"{name} finished, reason={reason}")
    return reason


def main() -> int:
    global cfg_path
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = ap.parse_args()
    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    accounts = [a for a in (cfg.get("accounts") or []) if isinstance(a, dict) and a.get("name")]
    if not accounts:
        log("no accounts in config")
        return 1
    g = cfg.get("global") or {}
    serial = str(g.get("adb_serial") or "192.168.1.110:16132")
    pkg = str(g.get("package") or "com.miHoYo.hkrpg")

    log("=== starrail-rotate start ===")
    matched, detail = current_account()
    log(f"current account: matched={matched} via={detail.get('via')} "
        f"uid={detail.get('uid')} selected={detail.get('selected_digits')}")
    names = [a["name"] for a in accounts]
    if not matched:
        log("unknown current account -> default to first account")
        start_idx = 0
    elif matched in names:
        start_idx = names.index(matched)
    else:
        log(f"current '{matched}' not in config -> default to first")
        start_idx = 0
    order = [names[(start_idx + i) % len(names)] for i in range(len(names))]
    log(f"rotation order: {order}")

    results: dict[str, str] = {}
    for i, name in enumerate(order):
        acc = accounts[names.index(name)]
        profile = str(acc.get("src_profile") or name)
        log(f"--- run {i + 1}/{len(order)}: {name} (profile={profile}) ---")
        reason = run_and_monitor(cfg, name, profile)
        results[name] = reason
        if app_running(serial, pkg):
            force_stop(serial, pkg)
        if i < len(order) - 1:
            nxt = order[(i + 1) % len(order)]
            nxt_acc = accounts[names.index(nxt)]
            nxt_uid = str(nxt_acc.get("uid") or "") or None
            if not switch_account(nxt, nxt_uid):
                log("switch failed -> abort rotation")
                return 1
    log(f"rotation complete: {json.dumps(results, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
