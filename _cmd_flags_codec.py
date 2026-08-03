#!/usr/bin/env python3
"""
CMD_FLAGS.txt codec for textgen-8060s.ps1
Uses the same Python shlex (posix=True) as TextGen modules/shared.py.

I/O: JSON file paths via CLI (never embed user tokens in -c).

  python _cmd_flags_codec.py <request.json> <response.json>
"""
from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


def parse_cmd_flags_text(text: str) -> List[str]:
    """Match TextGen: blank skip, full-line #, trailing \\, join, shlex.split."""
    if text is None:
        text = ""
    parts: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            line = line[:-1].rstrip()
        if line:
            parts.append(line)
    joined = " ".join(parts)
    if not joined.strip():
        return []
    return shlex.split(joined, posix=True)


def join_tokens(tokens: Sequence[str]) -> str:
    # shlex.join is reverse of split for token lists (3.8+)
    if hasattr(shlex, "join"):
        return shlex.join(list(tokens)) + "\n"
    # fallback for very old python
    out = []
    for t in tokens:
        out.append(shlex.quote(t))
    return " ".join(out) + "\n"


def round_trip_ok(tokens: Sequence[str]) -> bool:
    text = join_tokens(tokens)
    back = shlex.split(text.strip(), posix=True)
    return list(back) == list(tokens)


def _is_flag(tok: str) -> bool:
    """True for CLI flags; False for values like -1 or -.5."""
    if not tok or tok == "-":
        return False
    if tok.startswith("--"):
        return len(tok) > 2
    # single-dash: flag if next char is a letter (e.g. -v, -h), not a digit
    if tok.startswith("-") and len(tok) > 1 and tok[1].isalpha():
        return True
    return False


def _split_eq(tok: str) -> Tuple[str, Optional[str]]:
    if tok.startswith("-") and "=" in tok:
        name, val = tok.split("=", 1)
        return name, val
    return tok, None


def _normalize_long_name(name: str) -> str:
    if name.startswith("--") and not name.startswith("---"):
        return "--" + name[2:].replace("_", "-")
    return name


def strip_outer_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def detect_log_file_in_extra_flags(extra_value: str) -> Optional[str]:
    """Exact TextGen-compatible extra-flags log-file detection."""
    if extra_value is None:
        return None
    payload = strip_outer_quotes(str(extra_value).strip())
    if not payload:
        return None

    if payload.startswith("-"):
        try:
            toks = shlex.split(payload, posix=True)
        except ValueError:
            toks = payload.split()
        i = 0
        while i < len(toks):
            t = toks[i]
            name, eqval = _split_eq(t)
            name = _normalize_long_name(name)
            if name == "--log-file":
                if eqval is not None:
                    return eqval
                if i + 1 < len(toks):
                    return toks[i + 1]
                return ""
            i += 1
        return None

    # legacy: comma-separated name=value
    for item in payload.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            k, v = item.split("=", 1)
        else:
            k, v = item, ""
        k = k.strip().lstrip("-").replace("_", "-")
        if k == "log-file":
            return v.strip()
    return None


def find_extra_flags_conflict(tokens: Sequence[str]) -> Optional[str]:
    """
    --extra-flags takes a single string argument (may itself start with '-').
    Also scan remaining tokens if the payload is only a log-file flag name
    (unquoted: --extra-flags --log-file path).
    """
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        name, eqval = _split_eq(t)
        if name == "--extra-flags":
            if eqval is not None:
                val = eqval
                rest: List[str] = []
            elif i + 1 < n:
                val = tokens[i + 1]
                # If value is only a log-file flag token, path may follow as next token
                rest = list(tokens[i + 2 : i + 4])
                i += 1
            else:
                val = ""
                rest = []
            hit = detect_log_file_in_extra_flags(val)
            if hit is not None:
                # empty string means flag present without path in payload
                if hit == "" and rest and not _is_flag(rest[0]):
                    return f"{val} {rest[0]}"
                return val if val else "(empty --log-file)"
            # unquoted multi-token: --extra-flags --log_file path already partly handled
            # also: value is --log-file and we already returned; if value is something else, no conflict
        i += 1
    return None


# alias groups: canonical -> aliases (including canonical)
ALIAS_GROUPS = {
    "--gpu-layers": ["--gpu-layers", "--n-gpu-layers"],
    "--ctx-size": ["--ctx-size", "--n_ctx", "--max_seq_len"],
    "--cache-type": ["--cache-type", "--cache_type"],
}

# map any alias -> canonical
ALIAS_TO_CANON: Dict[str, str] = {}
for canon, aliases in ALIAS_GROUPS.items():
    for a in aliases:
        ALIAS_TO_CANON[_normalize_long_name(a)] = canon
        ALIAS_TO_CANON[a] = canon


FORCE_FLAGS = ["--listen", "--api"]  # bare flags
FORCE_KV = {
    "--listen-host": "0.0.0.0",
    # listen-port, api-port, api-key, loader filled by request
}

DEFAULT_KV = {
    "--gpu-layers": "-1",
    "--ctx-size": "0",
    "--cache-type": "q8_0",
}


def _token_iter(tokens: Sequence[str]):
    """Yield (index, name, value_or_None, consumed_count, is_eq_form)."""
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        name, eqval = _split_eq(t)
        if _is_flag(name):
            if eqval is not None:
                yield i, name, eqval, 1, True
                i += 1
            elif i + 1 < n and not _is_flag(tokens[i + 1]):
                yield i, name, tokens[i + 1], 2, False
                i += 2
            else:
                yield i, name, None, 1, False
                i += 1
        else:
            yield i, t, None, 1, False
            i += 1


def _strip_log_file_from_extra_payload(payload: str) -> str:
    """Remove --log-file / log-file= from an extra-flags payload; return remaining string."""
    payload = strip_outer_quotes((payload or "").strip())
    if not payload:
        return ""
    if payload.startswith("-"):
        try:
            toks = shlex.split(payload, posix=True)
        except ValueError:
            toks = payload.split()
        out: List[str] = []
        i = 0
        while i < len(toks):
            name, eqval = _split_eq(toks[i])
            name_n = _normalize_long_name(name)
            if name_n == "--log-file":
                if eqval is not None:
                    i += 1
                elif i + 1 < len(toks) and not _is_flag(toks[i + 1]):
                    i += 2
                else:
                    i += 1
                continue
            out.append(toks[i])
            i += 1
        if hasattr(shlex, "join"):
            return shlex.join(out)
        return " ".join(shlex.quote(x) for x in out)

    # legacy comma name=value
    parts_out = []
    for item in payload.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            k, _v = item.split("=", 1)
        else:
            k, _v = item, ""
        k_n = k.strip().lstrip("-").replace("_", "-")
        if k_n == "log-file":
            continue
        parts_out.append(item)
    return ",".join(parts_out)


def merge_tokens(
    tokens: List[str],
    *,
    listen_port: int,
    api_port: int,
    api_key: str,
    loader: str = "llama.cpp",
    extensions_add: Optional[List[str]] = None,
    extensions_remove: Optional[List[str]] = None,
    log_file: Optional[str] = None,
    remove_log_file: bool = False,
) -> List[str]:
    force_kv = dict(FORCE_KV)
    force_kv["--listen-port"] = str(listen_port)
    force_kv["--api-port"] = str(api_port)
    force_kv["--api-key"] = str(api_key)
    force_kv["--loader"] = loader

    ext_names: List[str] = []
    seen_ext: set = set()
    alias_values: Dict[str, str] = {}
    other: List[str] = []
    extra_payload: Optional[str] = None

    drop_names = set(FORCE_FLAGS) | set(force_kv.keys()) | set(ALIAS_TO_CANON.keys())
    for al in ALIAS_TO_CANON:
        drop_names.add(al)

    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        name, eqval = _split_eq(t)

        if name == "--extensions":
            vals: List[str] = []
            if eqval is not None:
                vals.append(eqval)
                i += 1
            else:
                i += 1
                while i < n and not _is_flag(tokens[i]):
                    vals.append(tokens[i])
                    i += 1
            for v in vals:
                if v not in seen_ext:
                    seen_ext.add(v)
                    ext_names.append(v)
            continue

        if name == "--extra-flags":
            if eqval is not None:
                extra_payload = eqval
                i += 1
            elif i + 1 < n:
                extra_payload = tokens[i + 1]
                i += 2
            else:
                extra_payload = ""
                i += 1
            continue

        norm = _normalize_long_name(name) if _is_flag(name) else name

        if norm in ALIAS_TO_CANON or name in ALIAS_TO_CANON:
            canon = ALIAS_TO_CANON.get(norm) or ALIAS_TO_CANON.get(name)
            if eqval is not None:
                alias_values[canon] = eqval
                i += 1
            elif i + 1 < n and not _is_flag(tokens[i + 1]):
                alias_values[canon] = tokens[i + 1]
                i += 2
            else:
                i += 1
            continue

        if norm in drop_names or name in drop_names:
            if eqval is not None:
                i += 1
            elif name in FORCE_FLAGS or norm in FORCE_FLAGS:
                i += 1
            elif i + 1 < n and not _is_flag(tokens[i + 1]):
                i += 2
            else:
                i += 1
            continue

        if _is_flag(name) and eqval is None and i + 1 < n and not _is_flag(tokens[i + 1]):
            other.append(tokens[i])
            other.append(tokens[i + 1])
            i += 2
        else:
            other.append(tokens[i])
            i += 1

    if extensions_remove:
        rem = set(extensions_remove)
        ext_names = [e for e in ext_names if e not in rem]
    if extensions_add:
        for e in extensions_add:
            if e not in ext_names:
                ext_names.append(e)

    # Manage log-file inside --extra-flags. Keep the payload style when one
    # already exists. For a new payload use TextGen's documented legacy form
    # (log-file=...), which works with old and new portable releases.
    if extra_payload is not None:
        extra_payload = _strip_log_file_from_extra_payload(extra_payload)
    if remove_log_file:
        pass  # already stripped
    elif log_file:
        lf = str(log_file).strip().replace("\\", "/")
        if extra_payload and extra_payload.strip().startswith("-"):
            # A user already chose TextGen's newer literal style.
            extra_payload = (extra_payload.strip() + f" --log-file {shlex.quote(lf)}").strip()
        elif extra_payload and extra_payload.strip():
            # Legacy comma-separated name=value payload.
            extra_payload = f"{extra_payload.strip()},log-file={lf}"
        else:
            extra_payload = f"log-file={lf}"
    if extra_payload is not None and not str(extra_payload).strip():
        extra_payload = None

    out: List[str] = []
    for f in FORCE_FLAGS:
        out.append(f)
    for k, v in force_kv.items():
        out.append(k)
        out.append(v)

    for canon, default in DEFAULT_KV.items():
        if canon in alias_values:
            out.append(canon)
            out.append(alias_values[canon])
        else:
            out.append(canon)
            out.append(default)

    if ext_names:
        out.append("--extensions")
        out.extend(ext_names)

    if extra_payload is not None:
        out.append("--extra-flags")
        out.append(extra_payload)

    out.extend(other)
    return out


def handle(req: Dict[str, Any]) -> Dict[str, Any]:
    op = req.get("op")
    if op == "selftest":
        samples = [
            ["--api-key", "sk-local"],
            ["--extra-flags", '--log-file "C:\\path with spaces\\a.log"'],
            ["--path", r"C:\dir with spaces\file"],
            ["--flag=value"],
            ["--gpu-layers", "-1"],
            ["--extensions", "a", "b"],
            ["--msg", "it's"],
            ["--empty", ""],
        ]
        results = []
        for s in samples:
            ok = round_trip_ok(s)
            results.append({"tokens": s, "ok": ok})
        return {"ok": all(r["ok"] for r in results), "results": results}

    if op == "parse":
        text = req.get("text") or ""
        tokens = parse_cmd_flags_text(text)
        return {"ok": True, "tokens": tokens}

    if op == "join":
        tokens = list(req.get("tokens") or [])
        if not round_trip_ok(tokens):
            return {"ok": False, "error": "round-trip failed before join"}
        return {"ok": True, "text": join_tokens(tokens), "tokens": tokens}

    if op == "check_log_file_conflict":
        text = req.get("text") or ""
        tokens = parse_cmd_flags_text(text)
        conflict = find_extra_flags_conflict(tokens)
        return {
            "ok": True,
            "conflict": conflict is not None,
            "value": conflict,
            "tokens": tokens,
        }

    if op == "merge":
        text = req.get("text") or ""
        tokens = parse_cmd_flags_text(text)
        listen_port = int(req.get("listen_port", 7860))
        api_port = int(req.get("api_port", 5000))
        api_key = str(req.get("api_key", "sk-local"))
        loader = str(req.get("loader", "llama.cpp"))
        def _as_list(v):
            if v is None:
                return None
            if isinstance(v, list):
                return v
            return [v]

        extensions_add = _as_list(req.get("extensions_add"))
        extensions_remove = _as_list(req.get("extensions_remove"))
        log_file = req.get("log_file")
        if log_file is not None:
            log_file = str(log_file).strip() or None
        remove_log_file = bool(req.get("remove_log_file"))
        # Foreign log-file before merge (different path) — report; merge still rewrites to ours when log_file set
        pre_conflict = find_extra_flags_conflict(tokens)
        pre_path = None
        if pre_conflict is not None:
            pre_path = detect_log_file_in_extra_flags(
                pre_conflict if str(pre_conflict).lstrip().startswith("-") else f"--log-file {pre_conflict}"
            )
            if pre_path is None:
                pre_path = detect_log_file_in_extra_flags(str(pre_conflict))

        merged = merge_tokens(
            tokens,
            listen_port=listen_port,
            api_port=api_port,
            api_key=api_key,
            loader=loader,
            extensions_add=extensions_add,
            extensions_remove=extensions_remove,
            log_file=log_file,
            remove_log_file=remove_log_file,
        )

        if not round_trip_ok(merged):
            return {"ok": False, "error": "round-trip failed after merge", "tokens": merged}
        out_text = join_tokens(merged)
        changed = tokens != merged

        foreign = False
        foreign_val = None
        if pre_path and log_file:
            ours = log_file.replace("\\", "/").rstrip("/")
            theirs = pre_path.replace("\\", "/").rstrip("/")
            if theirs and theirs != ours:
                foreign = True
                foreign_val = pre_path
        elif pre_path and not log_file and not remove_log_file:
            foreign = True
            foreign_val = pre_path

        return {
            "ok": True,
            "text": out_text,
            "tokens": merged,
            "changed": changed,
            "conflict": foreign,
            "conflict_value": foreign_val,
        }

    return {"ok": False, "error": f"unknown op: {op}"}


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print("usage: _cmd_flags_codec.py <request.json> <response.json>", file=sys.stderr)
        return 2
    req_path = Path(argv[1])
    resp_path = Path(argv[2])
    try:
        req = json.loads(req_path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        resp_path.write_text(
            json.dumps({"ok": False, "error": f"bad request json: {e}"}),
            encoding="utf-8",
        )
        return 1
    try:
        resp = handle(req)
    except Exception as e:
        resp = {"ok": False, "error": str(e)}
    resp_path.write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
