/* Self-check чистой логики trigger.js. Запуск: node scripts/test_trigger.js */
const assert = require('assert');
const { cartHash, shouldPing, normItem } = require('../app/static/trigger.js');

// shouldPing: слать только при активной вкладке И непустой корзине (ROADMAP 3.1).
assert.strictEqual(shouldPing(true, 2), true);
assert.strictEqual(shouldPing(true, 0), false, 'пустая корзина не пингуется');
assert.strictEqual(shouldPing(false, 3), false, 'скрытая вкладка не пингуется');

// cartHash: стабилен и не зависит от порядка ключей, но меняется на смену состава.
const a = [{ product_id: 'p1', price: 10, qty: 1 }];
const b = [{ qty: 1, price: 10, product_id: 'p1' }];
assert.strictEqual(cartHash(a), cartHash(b), 'порядок ключей не влияет');
assert.notStrictEqual(cartHash(a), cartHash([{ product_id: 'p1', price: 10, qty: 2 }]),
  'смена qty меняет hash');
assert.strictEqual(cartHash([]), cartHash(null), 'пусто и null дают один hash');

// normItem: приведение к контракту Ping.CartItem + дефолты.
assert.deepStrictEqual(
  normItem({ product_id: 42, category_id: 7, price: '99.5' }),
  { product_id: '42', qty: 1, category_id: '7', price: 99.5 },
);
// Минимальный контракт: одного product_id достаточно — необязательные поля не выдумываем
// (пустая строка/0 хуже отсутствия: бэкенд взял бы их за истину).
assert.deepStrictEqual(normItem({ product_id: 'p1' }), { product_id: 'p1', qty: 1 });
assert.deepStrictEqual(
  normItem({ product_id: 'p1', category_id: '', price: '' }),
  { product_id: 'p1', qty: 1 },
);
assert.deepStrictEqual(
  normItem({ product_id: 'p1', price: '71,60 ₽', qty: 2 }),
  { product_id: 'p1', qty: 2 }, 'цена-строка с валютой не превращается в 0',
);

console.log('trigger.js self-check OK');
