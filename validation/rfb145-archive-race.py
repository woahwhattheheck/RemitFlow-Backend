import json
import os
from pathlib import Path
import subprocess
import sys

BASE = 'b67b7e7639bada58642a12abffa6da6ac5293c5b'
SOURCE = Path('src/services/transferService.js')
TEST = Path('test/transferLifecycleConcurrency.test.js')
SOURCE_BLOB = 'da558d83b3852188adfc61f12b94ca8ccf8f837d'
TEST_BLOB = 'de68455901c59021e3d6671289c8ed43bec63363'
OUT = Path(os.environ['EVIDENCE_DIR'])
OUT.mkdir(parents=True, exist_ok=True)

def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()

def run(name, command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    (OUT / (name + '.stdout')).write_text(result.stdout)
    (OUT / (name + '.stderr')).write_text(result.stderr)
    print(name, 'exit', result.returncode)
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return result

assert git('rev-parse', 'HEAD') == BASE
assert git('hash-object', str(SOURCE)) == SOURCE_BLOB
assert git('hash-object', str(TEST)) == TEST_BLOB
original_source = SOURCE.read_text()
original_test = TEST.read_text()

regression = r'''

// Archive changes share the resource version with terminal settlement.
for (const action of ['archive', 'unarchive']) {
  test(`archive metadata cannot race settlement: ${action}`, () => {
    const transfer = transferService.createTransfer(PAYLOAD);
    if (action === 'unarchive') transferService.archiveTransfer(transfer.id);
    const version = transfer.version;
    const archivedAt = transfer.archivedAt;
    const context = lifecycle(`archive-race-${action}`, version);
    let nestedError;
    let outerError;
    let first;

    settlementWorker.settleClaim = (operationId) => {
      settleCalls += 1;
      const receipt = realSettleClaim(operationId);
      if (settleCalls === 1) {
        try {
          transferService[`${action}Transfer`](transfer.id);
        } catch (error) {
          nestedError = error;
        }
      }
      return receipt;
    };
    try {
      first = transferService.claimTransfer(transfer.id, 'outer', context);
    } catch (error) {
      outerError = error;
    }
    const afterFirst = {
      status: transfer.status,
      version: transfer.version,
      receiptCount: store.settlementReceipts.size,
      nestedStatus: nestedError?.statusCode || null,
      outerStatus: outerError?.statusCode || null,
    };
    // On the old code, the first receipt exists while local state is pending.
    // A new-key retry demonstrates the second settlement, not merely a 409.
    if (outerError && !nestedError) {
      transferService.claimTransfer(
        transfer.id, 'retry', lifecycle(`second-${action}`, transfer.version)
      );
    }
    console.log('ARCHIVE_SETTLEMENT_OBSERVATION', JSON.stringify({
      action, afterFirst, finalReceiptCount: store.settlementReceipts.size,
      settleCalls,
    }));

    assert.ok(nestedError instanceof ApiError, 'metadata mutation must lose the held lease');
    assert.equal(nestedError.statusCode, 409);
    assert.equal(nestedError.details.requestedAction, action);
    assert.equal(outerError, undefined);
    assert.equal(first.status, 'claimed');
    assert.equal(first.version, version + 1);
    assert.equal(first.archivedAt, archivedAt);
    assert.equal(store.settlementReceipts.size, 1);
    assert.equal(settleCalls, 1);
    assert.deepEqual(transferService.claimTransfer(transfer.id, 'replay', context), first);
    assert.equal(settleCalls, 1);
    assert.equal(store.lifecycleLeases.size, 0);
  });
}

test('already archived metadata remains an idempotent no-op during settlement', () => {
  const transfer = transferService.createTransfer(PAYLOAD);
  transferService.archiveTransfer(transfer.id);
  const archivedAt = transfer.archivedAt;
  const version = transfer.version;
  settlementWorker.settleClaim = (operationId) => {
    settleCalls += 1;
    assert.equal(transferService.archiveTransfer(transfer.id), transfer);
    assert.equal(transfer.version, version);
    return realSettleClaim(operationId);
  };
  const result = transferService.claimTransfer(transfer.id, 'claim', lifecycle('noop', version));
  assert.equal(result.archivedAt, archivedAt);
  assert.equal(result.status, 'claimed');
  assert.equal(settleCalls, 1);
});

test('provider failure releases the lease for later archive and unarchive', () => {
  const transfer = transferService.createTransfer(PAYLOAD);
  settlementWorker.settleClaim = () => { throw new Error('provider failed'); };
  assert.throws(
    () => transferService.claimTransfer(transfer.id, 'claim', lifecycle('failure-archive', 1)),
    /provider failed/
  );
  assert.equal(store.lifecycleLeases.size, 0);
  assert.equal(store.lifecycleIdempotency.size, 0);
  assert.equal(transferService.archiveTransfer(transfer.id).version, 2);
  assert.equal(transferService.unarchiveTransfer(transfer.id).version, 3);
  assert.equal(transfer.status, 'pending');
  assert.equal(transfer.archivedAt, null);
  assert.equal(store.settlementReceipts.size, 0);
});
'''
TEST.write_text(original_test + regression)
before_command = ['node', '--test', '--test-name-pattern=archive metadata cannot race settlement', str(TEST)]
before = run('before', before_command)
assert before.returncode == 1, 'baseline must fail the new race assertions'
assert '# fail 2' in before.stdout
observations = []
for line in before.stdout.splitlines():
    marker = 'ARCHIVE_SETTLEMENT_OBSERVATION '
    if marker in line:
        observations.append(json.loads(line.split(marker, 1)[1]))
assert len(observations) == 2
assert all(o['afterFirst']['status'] == 'pending' and o['afterFirst']['receiptCount'] == 1
           and o['finalReceiptCount'] == 2 and o['settleCalls'] == 2 for o in observations)

old = '''function acquireLifecycleLease(transferId, lease) {
  const existing = store.lifecycleLeases.get(transferId);'''
new = '''function assertLifecycleUnlocked(transferId, action) {
  const existing = store.lifecycleLeases.get(transferId);'''
assert original_source.count(old) == 1
fixed = original_source.replace(old, new, 1)
old = '''        requestedAction: lease.action,
      }
    );
  }
  store.lifecycleLeases.set(transferId, lease);
}'''
new = '''        requestedAction: action,
      }
    );
  }
}

function acquireLifecycleLease(transferId, lease) {
  assertLifecycleUnlocked(transferId, lease.action);
  store.lifecycleLeases.set(transferId, lease);
}'''
assert fixed.count(old) == 1
fixed = fixed.replace(old, new, 1)
old = '''  if (!transfer.archivedAt) {
    const timestamp = nextTimestamp(transfer.updatedAt);'''
new = '''  if (!transfer.archivedAt) {
    assertLifecycleUnlocked(id, 'archive');
    const timestamp = nextTimestamp(transfer.updatedAt);'''
assert fixed.count(old) == 1
fixed = fixed.replace(old, new, 1)
old = '''    throw ApiError.conflict(`Transfer is not archived: ${id}`);
  }
  transfer.archivedAt = null;'''
new = '''    throw ApiError.conflict(`Transfer is not archived: ${id}`);
  }
  assertLifecycleUnlocked(id, 'unarchive');
  transfer.archivedAt = null;'''
assert fixed.count(old) == 1
fixed = fixed.replace(old, new, 1)
old = ''' * Acquire an exclusive lease for one transfer while a lifecycle mutation runs.
 * Two different operation keys cannot both prepare provider work for the same
 * transfer — that is the original double-settlement window.
 * @param {string} transferId
 * @param {{ action: string, actor: string, key: string, token: string }} lease'''
new = ''' * Refuse a competing mutation while settlement holds the resource lease.
 * Archive metadata advances the same version as claim/cancel, so it must not
 * invalidate a prepared settlement before the terminal state commits.
 * @param {string} transferId
 * @param {string} action'''
assert fixed.count(old) == 1
fixed = fixed.replace(old, new, 1)
SOURCE.write_text(fixed)
after_command = ['node', '--test', str(TEST)]
after = run('after', after_command)
assert after.returncode == 0
assert '# fail 0' in after.stdout and '# tests 16' in after.stdout
assert not git('diff', '--name-only', '--', 'package.json', 'package-lock.json')
source_blob = git('hash-object', str(SOURCE))
test_blob = git('hash-object', str(TEST))
(OUT / 'change.diff').write_text(subprocess.check_output(['git', 'diff', '--', str(SOURCE), str(TEST)], text=True))
subprocess.run(['git', 'add', str(SOURCE), str(TEST)], check=True)
subprocess.run(['git', 'commit', '-m', 'fix(transfers): exclude archive writes during settlement [skip ci]'], check=True)
candidate = git('rev-parse', 'HEAD')
receipt = {
    'base': BASE, 'candidate': candidate,
    'source_before': SOURCE_BLOB, 'source_after': source_blob,
    'test_before': TEST_BLOB, 'test_after': test_blob,
    'before_command': before_command, 'before_exit': before.returncode,
    'before_observations': observations,
    'after_command': after_command, 'after_exit': after.returncode,
    'node': subprocess.check_output(['node', '--version'], text=True).strip(),
    'platform': sys.platform,
    'run_url': os.environ['RUN_URL'],
    'limits': 'Actual repository services and process-local mock provider; no external settlement, whole-app HTTP, durable multi-worker or live-funds execution.',
}
(OUT / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
(OUT / SOURCE.name).write_text(fixed)
(OUT / TEST.name).write_text(original_test + regression)
print(json.dumps(receipt, indent=2))
subprocess.run(['git', 'push', 'origin', 'HEAD:refs/heads/validation/rfb145-archive-race-rivet1004'], check=True)
