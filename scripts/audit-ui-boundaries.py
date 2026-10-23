#!/usr/bin/env python3
"""Discover and check the Stage 0 boundary register without importing the app.

Run with --list for route/handler locations, or with no arguments to check the
reviewed register. Discovery never executes application startup or reads runtime
data. New/changed dispatch syntax fails closed for review.
"""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "docs/database-boundary-inventory.json"
METHODS = {"get", "post", "put", "patch", "delete", "websocket", "head", "options"}


def constant(node, values):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return constant(node.left, values) + constant(node.right, values)
    raise ValueError(f"Review new route expression: {ast.dump(node)}")


def discover():
    found = {}
    app = ROOT / "apps/lea-standalone/adapter/app"
    for path in [app / "main.py", *sorted((app / "api_routes").glob("*.py"))]:
        tree = ast.parse(path.read_text())
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                try:
                    values[node.targets[0].id] = constant(node.value, values)
                except (ValueError, KeyError):
                    pass
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"add_api_route", "add_route", "api_route", "route",
                                      "websocket_route", "add_websocket_route", "add_api_websocket_route"}:
                    raise ValueError(f"Review new routing syntax in {path.relative_to(ROOT)}")
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "app":
                    if node.func.attr == "mount":
                        route = constant(node.args[0], values)
                        found[f"adapter MOUNT {route}"] = {"source": str(path.relative_to(ROOT)),
                            "line": node.lineno, "handler": "static_mount"}
                    if node.func.attr == "include_router" and node.keywords:
                        raise ValueError("Review router prefixes/options before inventorying routes")
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id in {"app", "router"} and dec.func.attr in METHODS):
                    continue
                route = constant(dec.args[0], values)
                key = f"adapter {dec.func.attr.upper()} {route}"
                if key in found:
                    raise ValueError(f"Duplicate route: {key}")
                found[key] = {"source": str(path.relative_to(ROOT)), "line": dec.lineno,
                              "handler": node.name}

    # The public transport is deliberately stateless. Its whole source is
    # fingerprinted below, so any changed dispatch requires a human review.
    path = ROOT / "apps/overleaf-extension/companion/server.mjs"
    source = path.read_text()
    for route in sorted(set(re.findall(r"pathname(?:===|==)'([^']+)'", source)+re.findall(r"pathname\.startsWith\('([^']+)'",source)+re.findall(r"'(/[^']+)':",source))):
        offset = source.index("'"+route+"'")
        found[f"companion transport {route}"] = {"source":str(path.relative_to(ROOT)),
            "line":source.count("\n",0,offset)+1,"handler":"database_transport"}
    if not any(k.startswith('companion ') for k in found):
        raise ValueError('Companion discovery is incomplete; review dispatch syntax')
    # Worker handlers are not exposed by the transport. Keep their inventory to
    # prove the old surfaces still have a queue translator, not a UI back door.
    path = ROOT / "apps/overleaf-extension/companion/workerService.mjs"
    source = path.read_text()
    start=source.index('async function routeRequest(')
    end=source.index('\nexport async function buildSettingsResponse',start)
    dispatch=source[start:end]
    for header in re.findall(r"^  if \((.*?)\) \{",dispatch,re.M|re.S):
        if 'url.pathname' not in header:continue
        condition=' '.join(header.split())
        offset=start+dispatch.index(f'  if ({header}) {{')
        found[f'worker {condition}']={'source':str(path.relative_to(ROOT)),
            'line':source.count('\n',0,offset)+1,'handler':'private_routeRequest'}

    return found


def companion_dispatch_digest():
    source = (ROOT / "apps/overleaf-extension/companion/server.mjs").read_text()
    worker=(ROOT / "apps/overleaf-extension/companion/workerService.mjs").read_text()
    start=worker.index('async function routeRequest(')
    end=worker.index('\nexport async function buildSettingsResponse',start)
    return hashlib.sha256((source+'\0'+worker[start:end]).encode()).hexdigest()



def check(found):
    register = json.loads(REGISTER.read_text())
    entries = register["routes"]
    missing = sorted(found.keys() - entries.keys())
    obsolete = sorted(entries.keys() - found.keys())
    errors = [*(f"Unclassified route: {key}" for key in missing),
              *(f"Obsolete route: {key}" for key in obsolete)]
    if register["companion_dispatch_sha256"] != companion_dispatch_digest():
        errors.append("Companion dispatch changed: review all branches and update its inventory digest")
    required = {"caller", "source", "effect", "authority", "target", "stage", "compatibility"}
    for key, profile in entries.items():
        metadata = register["profiles"].get(profile, {})
        if not required <= metadata.keys():
            errors.append(f"Incomplete profile {profile}: {key}")
        if any(not metadata.get(field) for field in required - {"stage"}):
            errors.append(f"Empty profile field {profile}: {key}")
        if type(metadata.get("stage")) is not int or not 0 <= metadata["stage"] <= 8:
            errors.append(f"Invalid migration stage {profile}: {key}")
        if metadata.get("target") not in {"infrastructure", "database-query", "database-mutation",
                                          "source-publication", "execution-command"}:
            errors.append(f"Invalid category {profile}: {key}")
    if errors:
        raise ValueError("\n".join(errors))
    print(f"Boundary register covers {sum(k.startswith('adapter ') for k in found)} adapter routes "
          f"and {sum(k.startswith('companion ') for k in found)} companion dispatch branches.")


if __name__ == "__main__":
    try:
        routes = discover()
        if sys.argv[1:] == ["--list"]:
            print(json.dumps(routes, indent=2))
        elif sys.argv[1:]:
            raise ValueError("Usage: audit-ui-boundaries.py [--list]")
        else:
            check(routes)
    except (ValueError, KeyError, OSError) as exc:
        sys.exit(str(exc))
