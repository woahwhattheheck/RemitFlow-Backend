from pathlib import Path
import hashlib
import json
import os
import re
import subprocess

BASE = 'b67b7e7639bada58642a12abffa6da6ac5293c5b'
SOURCE_BLOB = 'da558d83b3852188adfc61f12b94ca8ccf8f837d'
SOURCE = Path('src/services/transferService.js')
TEST = Path('test/transferArchiveSettlement.test.js')
DOC = Path('docs/validation/archive-settlement-lease-20261004.md')
EVIDENCE = Path('evidence')
EVIDENCE.mkdir()

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

assert git('rev-parse', 'HEAD') == BASE
assert git('hash-object', str(SOURCE)) == SOURCE_BLOB
assert not git('status', '--porcelain', '--untracked-files=no')
assert not TEST.exists()
TEST.write_text(r'''\
'use strict';

const { test, beforeEach, afterEach } = require('node:test');
const assert = require('node:assert/strict');
const { store, reset } = require('../src/store');
const transfers = require('../src/services/transferService');
const worker = require('../src/services/settlementWorker');
const ApiError = require('../src/utils/ApiError');

const payload = { senderName: 'Alice', recipientName: 'Bob', amount: 100, from: 'USD', to: 'EUR' };
const realSettle = worker.settleClaim;
const context = (key, expectedVersion) => ({ actor: 'archive-race-test', key, expectedVersion });

beforeEach(() => reset());
afterEach(() => { worker.settleClaim = realSettle; });

for (const action of ['archive', 'unarchive']) {
  test(`${action} cannot invalidate a prepared settlement or allow a second claim key`, () => {
    const transfer = transfers.createTransfer(payload);
    if (action === 'unarchive') transfers.archiveTransfer(transfer.id);
    const originalArchive = transfer.archivedAt;
    const version = transfer.version;
    let competingError;
    let providerCalls = 0;
    worker.settleClaim = (operationId) => {
      providerCalls += 1;
      const receipt = realSettle(operationId);
      try {
        transfers[`${action}Transfer`](transfer.id);
      } catch (error) {
        competingError = error;
      }
      return receipt;
    };

    const ctx = context(`claim-${action}`, version);
    const claimed = transfers.claimTransfer(transfer.id, 'claim', ctx);
    assert.ok(competingError instanceof ApiError);
    assert.equal(competingError.statusCode, 409);
    assert.equal(competingError.details.heldAction, 'claim');
    assert.equal(competingError.details.requestedAction, action);
    assert.equal(claimed.status, 'claimed');
    assert.equal(claimed.version, version + 1);
    assert.equal(claimed.archivedAt, originalArchive);
    assert.deepEqual(transfers.claimTransfer(transfer.id, 'retry', ctx), claimed);
    assert.throws(
      () => transfers.claimTransfer(transfer.id, 'other', context('different-key', claimed.version)),
      error => error instanceof ApiError && error.statusCode === 409
    );
    assert.equal(providerCalls, 1);
    assert.equal(store.settlementReceipts.size, 1);
    assert.equal(store.lifecycleLeases.size, 0);
    assert.equal(store.lifecycleIdempotency.size, 1);
    // The archive operation is available again after settlement releases its lease.
    transfers[`${action}Transfer`](transfer.id);
    assert.equal(transfer.version, version + 2);
    assert.equal(transfer.status, 'claimed');
  });
}

test('idempotent re-archive remains a no-op during settlement', () => {
  const transfer = transfers.createTransfer(payload);
  transfers.archiveTransfer(transfer.id);
  const archivedAt = transfer.archivedAt;
  worker.settleClaim = operationId => {
    assert.equal(transfers.archiveTransfer(transfer.id), transfer);
    assert.equal(transfer.version, 2);
    return realSettle(operationId);
  };
  const result = transfers.claimTransfer(transfer.id, 'claim', context('archived-noop', 2));
  assert.equal(result.status, 'claimed');
  assert.equal(result.version, 3);
  assert.equal(result.archivedAt, archivedAt);
});

test('a lease on one transfer does not block another transfer archive', () => {
  const transfer = transfers.createTransfer(payload);
  const other = transfers.createTransfer(payload);
  worker.settleClaim = operationId => {
    transfers.archiveTransfer(other.id);
    return realSettle(operationId);
  };
  const result = transfers.claimTransfer(transfer.id, 'claim', context('independent-transfer', 1));
  assert.equal(result.status, 'claimed');
  assert.equal(other.status, 'pending');
  assert.equal(other.version, 2);
  assert.ok(other.archivedAt);
});

test('failed settlement releases the lease for archive and unarchive', () => {
  const transfer = transfers.createTransfer(payload);
  worker.settleClaim = () => { throw new Error('provider unavailable'); };
  assert.throws(
    () => transfers.claimTransfer(transfer.id, 'claim', context('failed-provider', 1)),
    /provider unavailable/
  );
  assert.equal(store.lifecycleLeases.size, 0);
  assert.equal(store.lifecycleIdempotency.size, 0);
  assert.equal(store.settlementReceipts.size, 0);
  transfers.archiveTransfer(transfer.id);
  transfers.unarchiveTransfer(transfer.id);
  assert.equal(transfer.status, 'pending');
  assert.equal(transfer.version, 3);
  assert.equal(transfer.archivedAt, null);
});
'''.removeprefix('\\\n'), encoding='utf-8')
# Avoid any escaped opening-line marker in the embedded source.
raw_test = TEST.read_text()
if raw_test.startswith('\\\n'):
    TEST.write_text(raw_test[2:])

def run_tests(name, files):
    command = ['node', '--test', '--test-reporter=tap', *files]
    run = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (EVIDENCE / (name + '.log')).write_text(run.stdout)
    print(run.stdout, flush=True)
    counts = {key: int(re.search(r'^# ' + key + r' (\d+)\s*$', run.stdout, re.M).group(1))
              for key in ('tests', 'pass', 'fail', 'cancelled', 'skipped')}
    return {'command': command, 'exit': run.returncode, **counts}

baseline = run_tests('baseline-new-regressions', [str(TEST)])
assert baseline['exit'] != 0 and baseline['fail'] == 2 and baseline['pass'] == 3, baseline
source = SOURCE.read_text()
old = '''function acquireLifecycleLease(transferId, lease) {
  const existing = store.lifecycleLeases.get(transferId);
  if (existing) {
    throw ApiError.conflict(
      'Transfer lifecycle operation already in progress',
      {
        transferId,
        heldAction: existing.action,
        requestedAction: lease.action,
      }
    );
  }
  store.lifecycleLeases.set(transferId, lease);
}'''
new = '''function assertLifecycleLeaseAvailable(transferId, requestedAction) {
  const existing = store.lifecycleLeases.get(transferId);
  if (existing) {
    throw ApiError.conflict(
      'Transfer lifecycle operation already in progress',
      {
        transferId,
        heldAction: existing.action,
        requestedAction,
      }
    );
  }
}

function acquireLifecycleLease(transferId, lease) {
  assertLifecycleLeaseAvailable(transferId, lease.action);
  store.lifecycleLeases.set(transferId, lease);
}'''
assert source.count(old) == 1
source = source.replace(old, new)
old_archive = '''function archiveTransfer(id) {
  const transfer = getTransferOrThrow(id);
  if (!transfer.archivedAt) {
    const timestamp'''
new_archive = '''function archiveTransfer(id) {
  const transfer = getTransferOrThrow(id);
  if (!transfer.archivedAt) {
    assertLifecycleLeaseAvailable(id, 'archive');
    const timestamp'''
assert source.count(old_archive) == 1
source = source.replace(old_archive, new_archive)
old_unarchive = '''    throw ApiError.conflict(`Transfer is not archived: ${id}`);
  }
  transfer.archivedAt = null;'''
new_unarchive = '''    throw ApiError.conflict(`Transfer is not archived: ${id}`);
  }
  assertLifecycleLeaseAvailable(id, 'unarchive');
  transfer.archivedAt = null;'''
assert source.count(old_unarchive) == 1
source = source.replace(old_unarchive, new_unarchive)
SOURCE.write_text(source)
candidate = run_tests('candidate-focused', [
    'test/transferLifecycleConcurrency.test.js',
    'test/transferLifecycleHttp.test.js',
    'test/transferArchive.test.js', str(TEST),
])
assert candidate['exit'] == 0 and candidate['fail'] == 0 and candidate['cancelled'] == 0 and candidate['skipped'] == 0, candidate
report = {
    'base_commit': BASE, 'baseline_source_blob': SOURCE_BLOB,
    'candidate_source_blob': git('hash-object', str(SOURCE)),
    'test_blob': git('hash-object', str(TEST)),
    'node': subprocess.check_output(['node', '--version'], text=True).strip(),
    'baseline': baseline, 'candidate': candidate,
    'scope': 'Actual synchronous in-memory transfer service, existing mock Stellar adapter and maintained HTTP/lifecycle tests; no live provider, durable database, deployment or full-suite claim.',
}
(EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
DOC.parent.mkdir(parents=True, exist_ok=True)
DOC.write_text('''# Archive and settlement lease regression\n\nArchive and unarchive now check the existing transfer-scoped lifecycle lease before changing state. Without that check, a reentrant archive can increment the resource version after provider preparation; the claim then rejects locally despite the retained provider receipt. The check is inside the actual archive mutation, so a retry of an already archived transfer remains a no-op. Other transfers and operations after lease release remain available. No provider, CAS, resource-version or idempotency policy is removed.\n\n## Executed evidence\n\n''' + f"Base: `{BASE}`. Production blob before: `{SOURCE_BLOB}`; after: `{report['candidate_source_blob']}`. Test blob: `{report['test_blob']}`. Runtime: `{report['node']}`.\n\n" + f"The five new cases on the original production source: {baseline['pass']} pass / {baseline['fail']} fail, exit {baseline['exit']}. The archive and unarchive settlement interleavings both fail before the repair.\n\n" + f"After the repair, the four focused files below: {candidate['pass']} pass / {candidate['fail']} fail / {candidate['cancelled']} cancelled / {candidate['skipped']} skipped; exit {candidate['exit']}. Existing state-machine, worker-reload, duplicate-callback, rollback and HTTP tests were not altered.\n\n```sh\nNODE_ENV=test " + ' '.join(candidate['command']) + '\n```\n\n' + report['scope'] + '\n', encoding='utf-8')
subprocess.run(['git', 'config', 'user.name', 'woahwhattheheck'], check=True)
subprocess.run(['git', 'config', 'user.email', '293286387+woahwhattheheck@users.noreply.github.com'], check=True)
subprocess.run(['git', 'add', str(SOURCE), str(TEST), str(DOC)], check=True)
assert set(git('diff', '--cached', '--name-only').splitlines()) == {str(SOURCE), str(TEST), str(DOC)}
subprocess.run(['git', 'commit', '-m', 'Prevent archive changes during transfer settlement [skip ci]'], check=True)
assert git('rev-parse', 'HEAD^') == BASE
report.update(candidate_commit=git('rev-parse', 'HEAD'), candidate_tree=git('rev-parse', 'HEAD^{tree}'))
(EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
(EVIDENCE / 'source.patch').write_text(git('diff', BASE, 'HEAD') + '\n')
(EVIDENCE / 'transferService.js').write_bytes(SOURCE.read_bytes())
(EVIDENCE / TEST.name).write_bytes(TEST.read_bytes())
print(json.dumps(report, indent=2), flush=True)
