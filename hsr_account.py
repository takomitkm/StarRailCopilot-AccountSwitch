#!/usr/bin/env python3
"""HSR (com.miHoYo.hkrpg) Android multi-account switcher (generic, YAML-driven).

Accounts are loaded from accounts.yaml (see init.py) so any number of accounts
works. Reuses pddcheck's operating rules: explicit ADB serial, strict PNG
decode, focus guard, staged states, OCR-bbox taps with region gates, and exact
masked-identity matching (OKWOW style) instead of row positions.

Usage:
    python3 hsr_account.py probe
    python3 hsr_account.py switch <account-name> [--uid <uid>] [--config accounts.yaml]
    python3 hsr_account.py current [--config accounts.yaml]
    python3 hsr_account.py verify-save

    # `switch` launches the game and returns the detected in-game UID, which
    # init.py uses to backfill accounts.yaml.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageEnhance, ImageOps

SERIAL = "192.168.1.110:16132"
PKG = "com.miHoYo.hkrpg"
ACTIVITY = f"{PKG}/com.mihoyo.combosdk.ComboSDKActivity"
DEBUG_DIR = Path("/home/user/tmp/hsr-switch")
RESULT_PATH = Path("/home/user/tmp/hsr-switch/last-result.json")
W, H = 1280, 720

REGION = {
    "title_click": (500, 630, 780, 700),
    "right_rail": (1150, 440, 1280, 530),
    "rail_wide": (1140, 30, 1280, 350),
    "confirm_title": (450, 250, 850, 340),
    "confirm_button": (690, 365, 830, 425),
    "modal_marker": (480, 470, 780, 660),
    "selected": (500, 265, 710, 325),
    "row1": (500, 380, 710, 435),
    "row2": (500, 495, 710, 555),
    "start_button": (450, 380, 830, 570),
    "uid": (0, 680, 180, 720),
}

UID_RE = re.compile(r"UID\s*[:：]\s*(\d+)")


class HsrError(RuntimeError):
    pass


def adb(*args: str, timeout: int = 25) -> subprocess.CompletedProcess[bytes]:
    argv = ["adb", "-s", SERIAL, *args]
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HsrError(f"adb_failed: {exc}") from exc
    if p.returncode != 0:
        raise HsrError(f"adb rc={p.returncode}: {(p.stderr or p.stdout).decode('utf-8', 'replace')[:300]}")
    return p


def ensure_focus() -> None:
    # The SDK can put a PopupWindow in mCurrentFocus while mFocusedApp still
    # belongs to HSR. Accept either focus line, and ride out launch transitions.
    deadline = time.monotonic() + 15
    text = ""
    while time.monotonic() < deadline:
        text = adb("shell", "dumpsys", "window").stdout.decode("utf-8", "replace")
        lines = re.findall(r"^\s*m(?:CurrentFocus|FocusedApp)=.*$", text, re.M)
        if any(PKG in line for line in lines):
            return
        time.sleep(0.5)
    raise HsrError(f"focus_not_hsr: {text[:300]}")


def capture(tag: str = "") -> Image.Image:
    ensure_focus()
    raw = adb("exec-out", "screencap", "-p", timeout=30).stdout
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.size != (W, H):
        raise HsrError(f"unexpected_frame_size: {img.size}")
    if tag:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        img.save(DEBUG_DIR / f"{int(time.time())}-{tag}.png")
    return img.convert("RGB")


def crop(img: Image.Image, box: tuple[int, int, int, int], *, scale: int = 3,
         contrast: float = 1.0, gray: bool = False) -> Image.Image:
    x0, y0, x1, y1 = box
    out = img.crop(box)
    if gray:
        out = ImageOps.grayscale(out)
    if contrast != 1.0:
        out = ImageEnhance.Contrast(out).enhance(contrast)
    if scale != 1:
        out = out.resize(((x1 - x0) * scale, (y1 - y0) * scale))
    return out


def save_tmp(img: Image.Image) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    p = DEBUG_DIR / f"ocr-{time.time_ns()}.png"
    img.save(p)
    return p


def ocr_rows(img: Image.Image, region: tuple[int, int, int, int], *, lang: str = "chi_sim+eng",
             psm: int = 11, whitelist: str | None = None, conf_min: float = 30.0,
             scale: int = 3, contrast: float = 1.0) -> list[dict[str, Any]]:
    x0, y0, _, _ = region
    sub = crop(img, region, scale=scale, contrast=contrast)
    path = save_tmp(sub)
    argv = ["tesseract", str(path), "stdout", "-l", lang, "--psm", str(psm), "tsv"]
    if whitelist:
        argv += ["-c", f"tessedit_char_whitelist={whitelist}"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=12, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HsrError(f"ocr_failed: {exc}") from exc
    path.unlink(missing_ok=True)
    rows: list[dict[str, Any]] = []
    for line in (p.stdout or "").splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            conf = float(parts[10])
        except ValueError:
            continue
        if conf < conf_min:
            continue
        lx, ty, w, h = (int(float(v)) for v in parts[6:10])
        rows.append({
            "text": parts[11].strip(),
            "conf": conf,
            "x": x0 + lx / scale,
            "y": y0 + ty / scale,
            "w": w / scale,
            "h": h / scale,
        })
    return rows


def joined(rows: list[dict[str, Any]]) -> str:
    # Tesseract splits CJK button labels into single-character rows; group by
    # line (10 px bucket) then left-to-right so "点击进入" reconstructs.
    return "".join(r["text"] for r in sorted(rows, key=lambda r: (int(r["y"]) // 10, r["x"])))


def has(rows: list[dict[str, Any]], needle: str) -> bool:
    return any(needle in r["text"] for r in rows) or needle in joined(rows)


def tap_point(rows: list[dict[str, Any]], needle: str,
              fallback: tuple[int, int]) -> tuple[int, int]:
    hits = [r for r in rows if needle in r["text"]]
    if not hits:
        return fallback
    r = max(hits, key=lambda r: r["conf"] * r["w"])
    return (int(r["x"] + r["w"] / 2), int(r["y"] + r["h"] / 2))


def tap(point: tuple[int, int]) -> None:
    ensure_focus()
    x, y = point
    if not (0 <= x < W and 0 <= y < H):
        raise HsrError(f"tap_out_of_frame: {point}")
    adb("shell", "input", "tap", str(x), str(y))


def digits_of(account: str) -> str:
    return re.sub(r"\D", "", account)


def account_matches(account: str, ocr_digits: str) -> bool:
    """OKWOW-style identity: exact normalized digits first, noise-tolerant fallback.

    The row text is masked like 136********89; Tesseract often reads an extra digit
    where an asterisk sits. Compare the literal prefix/suffix around the mask as a
    fallback, never the row index.
    """
    normalized = digits_of(account)
    ocr_digits = digits_of(ocr_digits)
    if not normalized or not ocr_digits:
        return False
    if normalized == ocr_digits:
        return True
    if "*" not in account:
        return False
    prefix, suffix = account.split("*", 1)[0], account.rsplit("*", 1)[1]
    pre, suf = digits_of(prefix), digits_of(suffix)
    if len(pre) < 2 or len(suf) < 2:
        return False
    return (pre in ocr_digits or pre[-2:] in ocr_digits) and suf in ocr_digits


def read_digits(img: Image.Image, region: tuple[int, int, int, int]) -> str:
    sub = crop(img, region, scale=4, contrast=2.0, gray=True)
    path = save_tmp(sub)
    try:
        p = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", "eng", "--psm", "13",
             "-c", "tessedit_char_whitelist=0123456789"],
            capture_output=True, text=True, timeout=12, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HsrError(f"digit_ocr_failed: {exc}") from exc
    path.unlink(missing_ok=True)
    return digits_of(p.stdout or "")


def wait_until(predicate: Callable[[Image.Image], Any], timeout: float,
               tag: str, *, interval: float = 1.0) -> tuple[Any, Image.Image]:
    deadline = time.monotonic() + timeout
    last: Image.Image | None = None
    while time.monotonic() < deadline:
        last = capture()
        value = predicate(last)
        if value:
            return value, last
        time.sleep(interval)
    raise HsrError(f"timeout_waiting_{tag}")


def is_title(img: Image.Image) -> bool:
    # OCR often misreads 入 as 信; the bottom-center "点击进入" strip is still
    # unambiguous by its 点击 prefix inside this region gate.
    text = joined(ocr_rows(img, REGION["title_click"]))
    return "点击" in text


def is_confirm(img: Image.Image) -> bool:
    text = joined(ocr_rows(img, REGION["confirm_title"]))
    return "确认" in text and "账号" in text


def is_modal(img: Image.Image) -> bool:
    # The "确认切换账号？" dialog also carries a 账号 token; it is not the
    # account-selection modal, so exclude any confirm dialog first.
    if is_confirm(img):
        return False
    # The orange "登录其他账号" label is not OCR-stable; the selected masked
    # account row inside the modal is. In title/in-game states that row is absent.
    return bool(read_digits(img, REGION["selected"])) or has(
        ocr_rows(img, REGION["modal_marker"]), "账号"
    )


def is_launcher(img: Image.Image) -> bool:
    # The white "开始游戏" label on the space background is OCR-hostile; the
    # right-rail buttons are not. Title and modal share some rail labels, so
    # exclude those states (and in-game, which carries a bottom-left UID).
    if is_title(img) or is_modal(img) or is_confirm(img) or uid_of(img):
        return False
    rail = joined(ocr_rows(img, REGION["rail_wide"]))
    return any(word in rail for word in ("设置", "登出", "更新"))


def uid_of(img: Image.Image) -> str | None:
    text = joined(ocr_rows(img, REGION["uid"], psm=7, scale=4, contrast=2.0))
    m = UID_RE.search(text)
    return m.group(1) if m else None


def launch() -> None:
    adb("shell", "am", "force-stop", PKG)
    time.sleep(1.5)
    adb("shell", "am", "start", "-n", ACTIVITY)
    time.sleep(2)


def wait_modal_or_title(timeout: float = 90) -> tuple[str, Image.Image]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        img = capture()
        if is_modal(img):
            return "modal", img
        if is_title(img):
            return "title", img
        if is_launcher(img):
            return "launcher", img
        time.sleep(1)
    raise HsrError("timeout_waiting_modal_or_title")


def wait_account_loaded(timeout: float = 90) -> None:
    # The login modal can appear before its account list is rendered.
    wait_until(lambda im: read_digits(im, REGION["selected"]), timeout, "account_loaded")


def open_login_modal() -> None:
    # Relaunch may restore the login modal directly, or sit on a launcher form
    # ("点击进入" title strip or "开始游戏" with a live session). Either launcher
    # form has a 登出 entry on the right rail; walk it to reach the account modal.
    state, img = wait_modal_or_title()
    if state == "modal":
        wait_account_loaded()
        return
    rail = ocr_rows(img, REGION["right_rail"])
    tap(tap_point(rail, "登出", (1204, 488)))
    # Some launcher forms show a confirm dialog before logging out; others go
    # straight back to the modal. Accept both.
    try:
        _, img = wait_until(is_confirm, 8, "logout_confirm")
    except HsrError:
        pass
    else:
        # The confirm button is a fixed orange box centred near (772, 396); OCR
        # tap_point on the small crop is unreliable here.
        tap((772, 396))
    wait_until(is_modal, 30, "login_modal")
    wait_account_loaded()


def select_account(target: str, *, attempts: int = 8) -> str:
    for attempt in range(attempts):
        img = capture(f"select-try-{attempt}")
        current = read_digits(img, REGION["selected"])
        if account_matches(target, current):
            return current
        if not current:
            # The modal can still be loading or the launcher can be mid-transition;
            # wait it out instead of opening the dropdown against a blank frame.
            time.sleep(3)
            continue
        tap((856, 295))                     # dropdown arrow, gate = is_modal above
        time.sleep(1.2)
        img = capture(f"dropdown-{attempt}")
        candidates: list[tuple[int, int, str]] = []
        for key in ("row1", "row2"):
            region = REGION[key]
            row_digits = read_digits(img, region)
            if account_matches(target, row_digits):
                y = (region[1] + region[3]) // 2
                candidates.append((y, 600, row_digits))
        if not candidates:
            raise HsrError(f"target_not_in_list: {target} (read={current})")
        # Multiple matches: click the lowest row, then READ BACK before login.
        y, x, _ = max(candidates)
        tap((x, y))
        time.sleep(1.2)
        img = capture(f"selected-{attempt}")
        current = read_digits(img, REGION["selected"])
        if account_matches(target, current):
            return current
    raise HsrError(f"selected_account_mismatch_after_{attempts}_attempts")


def switch(target: str, expected_uid: str | None = None) -> dict[str, Any]:
    # The login modal and its account list render asynchronously and can briefly
    # not be ready (empty selected read / account_loaded timeout). Retry the whole
    # launch -> open modal -> select chain a few times before giving up.
    selected: str | None = None
    last_err: HsrError | None = None
    for attempt in range(3):
        try:
            launch()
            open_login_modal()
            selected = select_account(target)
            break
        except HsrError as exc:
            last_err = exc
            print(f"switch attempt {attempt + 1} failed: {exc}", file=sys.stderr)
    else:
        assert last_err is not None
        raise last_err
    assert selected is not None
    img = capture("before-login")
    rows = ocr_rows(img, REGION["modal_marker"])
    enter = tap_point(rows, "进入游戏", (640, 434))
    tap(enter)
    _, img = wait_until(lambda im: is_launcher(im) or is_title(im), 120, "launcher")
    # The "点击进入" strip renders slightly after the launcher rail. Give it a
    # moment; if it never appears this is the "开始游戏" launcher form instead.
    try:
        _, img = wait_until(is_title, 30, "title_strip")
    except HsrError:
        # "开始游戏" form: tapping it boots the session and usually lands on a
        # "点击进入" form that needs one more tap before the game really starts.
        tap((640, 512))
        try:
            _, img = wait_until(is_title, 60, "title_strip_after_start")
        except HsrError:
            pass
        else:
            tap((640, 665))
    else:
        tap((640, 665))
    uid, img = wait_until(uid_of, 240, "in_game_uid")
    result = {
        "ok": expected_uid is None or uid == expected_uid,
        "target": target,
        "selected_digits": selected,
        "uid": uid,
        "expected_uid": expected_uid,
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def probe() -> dict[str, Any]:
    img = capture("probe")
    result: dict[str, Any] = {
        "title": is_title(img),
        "confirm": is_confirm(img),
        "modal": is_modal(img),
        "launcher": is_launcher(img),
        "uid": uid_of(img),
        "selected_digits": read_digits(img, REGION["selected"]),
    }
    return result


def parse_accounts(spec: str) -> dict[str, str | None]:
    """Parse 'masked=uid,masked=uid' into {masked: uid_or_None} for current."""
    accounts: dict[str, str | None] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            masked, uid = part.split("=", 1)
            accounts[masked.strip()] = uid.strip() or None
        else:
            accounts[part] = None
    return accounts


def ensure_hsr_foreground() -> None:
    """Bring HSR to the foreground for capture; start it if not focused."""
    try:
        ensure_focus()
        return
    except HsrError:
        pass
    adb("shell", "am", "start", "-n", ACTIVITY)
    time.sleep(3)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            ensure_focus()
            return
        except HsrError:
            time.sleep(2)
    raise HsrError("focus_not_hsr_after_start")


def current(accounts: dict[str, str | None]) -> dict[str, Any]:
    """Detect which account is logged in on the current HSR instance.

    Matches by in-game UID first (most reliable), then by the masked account
    selected on the login modal. Returns the matched masked account plus state.
    """
    ensure_hsr_foreground()
    img = capture("current")
    state = {
        "title": is_title(img),
        "confirm": is_confirm(img),
        "modal": is_modal(img),
        "launcher": is_launcher(img),
        "uid": uid_of(img),
        "selected_digits": read_digits(img, REGION["selected"]),
    }
    matched: str | None = None
    via = "none"
    uid = state["uid"]
    if uid:
        for masked, account_uid in accounts.items():
            if account_uid and uid == account_uid:
                matched, via = masked, "uid"
                break
        if matched is None:
            via = "uid_unknown"
    else:
        selected = state["selected_digits"]
        if selected:
            for masked in accounts:
                if account_matches(masked, selected):
                    matched, via = masked, "selected"
                    break
    return {**state, "ok": True, "matched": matched, "via": via}


def verify_save() -> dict[str, Any]:
    """Restart without logout; entering game again shows which account persisted."""
    launch()
    _, img = wait_until(is_title, 90, "title_after_restart")
    tap(tap_point(ocr_rows(img, REGION["title_click"]), "点击进入", (640, 665)))
    uid, _ = wait_until(uid_of, 240, "in_game_uid_after_restart")
    return {"ok": True, "uid_after_restart_without_logout": uid,
            "time": datetime.now().isoformat(timespec="seconds")}


DEFAULT_CONFIG = Path(__file__).resolve().with_name("accounts.yaml")


def load_accounts(config: Path | None = None) -> list[dict[str, Any]]:
    """Load accounts from YAML; returns a list of account dicts."""
    path = Path(config) if config else DEFAULT_CONFIG
    if not path.exists():
        raise HsrError(f"accounts config not found: {path} (run init.py first)")
    try:
        import yaml
    except ImportError as exc:
        raise HsrError("pyyaml is required (pip install pyyaml)") from exc
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise HsrError(f"'accounts' must be a list in {path}")
    accounts: list[dict[str, Any]] = []
    for a in raw:
        if isinstance(a, dict) and a.get("name"):
            accounts.append(a)
    if not accounts:
        raise HsrError(f"no accounts defined in {path}")
    return accounts


def account_masked(a: dict[str, Any]) -> str:
    """Derive the masked display string (e.g. 136******89) from prefix/suffix."""
    prefix = str(a.get("phone_prefix") or "")
    suffix = str(a.get("phone_suffix") or "")
    stars = max(0, 11 - len(prefix) - len(suffix))
    return prefix + "*" * stars + suffix


def account_uid(a: dict[str, Any]) -> str | None:
    u = a.get("uid")
    return str(u) if u not in (None, "") else None


def find_account(accounts: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    """Match an account by its name or by its masked identity."""
    for a in accounts:
        if a.get("name") == target or account_masked(a) == target:
            return a
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["probe", "switch", "current", "verify-save"])
    ap.add_argument("target", nargs="?", help="account name (e.g. main) or masked identity")
    ap.add_argument("--uid", help="expected in-game UID (overrides accounts.yaml)")
    ap.add_argument("--accounts", help="masked=uid,masked=uid inline spec (legacy)")
    ap.add_argument("--config", default=None, help="path to accounts.yaml (default: beside this script)")
    args = ap.parse_args()
    if args.cmd == "switch" and not args.target:
        ap.error("switch needs a target account")
    try:
        cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG
        accounts = load_accounts(cfg_path) if args.cmd in ("switch", "current") else []
        if args.cmd == "probe":
            out = probe()
        elif args.cmd == "switch":
            acc = find_account(accounts, args.target)
            if acc is None:
                raise HsrError(f"unknown account target: {args.target}")
            uid = args.uid or account_uid(acc)
            out = switch(account_masked(acc), uid)
            out["account"] = acc.get("name")
        elif args.cmd == "current":
            if args.accounts:
                spec = parse_accounts(args.accounts)
            else:
                spec = {account_masked(a): account_uid(a) for a in accounts}
            out = current(spec)
            if out.get("matched"):
                for a in accounts:
                    if account_masked(a) == out["matched"]:
                        out["account"] = a.get("name")
                        break
        else:
            out = verify_save()
    except HsrError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
