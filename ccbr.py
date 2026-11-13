#!/usr/bin/env python3
"""ccbr - Claude Code 세션 / 작업 폴더 백업·복원 도구.

- 로컬 전용: 네트워크, 클라우드, git 을 전혀 사용하지 않는다.
- 파이썬 표준 라이브러리만 사용하는 단일 파일이라 새 컴퓨터로 그대로 들고 갈 수 있다.
- 작업 폴더는 통째로 압축한다. .gitignore / .env / .git 등 어떤 파일도 걸러내지 않는다.

사용법은 `python ccbr.py --help` 또는 README.md 참고.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

VERSION = "1.0.0"
FORMAT = 1
TASK_NAME = "ClaudeCodeBackupRestore"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
WIN_RE = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
# 이미 압축된 형식은 다시 압축하지 않고 그대로 저장한다 (속도 목적, 내용은 동일).
NOCOMP = {".zip", ".7z", ".gz", ".bz2", ".xz", ".zst", ".rar", ".png", ".jpg", ".jpeg", ".gif", ".webp",
          ".mp3", ".mp4", ".mkv", ".mov", ".avi", ".pdf", ".docx", ".xlsx", ".pptx", ".jar", ".whl", ".pack"}

log = logging.getLogger("ccbr")


# ----------------------------------------------------------------------------
# 경로 / 설정
# ----------------------------------------------------------------------------
def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_json_path() -> Path:
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
    return Path.home() / ".claude.json"


def ccbr_home() -> Path:
    return Path(os.environ.get("CCBR_HOME") or Path.home() / ".ccbr")


def desktop_session_roots() -> list[Path]:
    """Claude 데스크톱 앱이 세션 목록 메타데이터(local_*.json)를 두는 폴더들."""
    if os.environ.get("CCBR_DESKTOP_DIR"):
        return [Path(os.environ["CCBR_DESKTOP_DIR"])]
    cands: list[Path] = []
    la, ad = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    if la:
        cands += Path(la).glob("Claude-Data/*/claude-code-sessions")
    if ad:
        cands.append(Path(ad) / "Claude" / "claude-code-sessions")
    cands.append(Path.home() / "Library/Application Support/Claude/claude-code-sessions")
    cands.append(Path.home() / ".config/Claude/claude-code-sessions")
    return [c for c in cands if c.is_dir()]


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def documents_dir() -> Path:
    """사용자의 '문서' 폴더. Windows 에서는 다른 위치로 옮겨(리디렉션) 둔 경우에도 실제 위치를 돌려준다."""
    if os.name == "nt":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:  # 5 = CSIDL_PERSONAL
                return Path(buf.value)
        except Exception:
            pass
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


def load_config() -> dict:
    cfg = load_json(ccbr_home() / "config.json", {})
    cfg.setdefault("dest", str(documents_dir() / "ClaudeCodeBackups"))
    cfg.setdefault("jobs", [])
    return cfg


def save_config(cfg: dict) -> None:
    save_json(ccbr_home() / "config.json", cfg)


def lp(p) -> str:
    """Windows 260자 경로 제한을 피하기 위한 확장 경로."""
    p = os.path.abspath(str(p))
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def sep_of(p: str) -> str:
    return "\\" if (WIN_RE.match(p) or "\\" in p) else "/"


def npath(p) -> str:
    p = str(p)
    if sep_of(p) == "\\":
        return p.replace("/", "\\").rstrip("\\").lower()
    return p.rstrip("/") or "/"


def is_under(p, root) -> bool:
    a, b = npath(p), npath(root)
    return a == b or a.startswith(b + sep_of(str(root)))


def remap(path: str, mappings: list[tuple[str, str]]) -> str:
    """mappings 의 (옛 경로 접두사 -> 새 경로 접두사) 를 적용한다."""
    for old, new in mappings:
        if is_under(path, old):
            rest = str(path).rstrip("\\/")[len(str(old).rstrip("\\/")):]
            rest = rest.replace("\\", os.sep).replace("/", os.sep)
            return str(new).rstrip("\\/") + rest
    return path


def encode_project_dir(cwd: str) -> str:
    """Claude Code 가 ~/.claude/projects 아래에 쓰는 폴더 이름 규칙."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def slug(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', "_", name).strip("._")[:60] or "backup"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def say(*a) -> None:
    print(*a)
    if log.handlers:
        log.info(" ".join(str(x) for x in a))


# ----------------------------------------------------------------------------
# 세션 탐색
# ----------------------------------------------------------------------------
def _json_str(raw: bytes) -> str:
    try:
        return json.loads(b'"' + raw + b'"')
    except ValueError:
        return raw.decode("utf-8", "replace")


def read_session_info(path: Path) -> dict:
    cwd = prompt = title = None
    size = 0
    try:
        with open(lp(path), "rb") as f:
            for i, line in enumerate(f):
                if i >= 300 or (cwd and prompt):
                    break
                if len(line) > 2_000_000:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(o, dict):
                    continue
                if not cwd and isinstance(o.get("cwd"), str):
                    cwd = o["cwd"]
                if not prompt and o.get("type") == "user" and not o.get("isMeta"):
                    c = (o.get("message") or {}).get("content")
                    texts = [c] if isinstance(c, str) else [
                        x.get("text", "") for x in c or [] if isinstance(x, dict) and x.get("type") == "text"]
                    for t in texts:
                        t = t.strip()
                        if t and not t.startswith("<"):
                            prompt = t.splitlines()[0][:80]
                            break
            size = f.seek(0, 2)
            f.seek(max(0, size - 512 * 1024))
            tail = f.read()
        for key in (b"customTitle", b"aiTitle", b"summary"):
            m = re.findall(rb'"' + key + rb'":"((?:[^"\\]|\\.)*)"', tail)
            if m:
                title = _json_str(m[-1])
                break
    except OSError:
        pass
    ct = load_json(path.with_suffix("") / "custom-title.json", {})
    if isinstance(ct, dict) and ct.get("customTitle"):
        title = ct["customTitle"]
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0
    return {"id": path.stem, "cwd": cwd, "project_dir": path.parent.name, "path": str(path),
            "title": title or prompt or "(제목 없음)", "mtime": mtime, "size": size}


def scan_sessions() -> list[dict]:
    out = []
    proj = claude_dir() / "projects"
    if proj.is_dir():
        for pd in proj.iterdir():
            if pd.is_dir():
                for f in pd.glob("*.jsonl"):
                    if UUID_RE.match(f.stem):
                        out.append(read_session_info(f))
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


def desktop_meta_index() -> dict[str, list[Path]]:
    """CLI 세션 ID -> 데스크톱 앱 메타데이터 파일 목록."""
    idx: dict[str, list[Path]] = {}
    for root in desktop_session_roots():
        for f in root.glob("*/*/local_*.json"):
            sid = load_json(f, {}).get("cliSessionId")
            if sid:
                idx.setdefault(sid, []).append(f)
    return idx


def find_session(sessions: list[dict], key: str) -> dict:
    hits = [s for s in sessions if s["id"].lower().startswith(key.lower())]
    if len(hits) != 1:
        raise SystemExit(f"세션 ID '{key}' 에 해당하는 세션이 {len(hits)}개입니다. `ccbr list` 로 확인하세요.")
    return hits[0]


def sessions_for_folder(sessions: list[dict], folder: str) -> list[dict]:
    return [s for s in sessions if s["cwd"] and is_under(s["cwd"], folder)]


# ----------------------------------------------------------------------------
# 파일 순회 (어떤 파일도 제외하지 않는다)
# ----------------------------------------------------------------------------
def walk_tree(root, exclude=()):
    """root 아래 모든 항목을 (kind, abs, rel, extra) 로 낸다. kind: file | dir | link | error.

    .gitignore, .env, .git, node_modules 등 이름·내용에 따른 필터링은 일절 하지 않는다.
    exclude 는 '백업 저장 폴더가 작업 폴더 안에 있는 경우' 자기 자신을 다시 담지 않기 위한 용도뿐이다.
    """
    ex = {os.path.normcase(os.path.abspath(str(e))) for e in exclude}
    stack = [("", str(root))]
    while stack:
        rel, ab = stack.pop()
        try:
            entries = sorted(os.scandir(lp(ab)), key=lambda e: e.name)
        except OSError as e:
            yield "error", ab, rel, str(e)
            continue
        for e in entries:
            r = f"{rel}/{e.name}" if rel else e.name
            a = os.path.join(ab, e.name)
            if os.path.normcase(os.path.abspath(a)) in ex:
                continue
            try:
                junction = getattr(e, "is_junction", lambda: False)()
                if e.is_symlink() or junction:
                    if junction or e.is_dir() or not e.is_file():
                        try:
                            target = os.readlink(lp(a))
                        except OSError:
                            target = ""
                        yield "link", a, r, target  # 폴더 링크/깨진 링크: 따라가지 않고 링크 자체를 기록
                        continue
                    yield "file", a, r, ""  # 파일 심볼릭 링크는 내용을 담는다
                elif e.is_dir(follow_symlinks=False):
                    yield "dir", a, r, ""
                    stack.append((r, a))
                else:
                    yield "file", a, r, ""
            except OSError as err:
                yield "error", a, r, str(err)


def session_sources(s: dict) -> list[tuple[str, str, bool]]:
    """세션 하나에 속한 모든 파일: (절대경로, 아카이브 경로, 실행 중 기록되는 jsonl 여부)."""
    cd, sid, pdn = claude_dir(), s["id"], s["project_dir"]
    pd = cd / "projects" / pdn
    out = [(str(pd / f"{sid}.jsonl"), f"claude/projects/{pdn}/{sid}.jsonl", True)]
    for sub, base in ((pd / sid, f"claude/projects/{pdn}/{sid}"),
                      (cd / "file-history" / sid, f"claude/file-history/{sid}"),
                      (cd / "session-env" / sid, f"claude/session-env/{sid}"),
                      (cd / "tasks" / sid, f"claude/tasks/{sid}")):
        if sub.is_dir():
            out += [(a, f"{base}/{r}", False) for k, a, r, _ in walk_tree(sub) if k == "file"]
    if (cd / "todos").is_dir():
        out += [(str(f), f"claude/todos/{f.name}", False) for f in (cd / "todos").glob(f"{sid}*.json")]
    return out


def memory_sources(pdn: str) -> list[tuple[str, str, bool]]:
    mem = claude_dir() / "projects" / pdn / "memory"
    if not mem.is_dir():
        return []
    return [(a, f"claude/projects/{pdn}/memory/{r}", False) for k, a, r, _ in walk_tree(mem) if k == "file"]


def fingerprint(root, sessions: list[dict], exclude=()) -> str:
    """변경 감지용 지문 (경로·크기·수정시각). 주기 백업에서 변화가 없으면 건너뛰는 데 쓴다."""
    h = hashlib.sha1()

    def feed(tag: str, path: str):
        try:
            st = os.stat(lp(path))
            h.update(f"{tag}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8", "replace"))
        except OSError:
            h.update(f"{tag}|?\n".encode("utf-8", "replace"))

    if root:
        for kind, a, r, extra in walk_tree(root, exclude):
            if kind == "file":
                feed(r, a)
            else:
                h.update(f"{kind}|{r}|{extra}\n".encode("utf-8", "replace"))
    for s in sorted(sessions, key=lambda x: x["id"]):
        for a, arc, _ in session_sources(s):
            feed(arc, a)
    for pdn in sorted({s["project_dir"] for s in sessions}):
        for a, arc, _ in memory_sources(pdn):
            feed(arc, a)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# 백업
# ----------------------------------------------------------------------------
def _add_file(zf: zipfile.ZipFile, src_path: str, arc: str, errors: list, live: bool = False) -> int:
    try:
        with open(lp(src_path), "rb") as src:
            st = os.fstat(src.fileno())
            dt = time.localtime(st.st_mtime)[:6]
            zi = zipfile.ZipInfo(arc, dt if dt[0] >= 1980 else (1980, 1, 1, 0, 0, 0))
            zi.external_attr = (st.st_mode & 0xFFFF) << 16
            zi.compress_type = zipfile.ZIP_STORED if os.path.splitext(arc)[1].lower() in NOCOMP else zipfile.ZIP_DEFLATED
            if live:
                # 실행 중인 세션은 마지막 줄이 쓰이는 도중일 수 있으므로 완결된 줄까지만 담는다.
                data = src.read()
                cut = data.rfind(b"\n")
                data = data[:cut + 1] if cut >= 0 else data
                zf.writestr(zi, data)
                return len(data)
            n = 0
            with zf.open(zi, "w", force_zip64=st.st_size >= (1 << 30)) as dst:
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    dst.write(buf)
                    n += len(buf)
            return n
    except OSError as e:
        errors.append({"path": arc, "error": str(e)})
        return 0


def make_backup(root: str, sessions: list[dict], include_folder: bool, dest: str, label: str,
                auto: bool = False) -> Path:
    """root(작업 폴더) 와 sessions 를 하나의 zip 으로 만든다."""
    root = os.path.abspath(root) if (include_folder or os.path.isdir(root)) else root
    kind = "full" if include_folder else "sessions"
    outdir = Path(dest) / label
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{label}__{kind}{'__auto' if auto else ''}__{time.strftime('%Y%m%d-%H%M%S')}.zip"
    tmp = out.with_name(out.name + ".partial")
    errors: list[dict] = []
    man = {
        "format": FORMAT, "tool": f"ccbr {VERSION}", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": socket.gethostname(), "platform": sys.platform, "home": str(Path.home()),
        "kind": kind, "root": root,
        "workdir": {"included": include_folder, "files": 0, "bytes": 0, "links": []},
        "sessions": [], "memory": [], "desktop": [], "claude_json_project": None, "errors": errors,
    }
    desk = desktop_meta_index()
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=6,
                             strict_timestamps=False) as zf:
            if include_folder:
                if not os.path.isdir(root):
                    raise SystemExit(f"작업 폴더가 없습니다: {root}")
                wd = man["workdir"]
                for k, a, r, extra in walk_tree(root, exclude=[dest]):
                    if k == "file":
                        before = len(errors)
                        n = _add_file(zf, a, "workdir/" + r, errors)
                        if len(errors) == before:
                            wd["files"] += 1
                            wd["bytes"] += n
                    elif k == "dir":
                        zf.mkdir("workdir/" + r)
                    elif k == "link":
                        wd["links"].append({"path": r, "target": extra})
                    else:
                        errors.append({"path": "workdir/" + r, "error": extra})
            pds = {}
            for s in sessions:
                files, size = [], 0
                for a, arc, live in session_sources(s):
                    before = len(errors)
                    size += _add_file(zf, a, arc, errors, live)
                    if len(errors) == before:
                        files.append(arc)
                man["sessions"].append({"id": s["id"], "cwd": s["cwd"], "project_dir": s["project_dir"],
                                        "title": s["title"], "mtime": s["mtime"], "bytes": size, "files": files})
                pds[s["project_dir"]] = s["cwd"]
                for f in desk.get(s["id"], []):
                    arc = f"desktop/{f.parent.parent.name}/{f.parent.name}/{f.name}"
                    before = len(errors)
                    _add_file(zf, str(f), arc, errors)
                    if len(errors) == before:
                        man["desktop"].append({"arc": arc, "session": s["id"]})
            pds.setdefault(encode_project_dir(root), root)
            for pdn, cwd in pds.items():
                files = []
                for a, arc, _ in memory_sources(pdn):
                    before = len(errors)
                    _add_file(zf, a, arc, errors)
                    if len(errors) == before:
                        files.append(arc)
                if files:
                    man["memory"].append({"project_dir": pdn, "cwd": cwd, "files": files})
            # ~/.claude.json 안의 이 프로젝트 전용 설정(허용 도구, 로컬 MCP 서버 등)
            projects = load_json(claude_json_path(), {}).get("projects") or {}
            for key, val in projects.items():
                if npath(key) == npath(root):
                    man["claude_json_project"] = {"key": key, "value": val}
            zf.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=1))
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    wd = man["workdir"]
    say(f"백업 완료: {out}  ({human(out.stat().st_size)})")
    if include_folder:
        say(f"  작업 폴더: {root}  파일 {wd['files']}개 / {human(wd['bytes'])}"
            + (f" / 링크 {len(wd['links'])}개" if wd["links"] else ""))
    say(f"  세션: {len(man['sessions'])}개" + "".join(f"\n    - {s['id'][:8]}  {s['title']}" for s in man["sessions"]))
    if errors:
        say(f"  경고: 읽지 못한 항목 {len(errors)}개 (잠긴 파일 등). `ccbr inspect` 로 목록 확인:")
        for e in errors[:5]:
            say(f"    ! {e['path']}: {e['error']}")
    return out


def backup_sessions(sessions: list[dict], dest: str, auto=False, label_suffix="") -> list[Path]:
    """세션들을 작업 폴더(cwd)별로 묶어, 폴더마다 작업 폴더 전체 + 선택한 세션을 담은 아카이브를 만든다."""
    groups: dict[str, list[dict]] = {}
    for s in sessions:
        if not s["cwd"]:
            raise SystemExit(f"세션 {s['id']} 의 작업 폴더를 알 수 없습니다.")
        groups.setdefault(npath(s["cwd"]), []).append(s)
    outs = []
    for group in groups.values():
        root = group[0]["cwd"]
        label = slug(os.path.basename(root.rstrip("\\/")) + label_suffix)
        with_folder = can_backup_folder(root)
        if not with_folder:
            say(f"작업 폴더는 담지 않고 세션만 백업합니다 "
                f"({'범위가 너무 넓음' if os.path.isdir(root) else '폴더 없음'}): {root}")
        outs.append(make_backup(root, group, with_folder, dest, label, auto))
    return outs


def can_backup_folder(cwd: str) -> bool:
    """작업 폴더를 통째로 담아도 되는가. 없어진 폴더나 너무 넓은 폴더는 세션만 담는다."""
    return os.path.isdir(cwd) and not is_unsafe_root(cwd)


def is_unsafe_root(cwd: str) -> bool:
    """홈 폴더나 드라이브 루트처럼 통째로 백업하기에는 너무 넓은 폴더인가."""
    p = str(cwd).rstrip("\\/")
    return (not p or npath(p) == npath(Path.home()) or bool(re.fullmatch(r"[A-Za-z]:", p))
            or is_under(Path.home(), p))


def plan_all(sessions: list[dict], with_folders: bool = True) -> list[dict]:
    """이 컴퓨터의 모든 세션(과 작업 폴더)을 아카이브 단위로 나눈다.

    - 작업 폴더가 다른 작업 폴더 안에 있으면 바깥 폴더 하나로 합친다 (같은 파일을 두 번 담지 않도록).
    - 폴더가 이미 없거나 홈 폴더/드라이브 루트인 경우에는 그 세션들만 담는다.
    """
    known = [s for s in sessions if s["cwd"]]
    cwds = list({npath(s["cwd"]): s["cwd"] for s in known}.values())
    units: list[dict] = []
    tops: list[str] = []
    if with_folders:
        cand = [c for c in cwds if os.path.isdir(c) and not is_unsafe_root(c)]
        tops = [c for c in cand if not any(npath(o) != npath(c) and is_under(c, o) for o in cand)]
        for t in tops:
            units.append({"root": t, "sessions": [s for s in known if is_under(s["cwd"], t)], "with_folder": True})
    rest: dict[str, list[dict]] = {}
    for s in known:
        if not any(is_under(s["cwd"], t) for t in tops):
            rest.setdefault(npath(s["cwd"]), []).append(s)
    for group in rest.values():
        why = "" if not with_folders else ("폴더 없음" if not os.path.isdir(group[0]["cwd"]) else "범위가 너무 넓음")
        units.append({"root": group[0]["cwd"], "sessions": group, "with_folder": False, "why": why})
    names: dict[str, int] = {}
    for u in units:
        u["label"] = slug(os.path.basename(u["root"].rstrip("\\/")))
        names[u["label"].lower()] = names.get(u["label"].lower(), 0) + 1
    for u in units:  # 이름이 같은 폴더가 여럿이면 경로 해시를 붙여 구분한다
        if names[u["label"].lower()] > 1:
            u["label"] += "-" + hashlib.sha1(npath(u["root"]).encode("utf-8")).hexdigest()[:6]
    return units


def backup_all(dest: str) -> int:
    sessions = scan_sessions()
    units = plan_all(sessions)
    unknown = [s for s in sessions if not s["cwd"]]
    say(f"전체 백업: 아카이브 {len(units)}개 / 세션 {sum(len(u['sessions']) for u in units)}개")
    for u in units:
        say(f"  - {'폴더+세션' if u['with_folder'] else '세션만  '} {u['root']}  (세션 {len(u['sessions'])}개)"
            + (f"  [{u['why']}]" if u.get("why") else ""))
    failures = 0
    for u in units:
        try:
            make_backup(u["root"], u["sessions"], u["with_folder"], dest, u["label"])
        except (Exception, SystemExit) as e:  # 한 폴더의 실패가 나머지를 막지 않게 한다
            failures += 1
            say(f"백업 실패: {u['root']}\n  {e}")
    for s in unknown:
        say(f"건너뜀(작업 폴더를 알 수 없는 세션): {s['id']}")
    say(f"전체 백업 끝: 성공 {len(units) - failures}개, 실패 {failures}개")
    return 1 if failures else 0


# ----------------------------------------------------------------------------
# 복원
# ----------------------------------------------------------------------------
def read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        man = json.loads(zf.read("manifest.json").decode("utf-8"))
    except KeyError:
        raise SystemExit("ccbr 백업 파일이 아닙니다 (manifest.json 없음).")
    if man.get("format", 0) > FORMAT:
        raise SystemExit("이 백업은 더 새로운 버전의 ccbr 로 만들어졌습니다.")
    return man


def _safe_target(base: str, rel: str) -> str:
    if rel.startswith(("/", "\\")) or WIN_RE.match(rel):
        raise SystemExit(f"위험한 경로가 아카이브에 있습니다: {rel}")
    base = os.path.abspath(base)
    target = os.path.abspath(os.path.join(base, *rel.split("/")))
    if target != base and not target.startswith(base.rstrip("\\/") + os.sep):
        raise SystemExit(f"위험한 경로가 아카이브에 있습니다: {rel}")
    return target


def _rewrite_cwd(data: bytes, mappings: list[tuple[str, str]]) -> bytes:
    """JSONL 의 "cwd":"..." 값에서 옛 작업 폴더 경로를 새 경로로 바꾼다 (대화 내용은 건드리지 않는다)."""
    text = data.decode("utf-8", "surrogateescape")
    for old, new in mappings:
        if npath(old) == npath(new) and old == new:
            continue
        o = json.dumps(str(old).rstrip("\\/"), ensure_ascii=False)[1:-1]
        n = json.dumps(str(new).rstrip("\\/"), ensure_ascii=False)[1:-1]
        pat = re.compile(r'("cwd":")' + re.escape(o) + r'(?=["\\/])', re.I if sep_of(old) == "\\" else 0)
        text = pat.sub(lambda m: m.group(1) + n, text)
    return text.encode("utf-8", "surrogateescape")


def _extract(zf: zipfile.ZipFile, arc: str, target: str, transform=None) -> None:
    info = zf.getinfo(arc)
    os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
    if transform:
        with open(lp(target), "wb") as dst:
            dst.write(transform(zf.read(info)))
    else:
        with zf.open(info) as src, open(lp(target), "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
    try:
        mt = time.mktime(info.date_time + (0, 0, -1))
        os.utime(lp(target), (mt, mt))
    except (OSError, ValueError, OverflowError):
        pass


def restore(archive: str, folder_to: str | None = None, only: str | None = None, overwrite=False,
            desktop=False, project_settings=False, dry_run=False, hint=True) -> None:
    with zipfile.ZipFile(archive) as zf:
        man = read_manifest(zf)
        names = set(zf.namelist())
        bad = {e["path"] for e in man.get("errors", [])}
        root_old = man["root"]
        mappings: list[tuple[str, str]] = []
        if folder_to:
            mappings.append((root_old, os.path.abspath(folder_to)))
        old_home, new_home = man.get("home"), str(Path.home())
        if old_home and npath(old_home) != npath(new_home):
            mappings.append((old_home, new_home))  # 사용자 이름이 다른 컴퓨터로 옮긴 경우
        root_new = remap(root_old, mappings)
        changed = [(o, n) for o, n in mappings if npath(o) != npath(n)]
        tag = "[미리보기] " if dry_run else ""

        # 1) 작업 폴더
        wd = man["workdir"]
        if wd["included"] and only != "sessions":
            if os.path.isdir(root_new) and os.listdir(root_new) and not overwrite:
                raise SystemExit(f"복원할 폴더가 이미 있고 비어 있지 않습니다: {root_new}\n"
                                 "  --folder-to 로 다른 위치를 지정하거나, 덮어쓰려면 --overwrite 를 주세요.\n"
                                 "  (--overwrite 는 같은 이름의 파일만 덮어쓰며 다른 파일은 지우지 않습니다.)")
            say(f"{tag}작업 폴더 복원: {root_old} -> {root_new}  (파일 {wd['files']}개)")
            if not dry_run:
                os.makedirs(lp(root_new), exist_ok=True)
                for arc in sorted(n for n in names if n.startswith("workdir/")):
                    rel = arc[len("workdir/"):]
                    if not rel or arc in bad:
                        continue
                    target = _safe_target(root_new, rel.rstrip("/"))
                    if arc.endswith("/"):
                        os.makedirs(lp(target), exist_ok=True)
                    else:
                        _extract(zf, arc, target)
                for link in wd.get("links", []):
                    target = _safe_target(root_new, link["path"])
                    try:
                        if not os.path.lexists(lp(target)):
                            os.makedirs(lp(os.path.dirname(target)), exist_ok=True)
                            os.symlink(link["target"], lp(target), target_is_directory=True)
                    except OSError as e:
                        say(f"  ! 링크를 다시 만들지 못했습니다: {link['path']} -> {link['target']} ({e})")

        # 2) 세션
        if only != "folder":
            cd = claude_dir()
            pdmap: dict[str, str] = {}

            def new_pd(pdn: str, cwd: str | None) -> str:
                if pdn not in pdmap:
                    ncwd = remap(cwd, mappings) if cwd else None
                    pdmap[pdn] = pdn if (not ncwd or npath(ncwd) == npath(cwd)) else encode_project_dir(ncwd)
                return pdmap[pdn]

            def claude_target(arc: str, pdn: str, npd: str) -> str:
                rel = arc[len("claude/"):]
                prefix = f"projects/{pdn}/"
                if rel.startswith(prefix):
                    rel = f"projects/{npd}/" + rel[len(prefix):]
                return _safe_target(str(cd), rel)

            restored = set()
            for s in man["sessions"]:
                npd = new_pd(s["project_dir"], s["cwd"])
                main = f"claude/projects/{s['project_dir']}/{s['id']}.jsonl"
                if os.path.exists(lp(claude_target(main, s["project_dir"], npd))) and not overwrite:
                    say(f"{tag}세션 건너뜀(이미 있음, --overwrite 로 덮어쓰기): {s['id'][:8]}  {s['title']}")
                    continue
                say(f"{tag}세션 복원: {s['id'][:8]}  {s['title']}\n    작업 폴더: {remap(s['cwd'], mappings) if s['cwd'] else '?'}")
                restored.add(s["id"])
                if dry_run:
                    continue
                for arc in s["files"]:
                    if arc in names:
                        tf = (lambda d: _rewrite_cwd(d, changed)) if (changed and arc.endswith(".jsonl")) else None
                        _extract(zf, arc, claude_target(arc, s["project_dir"], npd), tf)
            for m in man.get("memory", []):
                npd = new_pd(m["project_dir"], m["cwd"])
                n = 0
                for arc in m["files"]:
                    target = claude_target(arc, m["project_dir"], npd)
                    if arc in names and (overwrite or not os.path.exists(lp(target))):
                        n += 1
                        if not dry_run:
                            _extract(zf, arc, target)
                if n:
                    say(f"{tag}프로젝트 메모리 복원: 파일 {n}개")

            # 3) 데스크톱 앱 세션 목록 메타데이터 (선택)
            if desktop and man.get("desktop"):
                roots = desktop_session_roots()
                for d in man["desktop"]:
                    if d["session"] not in restored:
                        continue
                    _, acct, ws, fname = d["arc"].split("/")
                    cand = [r / acct / ws for r in roots if (r / acct / ws).is_dir()]
                    if not cand:
                        alld = [p for r in roots for p in r.glob("*/*") if p.is_dir()]
                        cand = alld if len(alld) == 1 else []
                    if not cand:
                        say("  ! 데스크톱 앱 세션 폴더를 특정할 수 없어 메타데이터 복원을 건너뜁니다 "
                            "(세션은 `claude --resume` 으로 이어갈 수 있습니다).")
                        break
                    target = cand[0] / fname
                    if target.exists() and not overwrite:
                        continue
                    say(f"{tag}데스크톱 앱 메타데이터 복원: {fname}")
                    if not dry_run:
                        meta = json.loads(zf.read(d["arc"]).decode("utf-8"))
                        for k in ("cwd", "originCwd", "gitAnchorsFolderRealpath"):
                            if isinstance(meta.get(k), str):
                                meta[k] = remap(meta[k], mappings)
                        save_json(target, meta)

            # 4) ~/.claude.json 의 프로젝트 설정 (선택)
            cj = man.get("claude_json_project")
            if project_settings and cj:
                path = claude_json_path()
                data = load_json(path, None)
                key = remap(root_old, mappings).replace("\\", "/")
                if data is None:
                    say(f"  ! {path} 을 읽을 수 없어 프로젝트 설정 복원을 건너뜁니다.")
                elif any(npath(k) == npath(key) for k in (data.get("projects") or {})) and not overwrite:
                    say("프로젝트 설정 건너뜀(이미 있음).")
                else:
                    say(f"{tag}프로젝트 설정 복원: {path} 의 projects[{key}]")
                    if not dry_run:
                        shutil.copy2(path, str(path) + ".ccbr-bak")
                        data.setdefault("projects", {})[key] = cj["value"]
                        save_json(path, data)
        if not dry_run and hint:
            say("복원 완료." + (f"\n  이어서 작업하려면:  cd \"{root_new}\"  후  claude --resume" if only != "folder" else ""))


def restore_all(src: str, only: str | None = None, overwrite=False, desktop=False, project_settings=False,
                dry_run=False) -> int:
    """백업 저장 폴더의 모든 작업 폴더·세션을 복원한다. 작업 폴더마다 가장 최근 백업을 쓴다."""
    best: dict[tuple[str, str], tuple[str, Path]] = {}
    files = list(Path(src).glob("*/*.zip")) + list(Path(src).glob("*.zip"))
    for f in files:
        try:
            with zipfile.ZipFile(f) as zf:
                man = read_manifest(zf)
            key = (npath(man["root"]), man["kind"])
        except (SystemExit, zipfile.BadZipFile, OSError, ValueError, KeyError) as e:
            say(f"건너뜀(읽을 수 없는 백업): {f}  {e}")
            continue
        if key not in best or man["created_at"] > best[key][0]:
            best[key] = (man["created_at"], f)
    if not best:
        raise SystemExit(f"복원할 백업이 없습니다: {src}")
    # 폴더가 든 백업을 먼저, 세션만 든 백업을 나중에 (더 최근 것부터). 이미 복원된 세션은 건너뛴다.
    order = sorted(best.items(), key=lambda kv: (kv[0][1] != "full", kv[1][0]), reverse=False)
    order = [x for x in order if x[0][1] == "full"] + sorted(
        [x for x in order if x[0][1] != "full"], key=lambda kv: kv[1][0], reverse=True)
    if only == "folder":
        order = [x for x in order if x[0][1] == "full"]
    failures = 0
    say(f"전체 복원: 백업 {len(order)}개  ({src})")
    for (_, kind), (_, f) in order:
        say(f"\n--- {f.name}")
        try:
            restore(str(f), None, only, overwrite, desktop, project_settings, dry_run, hint=False)
        except (Exception, SystemExit) as e:  # 한 백업의 실패가 나머지를 막지 않게 한다
            failures += 1
            say(f"복원하지 못함: {e}")
    say(f"\n전체 복원 끝: 성공 {len(order) - failures}개, 실패/중단 {failures}개"
        + ("\n  이미 있는 폴더는 건드리지 않습니다. 세션만 복원하려면 --only sessions, 덮어쓰려면 --overwrite"
           if failures else "")
        + ("" if dry_run else "\n  이어서 작업하려면 각 작업 폴더에서 claude --resume"))
    return 1 if failures else 0


def inspect(archive: str, verify=False) -> None:
    with zipfile.ZipFile(archive) as zf:
        man = read_manifest(zf)
        wd = man["workdir"]
        print(f"파일     : {archive}  ({human(os.path.getsize(archive))})")
        print(f"생성     : {man['created_at']}  @ {man['host']} ({man['platform']})  {man['tool']}")
        print(f"종류     : {'작업 폴더 + 세션' if wd['included'] else '세션만'}")
        print(f"작업 폴더: {man['root']}" + (f"  파일 {wd['files']}개 / {human(wd['bytes'])}" if wd["included"] else "  (미포함)"))
        print(f"세션     : {len(man['sessions'])}개")
        for s in man["sessions"]:
            print(f"  - {s['id']}  {human(s['bytes'])}  {s['title']}")
        if man.get("memory"):
            print(f"메모리   : 파일 {sum(len(m['files']) for m in man['memory'])}개")
        if man.get("claude_json_project"):
            print("프로젝트 설정(.claude.json): 포함")
        if wd.get("links"):
            print(f"링크     : {len(wd['links'])}개 (폴더 링크는 내용 대신 링크 정보만 저장)")
        if man.get("errors"):
            print(f"읽지 못한 항목 {len(man['errors'])}개:")
            for e in man["errors"]:
                print(f"  ! {e['path']}: {e['error']}")
        if verify:
            badfile = zf.testzip()
            print("무결성 검사: " + ("이상 없음" if badfile is None else f"손상된 항목 발견 - {badfile}"))


# ----------------------------------------------------------------------------
# 주기 백업 (백그라운드)
# ----------------------------------------------------------------------------
def setup_file_log() -> None:
    if log.handlers:
        return
    ccbr_home().mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(ccbr_home() / "ccbr.log", maxBytes=1_000_000, backupCount=3,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def job_desc(j: dict) -> str:
    what = (f"전체 ({'모든 세션' if j.get('sessions_only') else '모든 작업 폴더+세션'})" if j["type"] == "all"
            else f"폴더+관련 세션  {j['target']}" if j["type"] == "folder"
            else f"세션{'+폴더' if j.get('with_folder') else '만'}  {j['target']}")
    keep = f"최근 {j['keep']}개 보관" if j.get("keep") else "무제한 보관"
    return f"{what}  | {j['every']}분마다 | {keep}" + ("  [자동 등록]" if j.get("auto") else "")


def load_state(job_id: str) -> dict:
    """작업별 상태 파일. 주기가 다른 작업이 동시에 돌아도 서로 덮어쓰지 않도록 작업마다 따로 둔다."""
    st = load_json(ccbr_home() / "state" / f"{job_id}.json", None)
    if st is None:
        st = load_json(ccbr_home() / "state.json", {}).get(job_id, {})  # 예전 형식
    return st


def save_state(job_id: str, st: dict) -> None:
    save_json(ccbr_home() / "state" / f"{job_id}.json", st)


def run_jobs(every: int | None = None, force=False) -> int:
    """주기 백업 작업을 지금 실행한다. every 를 주면 그 주기의 작업만 (작업 스케줄러가 주기별로 호출).

    실행 시점은 작업 스케줄러의 트리거가 정한다. 여기서는 따로 시간을 재지 않는다.
    """
    setup_file_log()
    cfg = load_config()
    failures = 0
    sessions = None
    expired: set[str] = set()
    for j in cfg["jobs"]:
        if every is not None and j["every"] != every:
            continue
        st = load_state(j["id"])
        try:
            dest = j.get("dest") or cfg["dest"]
            if sessions is None:
                sessions = scan_sessions()
            if j["type"] == "all":
                # 실행할 때마다 다시 계산하므로 새로 생긴 세션·작업 폴더가 자동으로 포함된다.
                units = [(u["root"], u["sessions"], u["with_folder"], u["label"])
                         for u in plan_all(sessions, not j.get("sessions_only"))]  # sessions_only: 예전 버전의 작업
            elif j["type"] == "folder":  # 예전 버전에서 등록한 작업. 새로 만들 수는 없지만 계속 실행한다.
                root = j["target"]
                units = [(root, sessions_for_folder(sessions, root), True, slug(os.path.basename(root.rstrip("\\/"))))]
            else:
                hits = [x for x in sessions if x["id"] == j["target"]]
                if not hits or not hits[0]["cwd"]:
                    # 훅이 추가한 세션은 첫 메시지 전까지 기록 파일이 없다. 조용히 다음 차례를 기다린다.
                    st["last_check"] = time.time()
                    if j.get("auto") and time.time() - j.get("added", time.time()) > 7 * 86400:
                        expired.add(j["id"])  # 일주일 넘게 기록이 없는 자동 등록 작업은 정리
                    elif not j.get("auto"):
                        st["last_error"] = f"{time.strftime('%Y-%m-%d %H:%M:%S')} 세션 기록을 찾을 수 없음"
                    continue
                s = hits[0]
                units = [(s["cwd"], [s], bool(j.get("with_folder")) and can_backup_folder(s["cwd"]),
                          slug(f"{os.path.basename(s['cwd'].rstrip(chr(92) + '/'))}-{s['id'][:8]}"))]
            st["last_check"] = time.time()
            fps = st.setdefault("fps", {})
            unit_errors = []
            for root, sel, with_folder, label in units:
                try:
                    # 저장 위치도 키에 넣는다: 위치를 바꾸면 변경이 없어도 새 위치에 한 벌을 만든다.
                    key = f"{npath(root)}|{int(with_folder)}|{npath(os.path.abspath(dest))}"
                    fp = fingerprint(root if with_folder else None, sel, exclude=[dest])
                    if fp == fps.get(key) and not force:
                        log.info("변경 없음, 건너뜀: %s", root)
                        continue
                    out = make_backup(root, sel, with_folder, dest, label, auto=True)
                    fps[key] = fp
                    st.update(last_backup=time.time(), last_file=str(out), last_error=None)
                    if j.get("keep"):
                        kind = "full" if with_folder else "sessions"
                        for f in sorted(out.parent.glob(f"{label}__{kind}__auto__*.zip"))[:-j["keep"]]:
                            f.unlink()
                            log.info("오래된 자동 백업 삭제: %s", f)
                except (Exception, SystemExit) as e:  # 한 폴더의 실패가 나머지 폴더를 막지 않게 한다
                    log.exception("백업 실패: %s", root)
                    unit_errors.append(f"{root}: {e}")
            if unit_errors:
                raise RuntimeError("; ".join(unit_errors))
        except (Exception, SystemExit) as e:  # 한 작업의 실패가 다른 작업을 막지 않게 한다
            failures += 1
            st["last_error"] = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {e}"
            log.exception("작업 실패: %s", job_desc(j))
            print(f"작업 실패: {job_desc(j)}\n  {e}")
        finally:
            save_state(j["id"], st)
    if expired:
        fresh = load_config()  # 그사이 훅이 추가한 작업을 잃지 않도록 다시 읽는다
        fresh["jobs"] = [j for j in fresh["jobs"] if j["id"] not in expired]
        save_config(fresh)
        log.info("기록이 생기지 않은 자동 등록 작업 %d개 정리", len(expired))
        sync_tasks(quiet=True)
    return 1 if failures else 0


# ----------------------------------------------------------------------------
# Claude Code 연동 (SessionStart 훅): 새 세션이 열리면 주기 백업 작업에 자동 추가
# ----------------------------------------------------------------------------
def hook_command() -> str:
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" hook run'.replace("\\", "/")


def _is_ccbr_hook(h) -> bool:
    return isinstance(h, dict) and "ccbr.py" in str(h.get("command", "")) and "hook run" in str(h.get("command", ""))


def hook_run() -> None:
    """Claude Code 가 세션 시작 시 호출한다. 표준 출력은 세션 문맥에 들어가므로 아무것도 출력하지 않는다."""
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8-sig", "replace").strip() or "{}")
        sid = data.get("session_id")
        cfg = load_config()
        aa = cfg.get("auto_add")
        if not aa or not isinstance(sid, str) or not UUID_RE.match(sid):
            return
        if any(j["type"] == "session" and j["target"] == sid for j in cfg["jobs"]):
            return
        # 홈 폴더나 드라이브 루트에서 연 세션은 실행 시점에 세션만 담는다 (run_jobs 참고).
        job = {"id": hashlib.sha1(f"session|{sid}|{time.time()}".encode()).hexdigest()[:10], "type": "session",
               "target": sid, "every": aa.get("every", 60), "keep": aa.get("keep", 10),
               "with_folder": True, "auto": True, "added": time.time()}
        cfg["jobs"].append(job)
        save_config(cfg)
        setup_file_log()
        log.info("훅: 새 세션을 주기 백업에 추가 - %s", job_desc(job))
        sync_tasks(quiet=True)
    except Exception:  # 훅 실패가 Claude Code 세션 시작을 방해하면 안 된다
        try:
            setup_file_log()
            log.exception("훅 실행 실패")
        except Exception:
            pass


def hook_install(every: int, keep: int) -> None:
    path = claude_dir() / "settings.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise SystemExit(f"{path} 을 해석할 수 없어 중단합니다: {e}")
        shutil.copy2(path, str(path) + ".ccbr-bak")
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    for g in groups:
        g["hooks"] = [h for h in g.get("hooks", []) if not _is_ccbr_hook(h)]
    groups[:] = [g for g in groups if g.get("hooks")]
    groups.append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]})
    save_json(path, data)
    cfg = load_config()
    cfg["auto_add"] = {"every": max(1, every), "keep": max(0, keep)}
    save_config(cfg)
    print(f"Claude Code SessionStart 훅을 등록했습니다: {path}\n"
          f"  새 세션이 열릴 때마다 '세션 + 작업 폴더' 주기 백업 작업이 추가됩니다 ({every}분마다, "
          f"{'최근 ' + str(keep) + '개 보관' if keep else '무제한 보관'}).\n"
          "  이미 열려 있는 세션에는 적용되지 않으며, 다음에 여는 세션부터 적용됩니다.")


def hook_uninstall() -> None:
    path = claude_dir() / "settings.json"
    data = load_json(path, None)
    n = 0
    if isinstance(data, dict):
        groups = (data.get("hooks") or {}).get("SessionStart") or []
        for g in groups:
            before = len(g.get("hooks", []))
            g["hooks"] = [h for h in g.get("hooks", []) if not _is_ccbr_hook(h)]
            n += before - len(g["hooks"])
        if n:
            data["hooks"]["SessionStart"] = [g for g in groups if g.get("hooks")]
            if not data["hooks"]["SessionStart"]:
                del data["hooks"]["SessionStart"]
            if not data["hooks"]:
                del data["hooks"]
            save_json(path, data)
    cfg = load_config()
    cfg.pop("auto_add", None)
    save_config(cfg)
    print("훅을 제거했습니다." if n else "등록된 훅이 없습니다.", "(이미 추가된 주기 백업 작업은 그대로 남습니다.)")


def hook_status() -> None:
    data = load_json(claude_dir() / "settings.json", {})
    groups = (data.get("hooks") or {}).get("SessionStart") or [] if isinstance(data, dict) else []
    on = any(_is_ccbr_hook(h) for g in groups for h in g.get("hooks", []))
    aa = load_config().get("auto_add")
    print("Claude Code 연동: " + (f"등록됨 - 새 세션마다 '세션 + 작업 폴더' 추가, "
                                f"{aa['every']}분마다" if on and aa else "등록 안 됨  ->  `ccbr hook install`"))


def task_name(every: int) -> str:
    return f"{TASK_NAME}_{every}m"


def tasks_enabled() -> bool:
    return os.name == "nt" and not os.environ.get("CCBR_NO_SCHTASKS")


def existing_tasks() -> set[str]:
    """작업 스케줄러에 등록된 ccbr 작업 이름들 (예전 방식의 단일 점검 작업 포함)."""
    r = subprocess.run(["schtasks", "/Query", "/FO", "CSV", "/NH"], capture_output=True)
    names = set()
    for line in r.stdout.decode("mbcs", "replace").splitlines():
        m = re.match(r'"\\(' + re.escape(TASK_NAME) + r'(?:_\d+m)?)"', line)
        if m:
            names.add(m.group(1))
    return names


def _create_task(every: int) -> None:
    script = os.path.abspath(__file__)
    pyw = Path(sys.executable).with_name("pythonw.exe")  # 콘솔 창 없이 실행
    exe = str(pyw if pyw.exists() else sys.executable)
    user = f"{os.environ.get('USERDOMAIN', socket.gethostname())}\\{getpass.getuser()}"
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Claude Code 세션/작업 폴더 주기 백업 - {every}분마다 (ccbr, 로컬 전용)</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT{every}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{time.strftime('%Y-%m-%dT%H:%M:00')}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>{esc(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{esc(exe)}</Command><Arguments>"{esc(script)}" run --every {every}</Arguments></Exec></Actions>
</Task>"""
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        r = subprocess.run(["schtasks", "/Create", "/TN", task_name(every), "/XML", tmp, "/F"],
                           capture_output=True, text=True)
    finally:
        os.unlink(tmp)
    if r.returncode:
        raise RuntimeError(f"작업 스케줄러 등록 실패 ({task_name(every)}):\n{r.stdout}{r.stderr}")


def sync_tasks(recreate=False, quiet=False) -> None:
    """작업 스케줄러를 작업 목록에 맞춘다: 백업 주기마다 그 주기로 도는 트리거 하나.

    별도의 '점검' 작업은 없다. 트리거 간격이 곧 백업 주기다.
    """
    if not tasks_enabled():
        return
    out = (lambda *a: None) if quiet else print
    want = {task_name(j["every"]): j["every"] for j in load_config()["jobs"]}
    have = existing_tasks()
    for name in sorted(have - set(want)):
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)
        out(f"작업 스케줄러에서 제거: {name}" + ("  (예전 방식의 점검 작업)" if name == TASK_NAME else ""))
    for name, every in sorted(want.items(), key=lambda kv: kv[1]):
        if recreate or name not in have:
            _create_task(every)
            out(f"작업 스케줄러에 등록: {name}  ({every}분마다 실행)")


def schedule_install() -> None:
    if os.name != "nt":
        script = os.path.abspath(__file__)
        print("이 OS 에서는 crontab 에 아래 줄을 추가하세요 (crontab -e):")
        for every in sorted({j["every"] for j in load_config()["jobs"]}):
            when = f"*/{every} * * * *" if every < 60 else f"0 */{max(1, every // 60)} * * *"
            print(f'{when} "{sys.executable}" "{script}" run --every {every}')
        return
    if not load_config()["jobs"]:
        print("등록된 주기 백업 작업이 없습니다.  ->  `ccbr schedule add ...`")
    sync_tasks(recreate=True)
    print(f"  로그: {ccbr_home() / 'ccbr.log'}\n  주의: 이 ccbr.py 파일을 옮기면 `schedule install` 을 다시 실행하세요.")


def schedule_uninstall() -> None:
    if not tasks_enabled():
        print("crontab -e 에서 ccbr 줄을 지우세요.")
        return
    names = existing_tasks()
    for name in sorted(names):
        subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)
        print(f"작업 스케줄러에서 제거: {name}")
    print("백그라운드 실행을 해제했습니다. 작업 목록은 남아 있으며 `schedule install` 로 다시 켤 수 있습니다."
          if names else "등록된 작업이 없습니다.")


def schedule_status() -> None:
    cfg = load_config()
    print(f"백업 저장 위치: {cfg['dest']}")
    if not cfg["jobs"]:
        print("등록된 주기 백업 작업이 없습니다.  ->  `ccbr schedule add ...`")
    have = existing_tasks() if tasks_enabled() else None
    if have and TASK_NAME in have:
        print(f"예전 방식의 점검 작업 '{TASK_NAME}' 이 남아 있습니다.  ->  `ccbr schedule install` 로 정리")
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"
    for i, j in enumerate(cfg["jobs"], 1):
        st = load_state(j["id"])
        bg = "" if have is None else ("   백그라운드: 등록됨" if task_name(j["every"]) in have
                                      else "   백그라운드: 등록 안 됨 -> `ccbr schedule install`")
        print(f"[{i}] {job_desc(j)}\n     마지막 백업: {fmt(st.get('last_backup'))}   마지막 실행: {fmt(st.get('last_check'))}{bg}"
              + (f"\n     마지막 오류: {st['last_error']}" if st.get("last_error") else ""))


def schedule_add(jtype: str, target: str | None, every: int, keep: int) -> None:
    cfg = load_config()
    if jtype != "all" and not target:
        raise SystemExit("세션 ID 를 지정하세요.")
    if jtype == "all":
        if any(j["type"] == "all" for j in cfg["jobs"]):
            raise SystemExit("전체 백업 작업이 이미 등록되어 있습니다. `ccbr schedule list` 로 확인하세요.")
        target = "*"
    else:
        target = find_session(scan_sessions(), target)["id"]
    job = {"id": hashlib.sha1(f"{jtype}|{target}|{time.time()}".encode()).hexdigest()[:10], "type": jtype,
           "target": target, "every": max(1, every), "keep": max(0, keep), "with_folder": jtype == "session"}
    cfg["jobs"].append(job)
    save_config(cfg)
    print(f"추가됨: {job_desc(job)}")
    sync_tasks()


# ----------------------------------------------------------------------------
# 목록 / 대화형 메뉴
# ----------------------------------------------------------------------------
def fmt_session(s: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["mtime"]))
    return f"{s['id'][:8]}  {when}  {human(s['size']):>8}  {s['title'][:50]}\n        {s['cwd'] or '(작업 폴더 알 수 없음)'}"


def folder_list(sessions: list[dict]) -> list[tuple[str, int]]:
    seen: dict[str, list] = {}
    for s in sessions:
        if s["cwd"]:
            seen.setdefault(npath(s["cwd"]), [s["cwd"], 0])[1] += 1
    return [(v[0], v[1]) for v in seen.values()]


def pick(items: list, render, prompt: str, multi=True) -> list:
    if not items:
        print("선택할 항목이 없습니다.")
        return []
    for i, it in enumerate(items, 1):
        print(f"[{i:>2}] {render(it)}")
    raw = input(f"{prompt} ({'예: 1,3-5 / a=전체 / ' if multi else ''}Enter=취소): ").strip().lower()
    if not raw:
        return []
    if raw == "a" and multi:
        return list(items)
    idx: list[int] = []
    try:
        for part in raw.split(","):
            a, _, b = part.strip().partition("-")
            idx += range(int(a), int(b or a) + 1)
        chosen = [items[i - 1] for i in dict.fromkeys(idx) if 1 <= i <= len(items)]
    except ValueError:
        print("입력을 이해하지 못했습니다.")
        return []
    return chosen if multi else chosen[:1]


def clean_path(raw: str) -> str:
    """붙여넣은 경로를 다듬는다: 앞뒤 공백과 따옴표(탐색기의 '경로로 복사')를 떼고 ~ 와 환경 변수를 푼다."""
    raw = raw.strip().strip("\"'").strip()
    return os.path.expandvars(os.path.expanduser(raw)) if raw else ""


def interactive() -> None:
    cfg = load_config()
    while True:
        print(f"\n=== Claude Code 백업·복원 (ccbr {VERSION}) ===   저장 위치: {cfg['dest']}\n"
              "[백업]\n 1) 세션 + 해당 작업 폴더 백업\n 2) 전체 백업 (모든 작업 폴더 + 세션)\n 3) 주기 백업 추가\n"
              "[복원]\n 4) 복원\n 5) 전체 복원\n"
              "[설정]\n 6) 주기 백업 상태\n 7) 저장 위치 변경\n"
              " 0) 종료")
        c = input("선택: ").strip()
        if c == "1":
            sel = pick(scan_sessions(), fmt_session, "백업할 세션")
            if sel:
                backup_sessions(sel, cfg["dest"])
        elif c == "2":
            if input("이 컴퓨터의 모든 작업 폴더와 세션을 백업합니다. 계속할까요? (y/N): ").strip().lower() == "y":
                backup_all(cfg["dest"])
        elif c == "3":
            t = input("무엇을 주기 백업할까요? 1) 세션 + 해당 작업 폴더  2) 전체 : ").strip()
            if t == "1":
                sel = pick(scan_sessions(), fmt_session, "세션", multi=False)
                target = sel[0]["id"] if sel else None
            else:
                target = None
            if target or t == "2":
                every = int(input("주기(분, 기본 60): ").strip() or 60)
                keep = int(input("보관할 자동 백업 개수 (기본 10, 0=무제한): ").strip() or 10)
                schedule_add("all" if t == "2" else "session", target, every, keep)
        elif c == "4":
            archives = sorted(Path(cfg["dest"]).glob("*/*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
            sel = pick(archives, lambda p: f"{p.name}  ({human(p.stat().st_size)})", "복원할 백업", multi=False)
            if sel:
                inspect(str(sel[0]))
                to = input("작업 폴더를 복원할 위치 (Enter=원래 위치): ").strip().strip('"') or None
                ow = input("이미 있는 파일/세션을 덮어쓸까요? (y/N): ").strip().lower() == "y"
                restore(str(sel[0]), folder_to=to, overwrite=ow)
        elif c == "5":
            restore_all(cfg["dest"], dry_run=True)
            if input("위 내용대로 복원할까요? 이미 있는 폴더·세션은 건너뜁니다. (y/N): ").strip().lower() == "y":
                restore_all(cfg["dest"])
        elif c == "6":
            schedule_status()
        elif c == "7":
            raw = input("새 저장 위치 (경로를 붙여넣으세요, Enter=취소): ")
            if not clean_path(raw):
                continue
            path = os.path.abspath(clean_path(raw))
            if os.path.isfile(path):
                print(f"폴더가 아니라 파일입니다: {path}")
                continue
            if not os.path.isdir(path):
                if input(f"폴더가 없습니다. 새로 만들까요? {path} (y/N): ").strip().lower() != "y":
                    continue
                try:
                    os.makedirs(path)
                except OSError as e:
                    print(f"폴더를 만들 수 없습니다: {e}")
                    continue
            cfg = load_config()  # 이 메뉴에서 추가한 주기 백업 작업을 잃지 않도록 다시 읽는다
            cfg["dest"] = path
            save_config(cfg)
            print(f"저장 위치를 바꿨습니다: {path}\n  주기 백업도 이제 이곳에 저장합니다. 기존 백업 파일은 옮기지 않습니다.")
        elif c == "0" or c == "":
            return


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(prog="ccbr", description="Claude Code 세션 / 작업 폴더 백업·복원 도구 (로컬 전용)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="세션 또는 작업 폴더 목록")
    p.add_argument("what", nargs="?", choices=["sessions", "folders"], default="sessions")
    p.add_argument("--folder", help="이 작업 폴더에 속한 세션만")

    p = sub.add_parser("backup", help="백업")
    bs = p.add_subparsers(dest="mode", required=True)
    q = bs.add_parser("session", help="선택한 세션 + 그 세션의 작업 폴더 전체 백업")
    q.add_argument("ids", nargs="+", help="세션 ID (앞부분만 써도 됨)")
    q.add_argument("--dest", help="백업 저장 위치")
    q = bs.add_parser("all", help="이 컴퓨터의 모든 세션과 작업 폴더 백업")
    q.add_argument("--dest", help="백업 저장 위치")

    p = sub.add_parser("inspect", help="백업 파일 내용 보기")
    p.add_argument("archive")
    p.add_argument("--verify", action="store_true", help="압축 무결성(CRC) 검사")

    p = sub.add_parser("restore", help="복원")
    p.add_argument("archive", help="백업 zip 경로, 또는 all = 백업 저장 폴더의 모든 작업 폴더·세션을 최신 백업으로 복원")
    p.add_argument("--from", dest="source", help="(all) 백업들이 들어 있는 폴더 (기본: 설정된 백업 저장 위치)")
    p.add_argument("--folder-to", help="작업 폴더를 복원할 위치 (기본: 원래 경로). 세션의 작업 폴더 정보도 이에 맞춰 바뀜")
    p.add_argument("--only", choices=["sessions", "folder"], help="세션만 / 작업 폴더만 복원")
    p.add_argument("--overwrite", action="store_true", help="이미 있는 파일·세션을 덮어씀")
    p.add_argument("--desktop", action="store_true", help="데스크톱 앱의 세션 목록 메타데이터도 복원 (실험적, 앱 종료 후 사용)")
    p.add_argument("--project-settings", action="store_true", help="~/.claude.json 의 프로젝트 설정도 복원")
    p.add_argument("--dry-run", action="store_true", help="실제로 쓰지 않고 무엇을 할지만 표시")

    p = sub.add_parser("schedule", help="주기 백업 관리")
    ss = p.add_subparsers(dest="action", required=True)
    q = ss.add_parser("add", help="주기 백업 작업 추가")
    q.add_argument("type", choices=["session", "all"], help="session = 세션 + 작업 폴더, all = 전체")
    q.add_argument("target", nargs="?", help="세션 ID (all 은 생략)")
    q.add_argument("--every", type=int, default=60, help="주기(분), 기본 60")
    q.add_argument("--keep", type=int, default=10, help="보관할 자동 백업 개수 (0=무제한), 기본 10")
    ss.add_parser("list", help="작업 및 상태 보기")
    q = ss.add_parser("remove", help="작업 삭제")
    q.add_argument("number", type=int, help="`schedule list` 의 번호")
    ss.add_parser("install", help="백그라운드 실행 (재)등록 - 작업 목록에 맞춰 작업 스케줄러를 다시 만든다")
    ss.add_parser("uninstall", help="백그라운드 실행 해제 (작업 목록은 유지)")

    p = sub.add_parser("run", aliases=["run-due"], help="주기 백업 작업을 지금 실행 (작업 스케줄러가 호출)")
    p.add_argument("--every", type=int, help="이 주기(분)의 작업만 실행")
    p.add_argument("--force", action="store_true", help="변경이 없어도 백업")

    p = sub.add_parser("hook", help="Claude Code 연동: 새 세션을 주기 백업에 자동 추가")
    hs = p.add_subparsers(dest="action", required=True)
    q = hs.add_parser("install", help="SessionStart 훅 등록 (~/.claude/settings.json)")
    q.add_argument("--every", type=int, default=60, help="주기(분), 기본 60")
    q.add_argument("--keep", type=int, default=10, help="보관할 자동 백업 개수 (0=무제한), 기본 10")
    hs.add_parser("uninstall", help="훅 제거")
    hs.add_parser("status", help="연동 상태 보기")
    hs.add_parser("run", help="(Claude Code 가 호출) 표준 입력의 세션 정보를 읽어 작업 추가")

    p = sub.add_parser("config", help="설정 보기/변경")
    p.add_argument("--dest", help="기본 백업 저장 위치")

    a = ap.parse_args(argv)
    cfg = load_config()
    if a.cmd is None:
        interactive()
    elif a.cmd == "list":
        sessions = scan_sessions()
        if a.what == "folders":
            for f, n in folder_list(sessions):
                print(f"{f}  (세션 {n}개){'' if os.path.isdir(f) else '  [폴더 없음]'}")
        else:
            if a.folder:
                sessions = sessions_for_folder(sessions, os.path.abspath(a.folder))
            for s in sessions:
                print(fmt_session(s))
    elif a.cmd == "backup":
        dest = os.path.abspath(a.dest or cfg["dest"])
        if a.mode == "session":
            sessions = scan_sessions()
            backup_sessions([find_session(sessions, i) for i in dict.fromkeys(a.ids)], dest)
        else:
            return backup_all(dest)
    elif a.cmd == "inspect":
        inspect(a.archive, a.verify)
    elif a.cmd == "restore" and a.archive.lower() == "all" and not os.path.isfile(a.archive):
        if a.folder_to:
            raise SystemExit("restore all 에서는 --folder-to 를 쓸 수 없습니다 (각 폴더는 원래 경로로 복원됩니다).")
        return restore_all(os.path.abspath(a.source or cfg["dest"]), a.only, a.overwrite, a.desktop,
                           a.project_settings, a.dry_run)
    elif a.cmd == "restore":
        restore(a.archive, a.folder_to, a.only, a.overwrite, a.desktop, a.project_settings, a.dry_run)
    elif a.cmd == "schedule":
        if a.action == "add":
            schedule_add(a.type, a.target, a.every, a.keep)
        elif a.action == "list":
            schedule_status()
        elif a.action == "remove":
            if not 1 <= a.number <= len(cfg["jobs"]):
                raise SystemExit("그런 번호의 작업이 없습니다.")
            print(f"삭제됨: {job_desc(cfg['jobs'].pop(a.number - 1))}")
            save_config(cfg)
            sync_tasks()
        elif a.action == "install":
            schedule_install()
        else:
            schedule_uninstall()
    elif a.cmd in ("run", "run-due"):
        return run_jobs(a.every, a.force)
    elif a.cmd == "hook":
        if a.action == "install":
            hook_install(a.every, a.keep)
        elif a.action == "uninstall":
            hook_uninstall()
        elif a.action == "status":
            hook_status()
        else:
            hook_run()
    elif a.cmd == "config":
        if a.dest:
            cfg["dest"] = os.path.abspath(clean_path(a.dest))
            save_config(cfg)
        print(f"백업 저장 위치 : {cfg['dest']}\n설정 폴더      : {ccbr_home()}\nClaude 데이터  : {claude_dir()}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
