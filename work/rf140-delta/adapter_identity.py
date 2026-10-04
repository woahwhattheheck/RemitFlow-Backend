from pathlib import Path

p = Path('src/services/inFlightProbe.js')
s = p.read_text()
for old, new in [
    ('function acquire(name, probe) {', 'function acquire(name, probe, identity = probe) {'),
    ('entry.probe !== probe', 'entry.identity !== identity'),
    ('entry = { probe, waiters: new Set() };', 'entry = { identity, waiters: new Set() };'),
    ('// Replaced test adapters may finish after their successor started.', '// Replaced adapters may finish after their successor started.'),
]:
    assert s.count(old) == 1, old
    s = s.replace(old, new)
p.write_text(s)

p = Path('src/services/dependencyHealthService.js')
s = p.read_text()
for old, new in [
    ('async payments() {\n    const result = await stellarService.ping();', 'async payments(ping = stellarService.ping) {\n    const result = await ping.call(stellarService);'),
    ('async fx() {\n    const result = await rateService.ping();', 'async fx(ping = rateService.ping) {\n    const result = await ping.call(rateService);'),
    ('    const subscription = forced ? null : probePool.acquire(name, probe);', '''    // Replacing an adapter must not keep joining its predecessor's hung call.
    // Capture that method now, including its receiver, before deferred execution.
    const adapter = probe === defaultProbes.payments ? stellarService.ping
      : probe === defaultProbes.fx ? rateService.ping : probe;
    const invoke = probe === defaultProbes.payments || probe === defaultProbes.fx
      ? () => probe(adapter) : probe;
    const subscription = forced ? null : probePool.acquire(name, invoke, adapter);'''),
]:
    assert s.count(old) == 1, old
    s = s.replace(old, new)
p.write_text(s)
