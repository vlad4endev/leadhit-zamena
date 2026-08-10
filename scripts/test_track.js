/* Self-check чистой логики track.js. Запуск: node scripts/test_track.js */
const assert = require('assert');
const { isEmail, findEmail, normEvent, ga4Event, applyGa4 } = require('../app/static/track.js');

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

// --- Адаптер GA4/GTM ---

// GTM-форма события: {event, ecommerce:{items}}; item_id → product_id, quantity → qty.
assert.deepStrictEqual(
  ga4Event({ event: 'add_to_cart', ecommerce: { items: [
    { item_id: '0057412', item_category: 'salfetki', price: 71.6, quantity: 2 },
  ]}}),
  { kind: 'add', items: [{ product_id: '0057412', qty: 2, category_id: 'salfetki', price: 71.6 }] });

// gtag-форма: ['event','add_to_cart',{items}]. Старая разметка зовёт id вместо item_id.
assert.deepStrictEqual(
  ga4Event(['event', 'add_to_cart', { items: [{ id: 'p1' }] }]),
  { kind: 'add', items: [{ product_id: 'p1', qty: 1 }] }, 'дефолт qty=1, пустых полей нет');

// Позиция без id бесполезна (нечего искать в каталоге) — отбрасывается.
assert.deepStrictEqual(ga4Event({ event: 'add_to_cart', items: [{ price: 10 }] }),
  { kind: 'add', items: [] });

// Не про корзину → null: адаптер не трогает состояние на page_view и прочей аналитике.
assert.strictEqual(ga4Event({ event: 'page_view' }), null);
assert.strictEqual(ga4Event(['config', 'G-XXX']), null);
assert.strictEqual(ga4Event(null), null);

// applyGa4: add копит, повторный add суммирует qty.
let st = applyGa4([], { event: 'add_to_cart', items: [{ item_id: 'p1', quantity: 1 }] });
assert.deepStrictEqual(st, [{ product_id: 'p1', qty: 1 }]);
st = applyGa4(st, { event: 'add_to_cart', items: [{ item_id: 'p1', quantity: 2 }] });
assert.deepStrictEqual(st, [{ product_id: 'p1', qty: 3 }], 'дельты складываются');

// remove вычитает, до нуля — выкидывает позицию.
st = applyGa4(st, { event: 'remove_from_cart', items: [{ item_id: 'p1', quantity: 1 }] });
assert.deepStrictEqual(st, [{ product_id: 'p1', qty: 2 }]);
st = applyGa4(st, { event: 'remove_from_cart', items: [{ item_id: 'p1', quantity: 5 }] });
assert.deepStrictEqual(st, [], 'вычитание ниже нуля удаляет позицию, а не даёт qty<0');

// remove неизвестного товара не создаёт мусор.
assert.deepStrictEqual(applyGa4([], { event: 'remove_from_cart', items: [{ item_id: 'x' }] }), []);

// view_cart/begin_checkout — снапшот: перезаписывает накопленное (лечит рассинхрон дельт).
assert.deepStrictEqual(
  applyGa4([{ product_id: 'p1', qty: 9 }], { event: 'view_cart', ecommerce: { items: [
    { item_id: 'p2', quantity: 1 },
  ]}}),
  [{ product_id: 'p2', qty: 1 }]);

// purchase — корзина пуста (заказ оформлен, письмо не нужно).
assert.deepStrictEqual(applyGa4([{ product_id: 'p1', qty: 1 }], { event: 'purchase' }), []);

// Чужое событие не меняет состояние (null → вызывающий не дёргает groster.cart).
assert.strictEqual(applyGa4([{ product_id: 'p1', qty: 1 }], { event: 'scroll' }), null);

// Состояние не мутируется на месте: прошлый снимок остаётся прежним.
const before = [{ product_id: 'p1', qty: 1 }];
applyGa4(before, { event: 'add_to_cart', items: [{ item_id: 'p1', quantity: 1 }] });
assert.deepStrictEqual(before, [{ product_id: 'p1', qty: 1 }], 'редьюсер чистый');

// --- Код заказа (ТЗ 2) ---

// Явный контракт витрины: событие order уходит в groster.order() как есть.
assert.deepStrictEqual(
  normEvent({ event: 'order', order_id: 'A-1', total: 100, items: [{ product_id: 'p1' }] }),
  { call: 'order', order: { event: 'order', order_id: 'A-1', total: 100,
                            items: [{ product_id: 'p1' }] } });

// GA4 purchase несёт транзакцию: номер заказа и сумму берём из уже готовой разметки,
// на thank-you page витрине дописывать нечего.
const pur = ga4Event({ event: 'purchase', ecommerce: {
  transaction_id: 'ГР-184213', value: 143.2,
  items: [{ item_id: 'p1', price: 71.6, quantity: 2 }] } });
assert.strictEqual(pur.kind, 'clear', 'purchase по-прежнему очищает корзину');
assert.deepStrictEqual(pur.order, {
  order_id: 'ГР-184213', total: 143.2,
  items: [{ product_id: 'p1', qty: 2, price: 71.6 }] });

// gtag-форма того же события.
assert.strictEqual(
  ga4Event(['event', 'purchase', { transaction_id: 'A-2', value: 10 }]).order.order_id, 'A-2');

// purchase без номера заказа — только очистка корзины: атрибутировать нечего, а слать
// заказ без order_id нельзя (идемпотентность на сервере держится именно на нём).
assert.strictEqual(ga4Event({ event: 'purchase' }).order, undefined);
assert.strictEqual(ga4Event({ event: 'purchase', ecommerce: { transaction_id: '' } }).order,
  undefined);

// Обычные события корзины заказа не порождают.
assert.strictEqual(ga4Event({ event: 'add_to_cart', items: [{ item_id: 'p1' }] }).order, undefined);

console.log('track.js self-check OK');
