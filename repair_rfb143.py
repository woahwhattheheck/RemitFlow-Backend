from pathlib import Path
import json
import re
import subprocess

BASE = 'b0c161f1c9c8200d762e5810ba82c15995a1be0a'
BLOB = 'e82ec442dec7a190eae24f0efddcaabfceff4a17'
SOURCE = Path('src/services/transferService.js')
TEST = Path('test/bulkErrorBoundary.test.js')
DOC = Path('docs/validation/bulk-error-boundary-20261004.md')
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

const { test, before, after, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
process.env.NODE_ENV = 'test';
const createApp = require('../src/app');
const { reset } = require('../src/store');
const transfers = require('../src/services/transferService');
const stellar = require('../src/services/stellarService');
const ApiError = require('../src/utils/ApiError');
const payload = { senderName: 'Alice', recipientName: 'Bob', amount: 100, from: 'USD', to: 'EUR' };
const original = stellar.createClaimableBalanceId;
const secret = 'synthetic-provider-secret@db.invalid';
let server;
let origin;

before(() => new Promise(resolve => {
  server = createApp().listen(0, '127.0.0.1', () => {
    origin = `http://127.0.0.1:${server.address().port}`;
    resolve();
  });
}));
after(() => new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve())));
beforeEach(() => reset());
afterEach(() => { stellar.createClaimableBalanceId = original; });

async function request(path, body, token = 'test-token-admin') {
  const response = await fetch(origin + path, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: response.status, body: await response.json() };
}

const failures = [
  ['ordinary provider error', () => new Error(secret)],
  ['provider object claiming forbidden status', () => Object.assign(new Error(secret), { statusCode: 403 })],
  ['provider object claiming successful status', () => Object.assign(new Error(secret), { statusCode: 200 })],
  ['null rejection', () => null],
  ['string rejection', () => secret],
];

for (const [name, failure] of failures) {
  test(`bulk hides ${name} and continues independent rows`, async () => {
    const first = transfers.createTransfer(payload);
    const second = transfers.createTransfer(payload);
    let calls = 0;
    stellar.createClaimableBalanceId = (...args) => {
      calls += 1;
      if (calls === 1) throw failure();
      return original(...args);
    };
    const result = await request('/api/transfers/bulk', { action: 'claim', ids: [first.id, second.id] });
    assert.equal(result.status, 200, JSON.stringify(result.body));
    assert.equal(result.body.results.length, 2);
    assert.deepEqual(result.body.results[0].error, {
      code: 'error', status: 500, message: 'Internal server error',
    });
    assert.equal(result.body.results[0].ok, false);
    assert.equal(result.body.results[1].ok, true);
    assert.equal(result.body.results[1].transfer.status, 'claimed');
    assert.equal(calls, 2);
    assert.equal(JSON.stringify(result.body).includes(secret), false);
  });
}

test('expected ApiError conflict keeps its intentional public message', async () => {
  const transfer = transfers.createTransfer(payload);
  stellar.createClaimableBalanceId = () => { throw ApiError.conflict('Settlement is already pending'); };
  const result = await request('/api/transfers/bulk', { action: 'claim', ids: [transfer.id] });
  assert.equal(result.status, 200);
  assert.deepEqual(result.body.results[0].error, {
    code: 'conflict', status: 409, message: 'Settlement is already pending',
  });
});

test('scope denial happens before any provider invocation', async () => {
  const transfer = transfers.createTransfer(payload);
  let calls = 0;
  stellar.createClaimableBalanceId = () => { calls += 1; throw new Error(secret); };
  const result = await request('/api/transfers/bulk', { action: 'claim', ids: [transfer.id] }, 'test-token-readonly');
  assert.equal(result.status, 403);
  assert.equal(result.body.error.message, 'Insufficient token scopes');
  assert.equal(calls, 0);
  assert.equal(transfer.status, 'pending');
});

test('bulk uses the same unexpected-error message as the single-item route', async () => {
  const direct = transfers.createTransfer(payload);
  const bulk = transfers.createTransfer(payload);
  stellar.createClaimableBalanceId = () => { throw new Error(secret); };
  const one = await request(`/api/transfers/${direct.id}/claim`);
  const many = await request('/api/transfers/bulk', { action: 'claim', ids: [bulk.id] });
  assert.equal(one.status, 500);
  assert.equal(many.status, 200);
  assert.equal(many.body.results[0].error.status, one.body.error.status);
  assert.equal(many.body.results[0].error.message, one.body.error.message);
  assert.equal(one.body.error.message, 'Internal server error');
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
assert baseline['exit'] != 0 and baseline['fail'] == 6 and baseline['pass'] == 2, baseline
source = SOURCE.read_text()
old = '      const status = err && err.statusCode ? err.statusCode : 500;'
new = '      const isApiError = err instanceof ApiError;\n      const status = isApiError ? err.statusCode : 500;'
assert source.count(old) == 1
source = source.replace(old, new)
old_message = "          message: status === 404 ? transferNotFoundError().message : (err.message || 'error'),"
new_message = "          message: status === 404 ? transferNotFoundError().message\n            : (isApiError ? err.message : 'Internal server error'),"
assert source.count(old_message) == 1
source = source.replace(old_message, new_message)
SOURCE.write_text(source)
candidate = run_tests('candidate-focused', ['test/tokenScopes.test.js', str(TEST)])
assert candidate['exit'] == 0 and candidate['fail'] == 0 and candidate['cancelled'] == 0 and candidate['skipped'] == 0, candidate
report = {
    'base_commit': BASE, 'baseline_source_blob': BLOB,
    'candidate_source_blob': git('hash-object', str(SOURCE)),
    'test_blob': git('hash-object', str(TEST)),
    'node': subprocess.check_output(['node', '--version'], text=True).strip(),
    'baseline': baseline, 'candidate': candidate,
    'scope': 'Real createApp, Express authentication, service and loopback HTTP with synthetic provider failures and existing mock settlement. No live credentials, external provider, durable storage, full-suite or settlement rollback claim.',
}
DOC.parent.mkdir(parents=True, exist_ok=True)
DOC.write_text('''# Bulk error boundary\n\nBulk transfer actions now use the same error-disclosure boundary as the central HTTP handler: only an intentional ApiError controls the public status/message; other thrown values become 500 / Internal server error. Canonical missing-transfer errors, intended API conflicts and scope checks remain intact. Null and string throws remain per-item failures instead of crashing the batch. Independent items still execute.\n\nThis change does not alter transfer state transitions or claim to roll back provider failures.\n\n## Executed evidence\n\n''' + f"Base `{BASE}`; source before `{BLOB}`, after `{report['candidate_source_blob']}`; new test `{report['test_blob']}`. Runtime `{report['node']}`.\n\n" + f"Original production with new HTTP regressions: {baseline['pass']} pass / {baseline['fail']} fail, exit {baseline['exit']}. Failures cover ordinary and status-bearing provider errors, null/string throws, and single-item/bulk message parity.\n\n" + f"Repaired source with maintained token-scope suite and new HTTP regressions: {candidate['pass']} pass / {candidate['fail']} fail / {candidate['cancelled']} cancelled / {candidate['skipped']} skipped, exit {candidate['exit']}. Existing tests unchanged.\n\n```sh\nNODE_ENV=test " + ' '.join(candidate['command']) + '\n```\n\n' + report['scope'] + '\n', encoding='utf-8')
subprocess.run(['git', 'config', 'user.name', 'woahwhattheheck'], check=True)
subprocess.run(['git', 'config', 'user.email', '293286387+woahwhattheheck@users.noreply.github.com'], check=True)
subprocess.run(['git', 'add', str(SOURCE), str(TEST), str(DOC)], check=True)
assert set(git('diff', '--cached', '--name-only').splitlines()) == {str(SOURCE), str(TEST), str(DOC)}
subprocess.run(['git', 'commit', '-m', 'Keep unexpected bulk provider errors private [skip ci]'], check=True)
assert git('rev-parse', 'HEAD^') == BASE
report.update(candidate_commit=git('rev-parse', 'HEAD'), candidate_tree=git('rev-parse', 'HEAD^{tree}'))
(EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
(EVIDENCE / 'source.patch').write_text(git('diff', BASE, 'HEAD') + '\n')
(EVIDENCE / 'transferService.js').write_bytes(SOURCE.read_bytes())
(EVIDENCE / TEST.name).write_bytes(TEST.read_bytes())
print(json.dumps(report, indent=2), flush=True)
