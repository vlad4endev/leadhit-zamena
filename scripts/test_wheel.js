/* Self-check чистой логики wheel.js. Запуск: node scripts/test_wheel.js */
const assert = require('assert');
const { pickWeighted, targetRotation, sectorAtPointer } = require('../app/static/wheel.js');

// pickWeighted: границы кумулятивных весов попадают в нужный сектор.
const P = [{ weight: 1 }, { weight: 3 }, { weight: 6 }]; // total 10 → [0,.1) [.1,.4) [.4,1)
assert.strictEqual(pickWeighted(P, 0.0), 0);
assert.strictEqual(pickWeighted(P, 0.05), 0);
assert.strictEqual(pickWeighted(P, 0.1), 1, 'граница веса → следующий сектор');
assert.strictEqual(pickWeighted(P, 0.39), 1);
assert.strictEqual(pickWeighted(P, 0.4), 2);
assert.strictEqual(pickWeighted(P, 0.999), 2);
assert.strictEqual(pickWeighted([{ weight: 0 }, { weight: 0 }], 0.7), 0, 'все веса 0 → первый');

// Нулевой вес недостижим: rnd=1 не должен вернуть сектор с weight 0.
assert.strictEqual(pickWeighted([{ weight: 5 }, { weight: 0 }], 0.9999), 0, 'weight 0 недостижим');

// targetRotation ↔ sectorAtPointer: обратны при любом числе секторов и оборотов.
for (const n of [2, 3, 6, 8, 12]) {
  for (let i = 0; i < n; i++) {
    for (const turns of [0, 5, 7]) {
      const R = targetRotation(i, n, turns);
      assert.strictEqual(sectorAtPointer(R, n), i, `n=${n} i=${i} turns=${turns} round-trip`);
    }
  }
}

// Джиттер до ±0.5 сектора не меняет победителя (стрелка остаётся внутри сектора).
const n = 8, seg = 360 / n;
for (let i = 0; i < n; i++) {
  for (const j of [-0.49, -0.2, 0, 0.2, 0.49]) {
    const R = targetRotation(i, n, 6) + j * seg;
    assert.strictEqual(sectorAtPointer(R, n), i, `джиттер ${j}*seg не меняет сектор i=${i}`);
  }
}

console.log('wheel.js self-check OK');
