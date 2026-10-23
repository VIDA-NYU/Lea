"""Validate a retry against compiler ranges and immutable current source.

The lexical index never grants edit permission. Missing compiler evidence is a
retained conflict, not permission to regenerate a shared file.
"""
from __future__ import annotations

from lea.result_selection import ALLOWED_AXIOMS


class PreservationConflict(ValueError):
    pass


def offset(code, line, column):
    lines = code.splitlines(keepends=True)
    if line < 1 or line > len(lines) + 1:
        raise PreservationConflict("Invalid compiler source range")
    return sum(map(len, lines[:line - 1])) + column


def bounds(code, decl):
    r = decl.get("range")
    if not r:
        raise PreservationConflict("Compiler did not establish declaration boundaries")
    return offset(code, r["start_line"], r["start_column"]), offset(code, r["end_line"], r["end_column"])


def declarations(report):
    # Generated auxiliaries can share their parent's source range. Keep their
    # semantic evidence, but a range is removed from the source projection once.
    return {d["user_name"]: d for d in report["declarations"]}


def resolve(report, name):
    ds = declarations(report)
    matches = [d for n, d in ds.items() if n == name or ("." not in name and n.rsplit(".", 1)[-1] == name)]
    if len(matches) != 1:
        raise PreservationConflict(f"Target {name!r} is missing or ambiguous")
    return matches[0]


def _projection(code, report, editable):
    spans = set()
    for d in report["declarations"]:
        if d["user_name"] in editable:
            spans.add(bounds(code, d))
    for start, end in sorted(spans, reverse=True):
        code = code[:start] + code[end:]
    # New helper insertions may add blank separators, never remove comments,
    # change an attribute, or change whitespace inside protected declarations.
    return "\n".join(line for line in code.splitlines() if line.strip())


def validate_file(before, after, baseline, candidate, target, *, source_update=False):
    old = declarations(baseline)
    new = declarations(candidate)
    previous = resolve(baseline, target)
    current = resolve(candidate, target)
    if previous["kind"] != current["kind"]:
        raise PreservationConflict("The requested declaration kind changed")
    if not source_update and previous["type_expr"] != current["type_expr"]:
        raise PreservationConflict("A proof update cannot change the target interface")
    editable = {previous["user_name"]}
    existing_needed, existing_pending = set(), [previous['user_name']]
    while existing_pending:
        name = existing_pending.pop()
        if name in existing_needed:
            continue
        existing_needed.add(name)
        existing_pending.extend(n for n in old.get(name, {}).get('dependencies', []) if n in old)
    pending = [current["user_name"]]
    needed = set()
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending.extend(d for d in new.get(name, {}).get("dependencies", []) if d in new)
    for name in needed - editable:
        if name not in old:
            editable.add(name)
            continue
        d = old[name]
        # Complete an existing unfinished local helper only under its pinned
        # type, and only if no protected declaration consumes it.
        users = {n for n, x in old.items() if name in x.get("dependencies", [])}
        if "sorryAx" in d.get("axioms", []) and name in existing_needed and users <= existing_needed:
            if d["type_expr"] != new[name]["type_expr"]:
                raise PreservationConflict(f"Helper {name} changed its mathematical contract")
            if d["kind"] != "theorem" and d.get("body_expr") != new[name].get("body_expr"):
                raise PreservationConflict(f"Definition helper {name} needs an explicit pinned dependency")
            editable.add(name)
    # Generated constants contained in an editable command are editable only
    # when the compiler ties them to that exact command range.
    editable_ranges = {tuple(sorted(old[n].get("range", {}).items())) for n in editable if n in old}
    for n, d in old.items():
        if d.get("range") and tuple(sorted(d["range"].items())) in editable_ranges:
            editable.add(n)
    for name, d in old.items():
        if name in editable:
            continue
        other = new.get(name)
        if not other:
            raise PreservationConflict(f"Existing declaration {name} was removed")
        for key in ("kind", "visibility", "type_expr", "body_expr"):
            if d.get(key) != other.get(key):
                raise PreservationConflict(f"Protected declaration {name} changed ({key})")
        if set(other.get("axioms", [])) - set(d.get("axioms", [])) - ALLOWED_AXIOMS:
            raise PreservationConflict(f"Protected declaration {name} gained trust assumptions")
        if d.get('range') and other.get('range'):
            lo, hi = bounds(before, d)
            nlo, nhi = bounds(after, other)
            if before[lo:hi] != after[nlo:nhi]:
                raise PreservationConflict(f"Protected proof/source for {name} changed")
        elif before != after:
            # Compiled modules include generated constants without source ranges.
            # Only exact whole-file retention can establish their source here.
            raise PreservationConflict(f'Cannot establish protected boundaries for {name}')
    extra = set(new) - set(old)
    if extra - needed:
        raise PreservationConflict("The retry added unrelated declarations: " + ", ".join(sorted(extra - needed)))
    if _projection(before, baseline, editable) != _projection(after, candidate, editable | extra):
        raise PreservationConflict("The retry changed protected comments or shared command context")
    return {"editable": sorted(editable), "protected": sorted(set(old) - editable),
            "source_update": source_update}


def validate_project(before, after, baseline, candidate, target, selected, *, source_update=False,
                     pinned_helpers=()):
    """The same permissions across moves. Imports alone never grant permission."""
    def index(reports):
        result = {}
        for path, report in reports.items():
            if report is None:
                continue
            for d in report['declarations']:
                name = d['user_name']
                if name in result:
                    raise PreservationConflict(f'Duplicate declaration {name}')
                result[name] = (path, d)
        return result
    old, new = index(baseline), index(candidate)
    matches = [n for n in old if n == target or ('.' not in target and n.rsplit('.', 1)[-1] == target)]
    if not matches and target in pinned_helpers:
        matches = [target]
    if len(matches) != 1:
        raise PreservationConflict('The existing target has no unambiguous compiler boundary')
    original = matches[0]
    actual = selected['user_name']
    if actual not in new:
        raise PreservationConflict('The selected result is missing')
    if actual != original and selected.get('outcome') != 'disproved':
        raise PreservationConflict('Only an explicit disproof can select a different declaration')
    if actual == original and original in old and not source_update and old[original][1]['type_expr'] != new[actual][1]['type_expr']:
        raise PreservationConflict('A proof update cannot change the target interface')
    if actual == original and original in old:
        previous, current = old[original][1], new[actual][1]
        if previous['kind'] != current['kind']:
            raise PreservationConflict('The target declaration kind changed')
        if (not source_update and previous['kind'] != 'theorem' and
                'sorryAx' not in previous.get('axioms', []) and previous.get('body_expr') != current.get('body_expr')):
            raise PreservationConflict('A proof update cannot change a completed definition contract')
    existing_needed, existing_pending = set(), [original]
    while existing_pending:
        name = existing_pending.pop()
        if name in existing_needed:
            continue
        existing_needed.add(name)
        existing_pending.extend(n for n in old.get(name, ('', {}))[1].get('dependencies', []) if n in old)
    needed, pending = set(), [actual]
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending.extend(n for n in new.get(name, ('', {}))[1].get('dependencies', []) if n in new)
    editable = {original, actual}
    for name in needed:
        if name not in old:
            editable.add(name)
        elif name != original and 'sorryAx' in old[name][1].get('axioms', []):
            users = {n for n, (_, d) in old.items() if name in d.get('dependencies', [])}
            if name in pinned_helpers or (name in existing_needed and users <= existing_needed):
                if old[name][1]['type_expr'] != new[name][1]['type_expr']:
                    raise PreservationConflict(f'Unfinished helper {name} changed its pinned type')
                editable.add(name)
    for path, report in baseline.items():
        if not report:
            if before[path] != after.get(path):
                raise PreservationConflict(f'Cannot establish protected boundaries in {path}')
            continue
        ranges = {bounds(before[path], d) for d in report['declarations'] if d['user_name'] in editable}
        editable.update(d['user_name'] for d in report['declarations']
                        if d.get('range') and bounds(before[path], d) in ranges)
    protected = set(old) - editable
    affected = {original}
    while True:
        expanded = affected | {n for n, (_, d) in old.items() if set(d.get('dependencies', [])) & affected}
        if expanded == affected:
            break
        affected = expanded
    for name in protected:
        path, d = old[name]
        if name not in new:
            # A source update may leave a retained consumer unelaboratable, but
            # only exact original command bytes qualify as retained work.
            if source_update and name in affected:
                lo, hi = bounds(before[path], d)
                if after.get(path, '').count(before[path][lo:hi]) == 1:
                    continue
            raise PreservationConflict(f'Protected declaration {name} was removed')
        other_path, other = new[name]
        if d.get('range') and other.get('range'):
            lo, hi = bounds(before[path], d)
            nlo, nhi = bounds(after[other_path], other)
            if before[path][lo:hi] != after[other_path][nlo:nhi]:
                raise PreservationConflict(f'Protected source for {name} changed')
        elif path != other_path or before[path] != after.get(other_path):
            # A generated constant may have no compiler range. Do not infer one
            # from its name: retain its entire module at the same path instead.
            raise PreservationConflict(f'Cannot establish protected boundaries for {name}')
        keys = ('kind', 'visibility', 'type_expr', 'body_expr')
        if any(d.get(k) != other.get(k) for k in keys) and not (source_update and name in affected):
            raise PreservationConflict(f'Protected mathematics for {name} changed')
        if not (source_update and name in affected) and set(other.get('axioms', [])) - set(d.get('axioms', [])) - ALLOWED_AXIOMS:
            raise PreservationConflict(f'Protected result {name} gained trust assumptions')
    extra = set(new) - set(old)
    if extra - needed:
        raise PreservationConflict('Unrelated declarations were added: ' + ', '.join(sorted(extra - needed)))
    relocated = {n for n in protected if n in new and old[n][0] != new[n][0]}
    for path, code in before.items():
        report = baseline.get(path)
        if not report or code == after.get(path):
            continue
        other = candidate.get(path)
        if not other:
            raise PreservationConflict(f'Protected module context in {path} was removed')
        def context(text, evidence):
            # Relocation can add compatibility imports. Compiler identities above
            # still pin every protected declaration's meaning and source bytes.
            projected = _projection(text, evidence, editable | extra | relocated)
            return '\n'.join(l for l in projected.splitlines() if not l.lstrip().startswith('import '))
        if context(code, report) != context(after[path], other):
            raise PreservationConflict(f'Protected comments or module context changed in {path}')
    return {'editable': sorted(editable), 'protected': sorted(protected),
            'source_update': source_update, 'stable_protected': sorted(protected - affected if source_update else protected), 'target_before': original, 'target_after': actual,
            'relocations': [{'name': n, 'before_path': old[n][0], 'after_path': new[n][0]} for n in sorted(relocated)]}
