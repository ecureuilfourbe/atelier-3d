"""Exécution des scripts Blender en arrière-plan (blender -b --factory-startup), marqueurs « @@A3D » suivis en direct."""
from __future__ import annotations

import json
import pathlib
import subprocess
import time
from typing import Callable

from .base import Cancelled, StepFailure

CREATE_NO_WINDOW = 0x08000000


def blender_version(exe: str) -> str | None:
    try:
        out = subprocess.run([exe, "--factory-startup", "--version"], capture_output=True, text=True, timeout=60,
                             creationflags=CREATE_NO_WINDOW)
        return out.stdout.splitlines()[0].strip() if out.stdout else None
    except Exception:
        return None


def run_script(exe: str, script: pathlib.Path, args: dict, work_dir: pathlib.Path, *, on_event: Callable[[dict], None],
               should_cancel: Callable[[], bool], log_path: pathlib.Path, timeout_s: float = 3 * 3600) -> dict:
    work_dir.mkdir(parents=True, exist_ok=True)
    args = dict(args)
    args.setdefault("out_dir", str(work_dir))
    args.setdefault("result_path", str(work_dir / f"{script.stem}_result.json"))
    args_path = work_dir / f"{script.stem}_args.json"
    args_path.write_text(json.dumps(args, ensure_ascii=False, indent=2), encoding="utf-8")
    cmd = [exe, "-b", "--factory-startup", "-noaudio", "--python-exit-code", "3", "--python", str(script), "--", str(args_path)]
    t0 = time.time()
    err = None
    with open(log_path, "a", encoding="utf-8", errors="replace") as log:
        log.write(f"\n$ {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW, text=True, encoding="utf-8", errors="replace", bufsize=1)
        try:
            for line in proc.stdout:
                log.write(line)
                if line.startswith("@@A3D "):
                    try:
                        ev = json.loads(line[6:])
                    except ValueError:
                        continue
                    if ev.get("type") == "error":
                        err = ev
                    on_event(ev)
                if should_cancel():
                    proc.kill()
                    raise Cancelled("Annulé pendant l'exécution Blender")
                if time.time() - t0 > timeout_s:
                    proc.kill()
                    raise StepFailure(f"Blender a dépassé {timeout_s:.0f} s", code="timeout")
        finally:
            if proc.poll() is None:
                proc.wait(timeout=30)
    if proc.returncode != 0 or err:
        msg = (err or {}).get("message") or f"Blender s'est terminé avec le code {proc.returncode}"
        raise StepFailure(f"{script.name} : {msg}", code="blender_error", details=err,
                          corrective="Consultez le journal technique de l'étape ; l'entrée n'a pas été modifiée.")
    res_path = pathlib.Path(args["result_path"])
    if not res_path.is_file():
        raise StepFailure(f"{script.name} n'a pas écrit de résultat", code="blender_no_result")
    return json.loads(res_path.read_text(encoding="utf-8"))
