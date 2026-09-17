"""Verify one reviewed additive patch and publish Git objects only, never refs."""
from pathlib import Path
import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.request

BASE = "065bf3377457555096136bacf3b71527975a0760"
BASE_TREE = "badec6af0de9aaac04a51f01cd18ef89275bd65f"
PATCH_SHA = "e69253753201cb8cda77477fa9f8200acdbb3f8c2f93c66b58d89cbaa8ec4d18"
GZIP_SHA = "c2a0b5eb400d784eb178185724fe8c9032f0504b31a382c5394c041cdf58c027"
FILES = {
    "CHANGELOG.md": "d951400d671ad48d824b328a056dd907bc3634da",
    "docs/incremental-planner-contract.md": "70a8d2e2dad9ee7d0c512dd0e45e6ea266a4cee0",
    "src/memleaf/incremental_planner.py": "581f6573496f19782beb76d510816156e6297475",
    "src/memleaf/incremental_prompt.py": "8e8f4cc879740cddcf0fef612b13cc10c53ca536",
    "src/memleaf/incremental_protocol.py": "40f2f3a106bcf1da253664480e09fa1892c2233e",
    "tests_public/test_incremental_planner.py": "22db31f318c9af001dcec668f2144d686b34b5cd",
    "tests_public/test_incremental_protocol.py": "d6fed97ccb0b48b22566b28992bdb57251ff8f57",
}

def run(args, cwd, env=None):
    subprocess.run(args, cwd=cwd, env=env, check=True)

def output(args, cwd):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()

def blob_digest(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()

root = Path(__file__).resolve().parent
packed = b"".join((root / f"part-{i:02d}").read_bytes() for i in range(12))
assert hashlib.sha256(packed).hexdigest() == GZIP_SHA
patch = gzip.decompress(packed)
assert hashlib.sha256(patch).hexdigest() == PATCH_SHA
workspace = Path(os.environ["RUNNER_TEMP"]) / "g3-reviewed"
work = workspace / "source"
workspace.mkdir()
run(["git", "worktree", "add", "--detach", str(work), BASE], root.parent)
assert output(["git", "rev-parse", "HEAD"], work) == BASE
assert output(["git", "rev-parse", "HEAD^{tree}"], work) == BASE_TREE
patch_file = workspace / "reviewed.patch"
patch_file.write_bytes(patch)
run(["git", "apply", "--check", str(patch_file)], work)
run(["git", "apply", "--index", str(patch_file)], work)
run(["git", "diff", "--cached", "--check"], work)
assert set(output(["git", "diff", "--cached", "--name-only"], work).splitlines()) == set(FILES)
for name, expected in FILES.items():
    assert blob_digest((work / name).read_bytes()) == expected, name
expected_tree = output(["git", "write-tree"], work)
logs = Path(os.environ["GITHUB_WORKSPACE"]) / "g3-verification-logs"
logs.mkdir(exist_ok=True)

def tests(label, package_root, tests_root):
    env = dict(os.environ, PYTHONPATH=str(package_root))
    run([sys.executable, "-c", "import memleaf, pathlib; assert pathlib.Path(memleaf.__file__).resolve().is_relative_to(pathlib.Path(" + repr(str(package_root)) + "))"], workspace, env)
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(tests_root), "-p", "test_*.py", "-v"], cwd=workspace, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (logs / f"{label}.log").write_text(result.stdout, encoding="utf-8")
    print(label + ": " + "\n".join(result.stdout.splitlines()[-5:]))
    assert result.returncode == 0 and re.search(r"Ran 219 tests\b", result.stdout), label

tests("source", work / "src", work / "tests_public")
dist = workspace / "dist"
run([sys.executable, "-m", "build", "--wheel", "--sdist", "--outdir", str(dist)], work)
sdist = next(dist.glob("*.tar.gz"))
wheel = next(dist.glob("*.whl"))
assert sdist.name == "memleaf-0.2.65.tar.gz"
assert wheel.name == "memleaf-0.2.65-py3-none-any.whl"
with tarfile.open(sdist) as archive:
    archive.extractall(workspace / "sdist", filter="data")
unpacked = workspace / "sdist" / "memleaf-0.2.65"
installed = workspace / "installed-wheel"
run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile", "--target", str(installed), str(wheel)], workspace)
for name in FILES:
    assert (work / name).read_bytes() == (unpacked / name).read_bytes(), name
for path in (work / "src" / "memleaf").rglob("*.py"):
    relative = path.relative_to(work / "src")
    assert path.read_bytes() == (installed / relative).read_bytes(), str(relative)
tests("sdist", unpacked / "src", unpacked / "tests_public")
tests("wheel", installed, unpacked / "tests_public")
assert output(["git", "write-tree"], work) == expected_tree

repo = os.environ["GITHUB_REPOSITORY"]
assert repo == "miffyblueboo/memleaf"

def post(endpoint, payload):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/git/{endpoint}",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)

entries = []
for name, expected in FILES.items():
    created = post("blobs", {"content": base64.b64encode((work / name).read_bytes()).decode(), "encoding": "base64"})
    assert created["sha"] == expected, name
    entries.append({"path": name, "mode": "100644", "type": "blob", "sha": expected})
tree = post("trees", {"base_tree": BASE_TREE, "tree": entries})["sha"]
assert tree == expected_tree
(logs / "verified.json").write_text(json.dumps({"base": BASE, "tree": tree, "tests_per_carrier": 219, "files": FILES, "version": "0.2.65"}, indent=2) + "\n")
with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
    stream.write(f"tree={tree}\n")
print("Verified tree " + tree + "; no refs, tags or releases changed.")
