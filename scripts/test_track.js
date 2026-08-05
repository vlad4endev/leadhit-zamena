/* Self-check чистой логики track.js. Запуск: node scripts/test_track.js */
const assert = require('assert');
const { isEmail, findEmail, normEvent } = require('../app/static/track.js');

// isEmail: та же граница доверия, что valid_email на бэке.
assert.ok(isEmail('a@b.ru') && isEmail(' user.name+x@mail.example.com '));
assert.ok(!isEmail(null) && !isEmail('') && !isEmail('noatsign') && !isEmail('a@b'));

// findEmail: type=email — самый надёжный сигнал, перебивает порядок полей.
assert.strictEqual(
  findEmail([{ type: 'text', value: 'ignored' }, { type: 'email', value: 'me@x.ru' }]),
  'me@x.ru');

// autocomplete=email тоже надёжный сигнал.
assert.strictEqual(findEmail([{ autocomplete: 'email', value: 'ac@x.ru' }]), 'ac@x.ru');

// Намёк по имени поля важнее «случайного» валидного значения в другом поле.
assert.strictEqual(
  findEmail([
    { type: 'text', name: 'coupon', value: 'promo@x.ru' },   // валидный, но не почта по смыслу
    { type: 'text', name: 'user_email', value: 'real@x.ru' },
  ]),
  'real@x.ru');

// Нет ни type, ни намёка — берём любое значение-email (лучше, чем ничего).
assert.strictEqual(findEmail([{ type: 'text', name: 'q', value: 'x@y.ru' }]), 'x@y.ru');

// Совсем нет email — null (не сработает identify).
assert.strictEqual(findEmail([{ type: 'text', value: 'hello' }]), null);
assert.strictEqual(findEmail([]), null);

// normEvent: контракт dataLayer для кастомной витрины.
const items = [{ product_id: '1', category_id: 'c', price: 10, qty: 2 }];
assert.deepStrictEqual(normEvent({ event: 'cart', items }), { call: 'cart', items });
assert.deepStrictEqual(normEvent({ event: 'cart' }), { call: 'cart', items: [] }, 'нет items → []');
assert.deepStrictEqual(normEvent({ event: 'clear' }), { call: 'cart', items: [] });
// identify: только переданные ключи (чтобы не затирать email → null на бэке).
assert.deepStrictEqual(
  normEvent({ event: 'identify', user_id: '42' }), { call: 'identify', ids: { user_id: '42' } });
assert.deepStrictEqual(
  normEvent({ event: 'identify', email: 'a@b.ru', consent: true }),
  { call: 'identify', ids: { email: 'a@b.ru', consent: true } });
// Мусор → null (не дёргаем groster).
assert.strictEqual(normEvent(null), null);
assert.strictEqual(normEvent({ event: 'pageview' }), null);
assert.strictEqual(normEvent('cart'), null);

console.log('track.js self-check OK');
