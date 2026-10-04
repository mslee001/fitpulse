"""Guard: every call to a workouts function passes its required arguments.

Threading `user` through the app changed many signatures; a call that still
used the old one only failed at runtime (Peloton/Withings sync broke this way
after the multi-user deploy). This walks the source and checks each call to a
uniquely named module-level function or class against its signature."""
import ast
from pathlib import Path

from django.test import SimpleTestCase

WORKOUTS = Path(__file__).resolve().parent.parent
QUALIFIERS = {"_programs", "llm", "P"}   # module aliases used for calls like _programs.foo()


def _source_files():
    return [p for p in WORKOUTS.rglob("*.py") if not {"migrations", "tests"} & set(p.relative_to(WORKOUTS).parts)]


def _signatures(files):
    sigs = {}
    for path in files:
        for node in ast.parse(path.read_text()).body:
            fn, skip_self = node, False
            if isinstance(node, ast.ClassDef):
                fn = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
                skip_self = True
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            a = fn.args
            positional = [x.arg for x in a.posonlyargs + a.args][1 if skip_self else 0:]
            required = positional[:len(positional) - len(a.defaults)]
            required_kw = [x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
            sigs.setdefault(node.name, []).append((required, required_kw, a.vararg is not None))
    return {name: v[0] for name, v in sigs.items() if len(v) == 1}   # skip ambiguous names


class CallArityTests(SimpleTestCase):
    def test_calls_pass_required_arguments(self):
        files = _source_files()
        sigs = _signatures(files)
        problems = []
        for path in files:
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if isinstance(f, ast.Name):
                    name = f.id
                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in QUALIFIERS:
                    name = f.attr
                else:
                    continue
                if name not in sigs:
                    continue
                required, required_kw, varargs = sigs[name]
                if varargs or any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                    continue
                given_kw = {k.arg for k in node.keywords}
                missing = [r for i, r in enumerate(required) if i >= len(node.args) and r not in given_kw]
                missing += [k for k in required_kw if k not in given_kw]
                if missing:
                    problems.append(f"workouts/{path.relative_to(WORKOUTS)}:{node.lineno}: {name}() missing {missing}")
        self.assertEqual(problems, [], "Calls missing required arguments:\n" + "\n".join(problems))
