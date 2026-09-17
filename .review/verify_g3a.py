from pathlib import Path
import base64, hashlib, json, os, subprocess, sys, urllib.request, zlib
BASE = '065bf3377457555096136bacf3b71527975a0760'
TREE = '5e51734c5e27b2999eef67291de3033edcbf11e4'
BASE_TREE = 'badec6af0de9aaac04a51f01cd18ef89275bd65f'
root = Path(os.environ['RUNNER_TEMP']) / 'g3a-verified'
paths = ['CHANGELOG.md','docs/incremental-planner-preview.md','src/memleaf/incremental_dates.py','src/memleaf/incremental_preview.py','src/memleaf/incremental_prompts.py','src/memleaf/incremental_protocol.py','src/memleaf/service.py','tests_public/test_incremental_dates.py','tests_public/test_incremental_preview.py','tests_public/test_incremental_protocol.py']
def run(*args, cwd=root, **kw):
    return subprocess.run(args, cwd=cwd, check=True, **kw)
if sys.argv[1] == 'verify':
    patch = zlib.decompress(base64.b64decode(''.join(Path(f'.review/g3a/{i}.b64').read_text() for i in range(6)), validate=True))
    assert hashlib.sha256(patch).hexdigest() == 'c34c7f9307d61befbe1b269c709604cf96f1fd7faebac276ccd510aaf7ff79de'
    run('git','worktree','add','--detach',str(root),BASE,cwd=Path.cwd())
    run('git','apply','--check','-',input=patch)
    run('git','apply','-',input=patch)
    run('git','add','--',*paths)
    tree = run('git','write-tree',capture_output=True,text=True).stdout.strip()
    assert tree == TREE, (tree,TREE)
    env = dict(os.environ, PYTHONPATH=str(root/'src'))
    run(sys.executable,'-m','unittest','discover','-s','tests_public','-p','test_*.py',env=env)
    (root/'verified-tree.txt').write_text(TREE)
elif sys.argv[1] == 'store':
    assert (root/'verified-tree.txt').read_text() == TREE
    assert run('git','write-tree',capture_output=True,text=True).stdout.strip() == TREE
    for path in paths:
        recorded = run('git','rev-parse',':'+path,capture_output=True,text=True).stdout.strip()
        b = (root/path).read_bytes()
        assert hashlib.sha1(b'blob '+str(len(b)).encode()+b'\0'+b).hexdigest() == recorded
    payload = {'base_tree':BASE_TREE,'tree':[{'path':p,'mode':'100644','type':'blob','content':(root/p).read_text(encoding='utf-8')} for p in paths]}
    request = urllib.request.Request('https://api.github.com/repos/miffyblueboo/memleaf/git/trees',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json','Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(request,timeout=60) as response:
        actual = json.load(response)['sha']
    assert actual == TREE, (actual,TREE)
    with open(os.environ['GITHUB_OUTPUT'],'a') as f: f.write('tree='+actual+'\n')
    print('Verified source tree:', actual)
else:
    raise SystemExit('unknown mode')
