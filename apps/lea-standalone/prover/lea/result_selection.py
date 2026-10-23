"""Compiler-selected result identity and transitive trust, independent of storage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import os
import re

from . import execution

ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
INSPECTOR = Path(__file__).with_name("LibraryInspect.lean")


def inspect_file(path: str | Path, *, allow_errors=False) -> dict:
    path = Path(path).resolve()
    code = path.read_bytes().decode('utf-8')
    root = next((p for p in path.parents if (p / "lakefile.lean").exists()
                 or (p / "lakefile.toml").exists()), path.parent)
    policy = execution.current() or {}
    scratch = (Path(policy["temporary_root"]) / "inspection" if policy.get("temporary_root")
               else path.parent / ".lea" / "inspection")
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temp:
        source = Path(temp) / "Inspect.lean"
        module = None
        standalone_artifact = None
        inspection_env = None
        if path.is_relative_to(root / 'proofs'):
            from .tools import rebuild_module, _lean_check_has_error
            built = rebuild_module(str(path))
            if not _lean_check_has_error(built):
                module = '.'.join(path.relative_to(root / 'proofs').with_suffix('').parts)
            elif not allow_errors:
                if 'unknown target' not in built:
                    raise ValueError('The actual selected module did not build: ' + built[-5000:])
                # Loose chat files live below a session UUID, outside Lake's
                # registered lean_lib roots. Compile the original file unchanged,
                # then import its actual artifact into the inspector. Adding
                # `import Lean` to the submission would change its environment.
                parts = path.relative_to(root).with_suffix('').parts
                if any('«' in p or '»' in p for p in parts):
                    raise ValueError('Unsupported module path for result inspection')
                module = '.'.join(parts)
                compiled = Path(temp) / 'compiled'
                standalone_artifact = compiled.joinpath(*parts[:-1], parts[-1] + '.olean')
                standalone_artifact.parent.mkdir(parents=True)
                built_proc = execution.run(['lake','env','lean','-o',str(standalone_artifact),str(path)],
                    cwd=root,capture_output=True,text=True,timeout=120)
                if built_proc.returncode:
                    raise ValueError('Original file compilation failed: ' + (built_proc.stdout + built_proc.stderr)[-5000:])
                inspection_env = {**os.environ, 'LEAN_PATH':str(compiled) + os.pathsep + os.environ.get('LEAN_PATH','')}
        if module:
            # Inspect the actual compiled module. Recompiling altered source with
            # an extra `import Lean` could change metaprogram behavior and private
            # names, and is not evidence about the real result.
            import_name = '.'.join('«' + p + '»' for p in parts) if standalone_artifact else module
            display_module = ('.'.join(p if re.fullmatch(r"[^\W\d][\w']*", p) else '«' + p + '»'
                                      for p in parts) if standalone_artifact else module)
            source.write_text('import Lean\nimport ' + import_name + '\n' +
                INSPECTOR.read_text().removeprefix('import Lean\n') +
                '\n#lea_inspect_module ' + json.dumps(display_module) + '\n')
            from .check_evidence import ReadSnapshot, _artifact_family
            artifacts = ReadSnapshot()
            compiled_artifacts = {}
            if standalone_artifact:
                compiled_artifacts[str(standalone_artifact)] = artifacts.observation(str(standalone_artifact))[0]
            for directory in ('lib/lean', 'ir'):
                stem = root / '.lake/build' / directory / Path(*module.split('.'))
                family = _artifact_family(stem, {})
                artifacts.artifact_sets[str(stem)] = family
                compiled_artifacts.update({p: artifacts.observation(p)[0] for p in family})
        else:
            source.write_text("import Lean\n" + code + "\n" +
                INSPECTOR.read_text().removeprefix("import Lean\n") +
                f"\n#lea_inspect_source {len(code.splitlines()) + 1}\n")
        proc = execution.run(["lake", "env", "lean", str(source)] if root != path.parent
                             or (root / "lakefile.lean").exists() or (root / "lakefile.toml").exists()
                             else ["lean", str(source)], cwd=root,
                             capture_output=True, text=True, timeout=120, **({'env':inspection_env} if inspection_env else {}))
        if module and (not compiled_artifacts or not artifacts.unchanged()):
            raise ValueError('Compiled module changed during result inspection')
    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode and not allow_errors:
        raise ValueError("Result inspection failed: " + output[-5000:])
    marker = "LEA_LIBRARY_JSON:"
    if marker not in output:
        raise ValueError("Compiler produced no result inspection evidence: " + output[-2000:])
    declarations = json.JSONDecoder().raw_decode(output.rsplit(marker, 1)[1].lstrip())[0]
    for decl in declarations:
        if decl.get("range") and not module:
            decl["range"] = {**decl["range"],
                "start_line": decl["range"]["start_line"] - 1,
                "end_line": decl["range"]["end_line"] - 1}
    source_names = {d['full_name']: d['user_name'] for d in declarations}
    for decl in declarations:
        decl['dependencies'] = [source_names.get(n, n) for n in decl.get('dependencies', [])]
    if path.read_bytes().decode('utf-8') != code:
        raise ValueError("Source changed during result inspection")
    return {"declarations": declarations, "complete": proc.returncode == 0,
            'inspection': 'compiled_file' if standalone_artifact else 'compiled_module' if module else 'source',
            'compiled_artifacts': compiled_artifacts if module else {},
            "content_sha256": hashlib.sha256(code.encode()).hexdigest(), "diagnostic": output[-5000:]}


def select(report: dict, name: str, kind: str | None = None) -> dict:
    declarations = report["declarations"]
    matches = [d for d in declarations if d["user_name"] == name or d["full_name"] == name]
    if not matches and "." not in name:
        matches = [d for d in declarations if d["user_name"].rsplit(".", 1)[-1] == name]
    if len(matches) != 1:
        raise ValueError(f"Requested result {name!r} is missing or ambiguous")
    result = matches[0]
    if kind in {"theorem", "proved", "disproved", "lemma"} and result["kind"] != "theorem":
        raise ValueError("The selected result is not a theorem")
    if kind in {"definition", "defined"} and result["kind"] not in {"definition", "opaque", "inductive"}:
        raise ValueError("The selected result is not a definition")
    unexpected = set(result["axioms"]) - ALLOWED_AXIOMS
    if unexpected:
        raise ValueError("The selected result depends on unproved/unsupported axioms: " + ", ".join(sorted(unexpected)))
    return {**result, "content_sha256": report["content_sha256"],
            "signature": result["user_name"] + " : " + result["type"]}


def inspect_selected(path, name, kind=None):
    return select(inspect_file(path), name, kind)
