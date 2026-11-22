#!/usr/bin/env python3
"""init.py - 初始化 StarRailCopilot-AccountSwitch（在 102 上运行）。

流程：
  1. 检查本机工具（python3 / adb / tesseract，缺失则警告）。
  2. 若 accounts.yaml 不存在，从 accounts.example.yaml 复制一份（先编辑）。
  3. 对每个账号智能填充缺失信息：
       - uid 为空  -> 调 hsr_account.py switch <name>，从返回的 in-game UID 填入
       - phone_suffix 为空且 phone_prefix 唯一 -> 从登录页选中账号的脱敏号取尾 2 位填入
         （唯一前缀才能唯一定位该账号；否则要求手动输入）
  4. 写回 accounts.yaml（仅含本机真实值，不提交）。
  5. 生成 systemd user service + timer（starrail-rotate.{service,timer}）。
  6. 安装依赖（pyyaml）。
  7. 默认启用 timer（--no-enable 跳过）。
  8. 生成 src 的 WebUI service（starrailcopilot.service）：
       rotate 假定 gui.py 常驻（配置都在 WebUI 里），没有它换号流程无法改配置。
       默认只在单元不存在时写入 --install-webui-service 才强制覆盖。

用法：
    python3 init.py [--no-enable] [--no-fill] [--install-webui-service]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
HSR = BASE / "hsr_account.py"
EXAMPLE = BASE / "accounts.example.yaml"
CFG = BASE / "accounts.yaml"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def check_tools() -> list[str]:
    missing = [t for t in ("python3", "adb", "tesseract") if shutil.which(t) is None]
    return missing


def ensure_config() -> None:
    if CFG.exists():
        return
    if not EXAMPLE.exists():
        raise SystemExit("accounts.example.yaml missing beside init.py")
    shutil.copy(EXAMPLE, CFG)
    print(f"[init] created {CFG} from example - edit it (global.src_dir, accounts) then rerun")


def load_yaml(path: Path) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: dict) -> None:
    import yaml
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)


def switch_account(name: str) -> tuple[dict, int]:
    p = run([sys.executable, str(HSR), "switch", name, "--config", str(CFG)])
    try:
        out = json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        out = {"ok": False, "error": p.stderr.strip() or p.stdout.strip()}
    return out, p.returncode


def tail_digits(sel: str) -> str:
    digits = "".join(c for c in (sel or "") if c.isdigit())
    return digits[-2:] if len(digits) >= 5 else ""


def smart_fill(accounts: list[dict]) -> bool:
    """填充缺失 uid（switch 识别）与 phone_suffix（唯一前缀 + 屏幕脱敏号尾 2 位）。"""
    changed = False
    for i, a in enumerate(accounts):
        name = str(a.get("name") or f"account{i}")
        prefix = str(a.get("phone_prefix") or "")
        suffix = str(a.get("phone_suffix") or "")
        uid = str(a.get("uid") or "")
        if uid and suffix:
            continue
        if not prefix:
            print(f"[init] {name}: missing phone_prefix - set it manually")
            continue
        print(f"[init] {name}: switching to identify (uid/suffix)...")
        out, rc = switch_account(name)
        if not out.get("ok"):
            print(f"[init]   switch failed: {out.get('error')} - uid/suffix left as-is")
            continue
        if not uid and out.get("uid"):
            a["uid"] = str(out["uid"])
            uid = str(out["uid"])
            changed = True
            print(f"[init]   uid -> {out['uid']}")
        if not suffix:
            uniq_prefix = sum(1 for x in accounts if str(x.get("phone_prefix") or "") == prefix) == 1
            cand = tail_digits(str(out.get("selected_digits") or ""))
            if uniq_prefix and cand:
                ans = input(f"[init]   {name}: inferred phone suffix '{cand}' from screen, keep? [Y/n] ").strip().lower()
                if ans in ("", "y", "yes"):
                    a["phone_suffix"] = cand
                    changed = True
                    print(f"[init]   phone_suffix -> {cand}")
                else:
                    manual = input(f"[init]   {name}: enter phone suffix manually (>=2 digits): ").strip()
                    if manual:
                        a["phone_suffix"] = manual
                        changed = True
            else:
                reason = "prefix not unique" if not uniq_prefix else "OCR too short to read suffix"
                print(f"[init]   {name}: cannot infer unique suffix ({reason}) - set manually")
                manual = input(f"[init]   {name}: enter phone suffix manually (>=2 digits): ").strip()
                if manual:
                    a["phone_suffix"] = manual
                    changed = True
    return changed


def write_systemd(account_count: int, cfg: dict) -> Path:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    rotate = BASE / "rotate.py"
    py = sys.executable
    timer_time = str((cfg.get("global") or {}).get("timer_time") or "04:00")
    service = f"""\
[Unit]
Description=StarRailCopilot account rotation (daily, {account_count} accounts)
After=network.target

[Service]
Type=oneshot
ExecStart={py} {rotate} --config {CFG}
TimeoutStartSec=240min
Nice=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""
    timer = f"""\
[Unit]
Description=StarRailCopilot rotation timer

[Timer]
OnCalendar=*-*-* {timer_time}:00
Persistent=false
RandomizedDelaySec=0

[Install]
WantedBy=timers.target
"""
    (unit_dir / "starrail-rotate.service").write_text(service, encoding="utf-8")
    (unit_dir / "starrail-rotate.timer").write_text(timer, encoding="utf-8")
    print(f"[init] wrote {unit_dir / 'starrail-rotate.service'}")
    print(f"[init] wrote {unit_dir / 'starrail-rotate.timer'}")
    return unit_dir


WEBUI_PORT = 22367


def webui_service(cfg: dict) -> str:
    # Mirror of the unit running on 102 (same fields, paths from accounts.yaml):
    # gui.py is src's web UI; it must stay up because every config/Alas instance
    # lives in it and the rotation only launches <profile>.py against it.
    src_dir = str(Path((cfg.get("global") or {}).get("src_dir") or
                       "/home/user/Desktop/StarRailCopilot").resolve())
    venv_py = str((cfg.get("global") or {}).get("venv_python") or ".venv/bin/python")
    vp = Path(venv_py) if Path(venv_py).is_absolute() else (Path(src_dir) / venv_py)
    gui_log = f"{src_dir}/gui.log"
    return f"""\
[Unit]
Description=StarRailCopilot web UI (Alas launcher)
Documentation=file://{src_dir}/gui.py
After=network.target
PartOf=home-lab.service

[Service]
Type=simple
WorkingDirectory={src_dir}
Environment=PATH={vp.parent}:/usr/local/bin:/usr/bin:/bin
Environment=LANG=C.UTF-8
Environment=LC_ALL=C.UTF-8
Environment=PYTHONUNBUFFERED=1
ExecStart={vp} gui.py -p {WEBUI_PORT}
Restart=on-failure
RestartSec=15
# gui.py respawns itself when it reloads; do not fight it
KillMode=mixed
TimeoutStopSec=20
StandardOutput=append:{gui_log}
StandardError=append:{gui_log}

[Install]
WantedBy=default.target
"""


def write_webui_service(cfg: dict, force: bool) -> None:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    target = unit_dir / "starrailcopilot.service"
    if target.exists() and not force:
        print(f"[init] {target} already exists - keep it (use --install-webui-service to overwrite)")
        return
    target.write_text(webui_service(cfg), encoding="utf-8")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", "starrailcopilot.service"])
    print(f"[init] wrote+enabled {target} (src web UI, port {WEBUI_PORT})")


def enable_timer() -> None:
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", "starrail-rotate.timer"])
    print("[init] systemd timer enabled (starrail-rotate.timer)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-enable", action="store_true", help="do not enable systemd timer")
    ap.add_argument("--no-fill", action="store_true", help="skip device smart-fill (deps+systemd only)")
    ap.add_argument("--install-webui-service", action="store_true",
                    help="write/overwrite starrailcopilot.service (src web UI unit)")
    args = ap.parse_args()

    print("[init] checking tools...")
    missing = check_tools()
    if missing:
        print(f"[init] WARNING missing tools: {', '.join(missing)}")

    ensure_config()
    cfg = load_yaml(CFG)
    accounts = [a for a in (cfg.get("accounts") or []) if isinstance(a, dict) and a.get("name")]
    if not accounts:
        print("[init] no accounts in config - edit accounts.yaml then rerun")
        return 1

    if not args.no_fill:
        if smart_fill(accounts):
            save_yaml(CFG, cfg)
            print(f"[init] saved {CFG}")

    write_systemd(len(accounts), cfg)
    write_webui_service(cfg, args.install_webui_service)
    if not args.no_enable:
        enable_timer()

    print("[init] installing deps...")
    run([sys.executable, "-m", "pip", "install", "-r", str(BASE / "requirements.txt")])
    print("[init] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
