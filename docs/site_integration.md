# Интеграция триггер-сниппета на витрину groster.me

Клиентская часть сервиса «брошенная корзина». Сниппет [`app/static/trigger.js`](../app/static/trigger.js)
**намеренно тупой**: один таймер + один `POST /cart-ping`, без сбора поведения (152-ФЗ). Он **не читает
корзину сам** — витрина кормит его данными через три вызова. Рабочий эталон всех трёх — на странице
`GET /demo` (файл [`app/static/demo.html`](../app/static/demo.html)); открой её и смотри «живой лог».

> Платформа витрины не подтверждена. Примеры ниже иллюстративны — **сверить с реальной темой
> groster.me** (имена событий/DOM-хуки могут отличаться).

## Два способа подключения

### A. Один тег «всё сразу» — `track.js` (аналог track.leadhit.io, рекомендуется)
Универсальный загрузчик: вставляешь один тег с `clid`, он сам грузит `trigger.js`/`wheel.js`
и **авто-захватывает email из форм** (submit/blur → `identify({email})`). Логика живёт на
сервере — тег на сайте больше не меняется.
```html
<script>
  window.grConfig = { clid: 'ВАШ_ID', wheel: true };
  (function(){ var s=document.createElement('script'); s.async=true;
    s.src='https://groster.skypath.fun/track.js?ver='+Math.floor(Date.now()/1e8);
    var f=document.getElementsByTagName('script')[0]; f.parentNode.insertBefore(s,f); })();
</script>
```
`grConfig`: `clid` — id кабинета (сейчас бэкенд однотенантный, принимается «на вырост»);
`endpoint` — если API на другом домене (иначе берётся из origin самого `track.js`);
`wheel: true|{onceDays,force}` — включить попап колеса; `autoform: false` — выключить авто-захват.

**152-ФЗ:** авто-захват шлёт `identify({email})` **без** `consent` — email садится в
`cart_sessions` для идентификации, но **письма не будет** (подписчик заводится только при
явной галочке на оформлении/колесе).

#### Корзина на кастомной витрине — через `grDataLayer` (рекомендуется)
Состав корзины `track.js` сам не скрейпит из вёрстки (хрупко). Для кастома используйте
dataLayer-конвенцию: сайт пушит событие в массив `window.grDataLayer` — одной строкой в том
же месте, где обновляете бейдж корзины. Работает даже если пуш случился **до** загрузки
`track.js` (очередь дренится на старте):
```html
<!-- инициализировать массив ДО тега track.js, чтобы ранние пуши не потерялись -->
<script>window.grDataLayer = window.grDataLayer || [];</script>
```
```js
// на любое изменение корзины (add/remove/qty):
grDataLayer.push({ event: 'cart', items: [
  { product_id: '0057412', category_id: 'salfetki-bumajnye', price: 71.6, qty: 2 },
]});
grDataLayer.push({ event: 'clear' });                          // очистка корзины
grDataLayer.push({ event: 'identify', user_id: '42817' });     // логин
grDataLayer.push({ event: 'identify', email, consent });       // email + галочка на оформлении
```
Схема позиции — тот же контракт `Ping.CartItem` (см. «Три точки разводки»). `identify`
передаёт только те ключи, что указал (не затирает известный email в `null`). Своё имя
массива — `grConfig.datalayer: 'myLayer'`; выключить — `datalayer: false`.

Без `track.js` тот же результат даёт прямой вызов `groster.cart(...)` — см. способ B ниже.

### B. Ручное подключение `trigger.js` (полный контроль)
```html
<script src="https://groster.skypath.fun/trigger.js"
        data-endpoint="https://groster.skypath.fun" data-interval="45"></script>
```
`data-endpoint` — адрес сервиса триггеров (не витрины). Тег с `data-endpoint` авто-инициализируется.
Альтернатива без data-атрибутов: `groster.init({ endpoint: 'https://groster.skypath.fun' })`.

Инициализация делается **один раз** за загрузку. В SPA переинициализация при смене роута не нужна.

## Три точки разводки

### 1. Изменение корзины → `groster.cart(items)`
Звать на **любое** изменение: add / remove / смена qty / очистка.
```js
groster.cart([
  { product_id: '0057412', category_id: 'salfetki-bumajnye', price: 71.6, qty: 2 },
  { product_id: '0038921', category_id: 'kasaletki',        price: 11.8, qty: 1 },
]);
```
Контракт позиции (`app/cart.py` → `CartItem`):

| поле | тип | обязательно | примечание |
|---|---|---|---|
| `product_id` | string | да | id товара как в каталоге/1С |
| `category_id` | string | да | **должен совпадать с id категории из фида каталога/`top5`** — иначе подбор товаров в письме промахнётся |
| `price` | number | да | рубли, число (не строка с «₽») |
| `qty` | number | нет (деф. 1) | |

`category_id` и `price` берём **из стейта корзины на фронте** (см. решение по проекту) — бэкенд их не
резолвит. Очистка корзины = `groster.cart([])`.

### 2. Логин / известный пользователь → `groster.identify({ user_id })`
Как только знаем id пользователя магазина (после логина, или сразу если сессия залогинена):
```js
groster.identify({ user_id: '42817' });
```
Это приоритет 1 идентификации анонима (ROADMAP 3.4). Сниппет помнит личность в cookie между сессиями
(cookie-приоритет 3.4) — повторно звать при каждой загрузке не обязательно, но безвредно.

### 3. Email на оформлении + согласие → `groster.identify({ email, consent })`
Когда гость вводит email на шаге оформления/подписки:
```js
groster.identify({ email: 'user@example.com', consent: consentCheckbox.checked });
```
**152-ФЗ:** бэкенд заводит подписчика с `consent_at` **только при `consent: true`** (`app/cart.py`
→ `cart_ping`). `consent` = состояние галочки согласия на письма. Без согласия email хранится для
идентификации, но письма не будет. Отзыв согласия — `groster.identify({ consent: false })`.

Приоритеты «залогинен > email в сессии > cookie» решает **витрина** тем, что и когда передаёт;
cookie-приоритет сниппет закрывает сам.

## Примеры под стеки

### 1С-Битрикс
Корзина обновляется AJAX'ом компонента `sale.basket.basket`. Ловим кастомное событие обновления и
пересобираем позиции из актуального состава:
```js
// сверить имя события с темой; в разных сборках это OnBasketChange / кастомный триггер после AJAX
BX.addCustomEvent('OnBasketChange', function () {
  var items = collectBasketItems(); // прочитать из DOM корзины / из ответа AJAX
  groster.cart(items);
});
// email + согласие — на сабмите шага оформления sale.order.ajax:
orderForm.addEventListener('submit', function () {
  groster.identify({ email: emailInput.value, consent: consentInput.checked });
});
```

### Кастомный серверный рендер (PHP и т.п.)
Держим на фронте актуальный снапшот корзины и дёргаем `cart()` в обработчиках add-to-cart / изменения
количества (там же, где обновляете свой бейдж корзины):
```js
function onCartMutated(cartState) {
  groster.cart(cartState.lines.map(l => ({
    product_id: l.sku, category_id: l.categoryId, price: l.price, qty: l.qty
  })));
}
```

### SPA (React / Vue)
Эффект на стор корзины и на статус аутентификации:
```js
// React
useEffect(() => { groster.cart(cartLines); }, [cartLines]);
useEffect(() => { if (user) groster.identify({ user_id: user.id }); }, [user]);
// на форме оформления:
onSubmit(() => groster.identify({ email, consent }));
```

## Edge-cases и подводные камни
- **Пустая корзина / скрытая вкладка** — сниппет не пингует (по дизайну). Значит `identify()` при
  пустой корзине не отправит согласие: собирать email имеет смысл, когда в корзине есть товар (так и в
  реальном сценарии брошенной корзины).
- **Гость без email** — сессия не порождает письмо. Это ожидаемо (ROADMAP 3.4), не баг.
- **Очистка/оформление** — `groster.cart([])`; после реального заказа бэкенд сам отменит письмо
  (двойная проверка заказа), но слать `cart([])` всё равно правильно.
- **Дубли/частота** — кап и антидубль на бэке; на фронте частить `cart()` не страшно (сниппет шлёт
  только при смене состава либо по таймеру).
- **HTTPS** — витрина по https не постучится на http-endpoint; сервис триггеров должен быть за TLS.

## Истина по корзине (п.2 — закрыт)
Состав корзины на момент отправки письма берётся из **последнего пинга сниппета**
(`cart_sessions`), а не запросом в 1С. Поэтому прокидывать наш `session_id` в 1С не нужно —
никакой связки `session_id ↔ 1С` витрине настраивать не требуется. Достаточно вовремя звать
`groster.cart(...)` при изменениях корзины (в т.ч. `cart([])` при очистке).

## Как проверить
1. `make run`, открыть `http://localhost:8000/demo`.
2. Добавить товары → в сетевом табе (и в логе демо) виден `POST /cart-ping`; в БД — строка
   `cart_sessions` (`state=active`).
3. Ввести email, поставить галочку, «Оформить» → строка в `subscribers` с `consent_at`
   (или `anon:<email>` без user_id). Без галочки строки нет.
4. `make worker-cart` после таймаута ухода + grace → письмо через LogMailer в логе.
