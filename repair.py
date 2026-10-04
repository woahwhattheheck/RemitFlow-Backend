"""Repair numeric-output precision on the pinned existing PR; publish only an isolated candidate."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import time
import traceback

ROOT = Path(os.environ['GITHUB_WORKSPACE'])
SUBJECT = ROOT / 'subject'
OUT = ROOT / 'evidence'
OUT.mkdir(exist_ok=True)
PIN = '2e815f63d2f9f8fd2bbcb714fbad1b389877f2d5'
BRANCH = 'validation/quarry-rfb139-readback-candidate-20261004'
receipt = {'baseline_sha': PIN, 'success': False, 'commands': []}

def run(label, args, check=True, timeout=120):
    started = time.monotonic()
    with (OUT / (label + '.log')).open('w') as log:
        p = subprocess.run(args, cwd=SUBJECT, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    receipt['commands'].append({'label': label, 'argv': args, 'exit_code': p.returncode, 'seconds': time.monotonic() - started})
    if check and p.returncode:
        raise RuntimeError(f'{label} exited {p.returncode}')
    return p.returncode, (OUT / (label + '.log')).read_text()

def replace_once(text, before, after):
    assert text.count(before) == 1, before
    return text.replace(before, after)

try:
    assert run('head', ['git', 'rev-parse', 'HEAD'])[1].strip() == PIN
    path = SUBJECT / 'src/utils/currencyPolicy.js'
    original = path.read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(original)).encode() + b'\0' + original).hexdigest()
    assert blob == 'e8ff338e7d7928ebdd7dc83edb7d3ad01ed1e923', blob
    (OUT / 'currencyPolicy.before.js').write_bytes(original)
    run('install', ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'])
    test_path = SUBJECT / 'test/currencyPolicy.test.js'
    with test_path.open('a') as f:
        f.write(r'''

// Decimal cents must survive the existing Number-valued API representation.
test('numeric readback rejects cent values rounded by the Number representation', () => {
  for (const code of ['USD', 'EUR', 'GBP', 'INR', 'NGN', 'PHP', 'MXN', 'KES']) {
    for (const amount of ['90071992547409.91', '70368744177664.01', '-70368744177664.01']) {
      assert.throws(() => currencyPolicy.roundToCurrency(amount, code),
        (error) => error instanceof RangeError && /numeric range/.test(error.message));
    }
  }
});

test('numeric readback returns structured canonical errors without throwing', () => {
  for (const amount of ['90071992547409.91', '70368744177664.01']) {
    assert.deepEqual(currencyPolicy.canonicalizeAmount(amount, 'USD', { enforceMax: false }), {
      ok: false, errors: ['amount is outside the supported numeric range'],
    });
    assert.deepEqual(currencyPolicy.validateTransferPair(amount, 'USD', 'EUR', { enforceMax: false }),
      ['amount is outside the supported numeric range']);
  }
});

test('numeric readback retains representable decimal and whole-yen boundaries', () => {
  for (const amount of ['90071992547409.89', '90071992547409.88', '90071992547409.90', '0.01', '1.01']) {
    const result = currencyPolicy.canonicalizeAmount(amount, 'USD', { enforceMax: false });
    assert.equal(result.ok, true);
    const text = JSON.parse(JSON.stringify(result.amount)).toString();
    const [whole, fraction = ''] = text.split('.');
    const cents = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'));
    assert.equal(cents, BigInt(amount.replace('.', '')));
  }
  assert.equal(currencyPolicy.roundToCurrency(Number.MAX_SAFE_INTEGER, 'JPY'), Number.MAX_SAFE_INTEGER);
  assert.equal(currencyPolicy.roundToCurrency(-Number.MAX_SAFE_INTEGER, 'JPY'), -Number.MAX_SAFE_INTEGER);
  assert.ok(Object.is(currencyPolicy.roundToCurrency('-0.001', 'USD'), -0));
});

test('numeric readback returns HTTP 400 before transfer or audit effects', async (t) => {
  const submitPayment = t.mock.method(stellarService, 'submitPayment');
  for (const amount of ['90071992547409.91', '70368744177664.01']) {
    const response = await fetchJson(`/api/quote?amount=${amount}&from=USD&to=EUR`);
    assert.equal(response.status, 400);
    assert.ok(response.body.error.details.errors.includes('amount is outside the supported numeric range'));
  }
  assert.equal(submitPayment.mock.callCount(), 0);
  assert.equal(store.transfers.size, 0);
  assert.equal(store.idempotency.size, 0);
  assert.equal(auditService.countEntries(), 0);
});
''')
    before, _ = run('baseline', ['node', '--test', '--test-name-pattern=numeric readback', 'test/currencyPolicy.test.js'], check=False)
    assert before == 1, f'Expected reproduced failure, got {before}'
    source = original.decode()
    source = replace_once(source,
        '  return (negative ? -Number(rounded) : Number(rounded)) / factor;',
        '''  const result = (negative ? -Number(rounded) : Number(rounded)) / factor;
  // A safe integer number of cents need not survive division as a Number.
  // Compare its decimal serialization exactly, not another float multiply.
  const [readbackN, readbackD] = decimalRatio(result);
  const signedUnits = negative ? -rounded : rounded;
  if (readbackN * BigInt(factor) !== signedUnits * readbackD) {
    throw new RangeError('amount is outside the supported numeric range');
  }
  return result;''')
    source = replace_once(source,
        '  const canonical = roundToCurrency(amount, meta.code);',
        '''  let canonical;
  try {
    canonical = roundToCurrency(amount, meta.code);
  } catch (err) {
    if (!(err instanceof RangeError)) throw err;
    errors.push('amount is outside the supported numeric range');
    return { ok: false, errors };
  }''')
    path.write_text(source)
    with (SUBJECT / 'README.md').open('a') as f:
        f.write('''\n\n### Exact numeric amount boundary\n\nThe currency policy rejects an amount when its rounded minor-unit integer cannot\nround-trip through the existing Number-valued JSON API without changing its\ndecimal value. `Number.MAX_SAFE_INTEGER` minor units is an upper bound, not a\npromise that every smaller cent amount is representable. For example,\n`90071992547409.91` USD would serialize as `90071992547409.9`; it now produces the\nexisting numeric-range validation error rather than silently losing a cent.\nRepresentable neighboring values and whole-number JPY retain their existing\nbehavior. Direct rounding throws `RangeError`; canonical and HTTP validation\nretain structured errors. No string-valued API migration is introduced.\n\nFocused coverage: `node --test test/currencyPolicy.test.js`.\n''')
    run('candidate', ['node', '--test', 'test/currencyPolicy.test.js'])
    run('node-version', ['node', '--version'])
    run('diff', ['git', 'diff', '--', 'src/utils/currencyPolicy.js', 'test/currencyPolicy.test.js', 'README.md'])
    run('git-name', ['git', 'config', 'user.name', 'woahwhattheheck'])
    run('git-email', ['git', 'config', 'user.email', '293286387+woahwhattheheck@users.noreply.github.com'])
    run('stage', ['git', 'add', 'src/utils/currencyPolicy.js', 'test/currencyPolicy.test.js', 'README.md'])
    run('commit', ['git', 'commit', '-m', 'fix(currency): reject lossy numeric minor-unit output [skip ci]'])
    sha = run('candidate-head', ['git', 'rev-parse', 'HEAD'])[1].strip()
    receipt['candidate_sha'] = sha
    receipt['tree_sha'] = run('candidate-tree', ['git', 'rev-parse', 'HEAD^{tree}'])[1].strip()
    receipt['source_blob'] = run('candidate-blob', ['git', 'rev-parse', 'HEAD:src/utils/currencyPolicy.js'])[1].strip()
    assert not run('clean', ['git', 'status', '--porcelain'])[1].strip()
    run('source-archive', ['git', 'archive', '--format=tar.gz', '-o', str(OUT / 'source.tar.gz'), 'HEAD'])
    run('retain-candidate', ['git', 'push', 'origin', f'HEAD:refs/heads/{BRANCH}'])
    receipt['candidate_branch'] = BRANCH
    receipt['success'] = True
except Exception:
    receipt['error'] = traceback.format_exc()
finally:
    (OUT / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))
if not receipt['success']:
    raise SystemExit(1)
