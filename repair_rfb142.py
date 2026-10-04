from pathlib import Path
import json
import re
import subprocess

BASE = 'd2962a3e9ea3778c02b2d941b2a7553da10d5deb'
BLOB = 'f225d3244c5dde9d2a1a085929410d04ad2cfa77'
SOURCE = Path('src/utils/auditCrypto.js')
TEST = Path('test/auditArrayIntegrity.test.js')
DOC = Path('docs/validation/audit-array-integrity-20261004.md')
EVIDENCE = Path('evidence')
EVIDENCE.mkdir()

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

assert git('rev-parse', 'HEAD') == BASE
assert git('hash-object', str(SOURCE)) == BLOB
assert not git('status', '--porcelain', '--untracked-files=no')
assert not TEST.exists()
TEST.write_text(r'''
'use strict';

const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
process.env.NODE_ENV = 'test';
const createApp = require('../src/app');
const audit = require('../src/services/auditService');
const { canonicalize, sha256 } = require('../src/utils/auditCrypto');
let server;
let origin;

before(() => new Promise(resolve => {
  server = createApp().listen(0, '127.0.0.1', () => {
    origin = `http://127.0.0.1:${server.address().port}`;
    resolve();
  });
}));
after(() => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
beforeEach(() => audit.reset());

function record(changes) {
  return audit.addEntry({ action: 'transfer.updated', target: 'test-transfer', changes });
}

test('sparse array slots retain their JSON null positions', () => {
  const sparse = new Array(3);
  sparse[1] = 7;
  assert.equal(canonicalize(sparse), '[null,7,null]');
  assert.equal(canonicalize(new Array(1)), '[null]');
  assert.notEqual(sha256([]), sha256(new Array(1)));
  assert.deepEqual(JSON.parse(canonicalize(sparse)), JSON.parse(JSON.stringify(sparse)));
});

test('extending an empty stored array cannot keep a valid audit hash', () => {
  const entry = record({ recipients: [] });
  assert.equal(audit.verifyIntegrity().valid, true);
  entry.changes.recipients.length = 1;
  assert.equal(JSON.stringify(entry.changes), '{"recipients":[null]}');
  const result = audit.verifyIntegrity();
  assert.equal(result.valid, false);
  assert.equal(result.brokenAt, 0);
  assert.match(result.reason, /entryHash mismatch/);
});

test('a sparse stored array and its JSON round trip have the same digest', () => {
  const entry = record({ recipients: new Array(2) });
  assert.equal(audit.verifyIntegrity().valid, true);
  const originalHash = entry.entryHash;
  entry.changes.recipients = JSON.parse(JSON.stringify(entry.changes.recipients));
  assert.deepEqual(entry.changes.recipients, [null, null]);
  assert.equal(audit.verifyIntegrity().valid, true);
  assert.equal(entry.entryHash, originalHash);
});

test('ordinary dense JSON keeps the existing canonical bytes and digest', () => {
  const value = { z: [1, null, undefined, { y: 2, x: 1 }], a: 'dense' };
  const expected = '{"a":"dense","z":[1,null,null,{"x":1,"y":2}]}';
  assert.equal(canonicalize(value), expected);
  assert.equal(sha256(value), crypto.createHash('sha256').update(expected).digest('hex'));
});

test('authenticated integrity endpoint reports array-length tampering', async () => {
  const entry = record({ amounts: [] });
  entry.changes.amounts.length = 1;
  const response = await fetch(origin + '/api/audit/integrity', {
    headers: { Authorization: 'Bearer test-token-admin' },
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.valid, false);
  assert.equal(body.brokenAt, 0);
  assert.match(body.reason, /entryHash mismatch/);
});
'''.lstrip(), encoding='utf-8')

def run_tests(name, files):
    command = ['node', '--test', '--test-reporter=tap', *files]
    run = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (EVIDENCE / (name + '.log')).write_text(run.stdout)
    print(run.stdout, flush=True)
    counts = {key: int(re.search(r'^# ' + key + r' (\d+)\s*$', run.stdout, re.M).group(1))
              for key in ('tests', 'pass', 'fail', 'cancelled', 'skipped')}
    return {'command': command, 'exit': run.returncode, **counts}

baseline = run_tests('baseline-new-regressions', [str(TEST)])
assert baseline['exit'] != 0 and baseline['fail'] == 4 and baseline['pass'] == 1, baseline
source = SOURCE.read_text()
old = "    return `[${value.map(canonicalize).join(',')}]`;"
new = """    // Visit every numeric slot: map() skips holes and can hash [empty] as [].
    const items = Array.from(
      { length: value.length }, (_, index) => canonicalize(value[index])
    );
    return `[${items.join(',')}]`;"""
assert source.count(old) == 1
source = source.replace(old, new)
SOURCE.write_text(source)
candidate = run_tests('candidate-focused', [
    'test/auditIntegrity.test.js', 'test/auditCrypto.test.js', str(TEST),
])
assert candidate['exit'] == 0 and candidate['fail'] == 0 and candidate['cancelled'] == 0 and candidate['skipped'] == 0, candidate
report = {
    'base_commit': BASE, 'baseline_source_blob': BLOB,
    'candidate_source_blob': git('hash-object', str(SOURCE)),
    'test_blob': git('hash-object', str(TEST)),
    'node': subprocess.check_output(['node', '--version'], text=True).strip(),
    'baseline': baseline, 'candidate': candidate,
    'scope': 'Actual in-memory audit service, canonical hash implementation and authenticated loopback Express integrity endpoint. Synthetic records only; no live accounts, external provider, durable database or full-suite claim.',
}
DOC.parent.mkdir(parents=True, exist_ok=True)
DOC.write_text('''# Array positions in audit integrity hashes\n\nCanonical array encoding now visits every numeric slot, including holes, and encodes absent values as JSON null. Previously map() skipped holes: changing an empty stored array to length one changed its JSON output from [] to [null] but left the canonical hash input as []. The existing verifier and HTTP integrity endpoint could therefore miss the edit.\n\nThe repair also makes sparse arrays agree with their JSON round trip. Ordinary dense JSON keeps the same bytes and digest. No record hash is rewritten. Existing pre-repair sparse-array records may fail verification under the corrected encoding; investigate them rather than silently rehashing or accepting the old ambiguous representation. The documented store is in-memory; any separate durable-store adopter must handle this compatibility boundary explicitly.\n\nThe previous own-JSON-key redaction repair is retained unchanged, as are actor fingerprints, compatibility-alias checks and the set of hashed event fields.\n\n## Executed evidence\n\n''' + f"Base `{BASE}`; source before `{BLOB}`, after `{report['candidate_source_blob']}`; new test `{report['test_blob']}`. Runtime `{report['node']}`.\n\n" + f"Original source with new regressions: {baseline['pass']} pass / {baseline['fail']} fail, exit {baseline['exit']}. The actual service and authenticated integrity endpoint accepted an array-length edit before the repair.\n\n" + f"Repaired source with maintained auditIntegrity and auditCrypto suites plus new regressions: {candidate['pass']} pass / {candidate['fail']} fail / {candidate['cancelled']} cancelled / {candidate['skipped']} skipped, exit {candidate['exit']}. Existing tests unchanged.\n\n```sh\nNODE_ENV=test " + ' '.join(candidate['command']) + '\n```\n\n' + report['scope'] + '\n', encoding='utf-8')
subprocess.run(['git', 'config', 'user.name', 'woahwhattheheck'], check=True)
subprocess.run(['git', 'config', 'user.email', '293286387+woahwhattheheck@users.noreply.github.com'], check=True)
subprocess.run(['git', 'add', str(SOURCE), str(TEST), str(DOC)], check=True)
assert set(git('diff', '--cached', '--name-only').splitlines()) == {str(SOURCE), str(TEST), str(DOC)}
subprocess.run(['git', 'commit', '-m', 'Include every array slot in audit integrity hashes [skip ci]'], check=True)
assert git('rev-parse', 'HEAD^') == BASE
report.update(candidate_commit=git('rev-parse', 'HEAD'), candidate_tree=git('rev-parse', 'HEAD^{tree}'))
(EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
(EVIDENCE / 'source.patch').write_text(git('diff', BASE, 'HEAD') + '\n')
(EVIDENCE / 'auditCrypto.js').write_bytes(SOURCE.read_bytes())
(EVIDENCE / TEST.name).write_bytes(TEST.read_bytes())
print(json.dumps(report, indent=2), flush=True)
