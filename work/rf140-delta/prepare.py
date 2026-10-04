"""Prepare only the pinned readiness follow-up; performs no publication."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

PAYLOAD = '''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'''
raw = gzip.decompress(base64.b64decode(PAYLOAD))
assert hashlib.sha256(raw).hexdigest() == 'e37711853622ef63056ebd302ad0788f72ec271cd996efcf027c806f11aecd7b'
files = json.loads(raw)
for path, content in files.items():
    target = Path(path)
    assert not target.exists(), path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)

# --test-only exposes the exact new regressions against the old implementation.
import sys
if '--test-only' in sys.argv:
    raise SystemExit(0)

p = Path('src/services/dependencyHealthService.js')
assert subprocess.check_output(['git', 'hash-object', str(p)], text=True).strip() == '5a361b2d3f56b55f418af41222878a43ac3628aa'
s = p.read_text()
old = "const stellarService = require('./stellarService');"
assert s.count(old) == 1
s = s.replace(old, old + "\nconst { createProbePool } = require('./inFlightProbe');\nconst probePool = createProbePool();")
old = '    await withTimeout(forced || probe(), timeoutMs, timeoutReason);'
new = '''    const subscription = forced ? null : probePool.acquire(name, probe);
    try {
      await withTimeout(forced || subscription.promise, timeoutMs, timeoutReason);
    } finally {
      if (subscription) subscription.release();
    }'''
assert s.count(old) == 1
s = s.replace(old, new)
old = 'function resetForTests() {\n'
assert s.count(old) == 1
s = s.replace(old, old + '  probePool.clear();\n')
s = s.replace('and every\n * probe is re-evaluated on the next request', 'and completed\n * probes are re-evaluated on the next request')
s = s.replace(' * Evaluate every dependency. Safe to call on every readiness request —\n * nothing is cached as permanently failed.', ' * Evaluate every dependency. Overlapping requests share only unfinished\n * probes; each caller keeps its own deadline and completed checks are not cached.')
p.write_text(s)
