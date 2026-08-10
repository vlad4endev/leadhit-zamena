/* groster.me — триггер-сниппет «брошенная корзина» (session-ping, ROADMAP 3.1).
 *
 * Намеренно тупой: один таймер, один POST. Никакого сбора поведения (просмотры,
 * клики, история) — только heartbeat «вкладка жива + корзина непуста» (152-ФЗ).
 *
 * Подключение (авто-init из data-атрибутов тега):
 *   <script src="https://groster.skypath.fun/trigger.js"
 *           data-endpoint="https://groster.skypath.fun" data-interval="45"></script>
 *
 * Сайт кормит сниппет тремя вызовами:
 *   groster.cart([{product_id, category_id, price, qty}])  // на каждое изменение корзины
 *   groster.identify({ user_id, email })                   // при логине / вводе email на оформлении
 *   groster.order({ order_id, total, items })              // на thank-you page (ТЗ 2)
 *
 * Идентификация анонима (ROADMAP 3.4): приоритеты (залогинен > email в сессии > cookie)
 * решает интеграция на сайте — что передали в identify(), то и шлём. Cookie-приоритет
 * закрыт здесь: последнюю известную личность сниппет помнит между сессиями.
 */
(function (root) {
  'use strict';

  // --- Чистые функции (тестируются под node, см. scripts/test_trigger.js) ---

  // FNV-1a 32-бит по стабильному JSON корзины. Бэкенд ловит смену состава по cart_hash.
  function cartHash(items) {
    var s = JSON.stringify(
      (items || []).map(function (i) {
        return [String(i.product_id), Number(i.price) || 0, Number(i.qty) || 0];
      })
    );
    var h = 0x811c9dc5;
    for (var k = 0; k < s.length; k++) {
      h ^= s.charCodeAt(k);
      h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
    }
    return ('0000000' + h.toString(16)).slice(-8);
  }

  // Слать ли сейчас: вкладка активна И корзина непуста (ROADMAP 3.1).
  function shouldPing(visible, itemCount) {
    return !!visible && itemCount > 0;
  }

  // Нормализация позиции к контракту Ping.CartItem (лишнее backend игнорит).
  // Обязателен только product_id: цену/название/фото письмо берёт из каталога. category_id
  // и price шлём, лишь если витрина их знает — пустышки вместо них хуже, чем их отсутствие.
  function normItem(i) {
    var out = { product_id: String(i.product_id), qty: Number(i.qty) || 1 };
    if (i.category_id != null && i.category_id !== '') out.category_id = String(i.category_id);
    var price = Number(i.price);
    if (i.price != null && i.price !== '' && !isNaN(price)) out.price = price;
    return out;
  }

  // Нормализация транзакции с thank-you page к контракту POST /order. null — слать нечего.
  // В отличие от корзины price шлём всегда, пусть нулём: из позиций считается выручка
  // письма в атрибуции, и «поля нет» бэкенду интерпретировать не во что.
  function normOrder(o) {
    o = o || {};
    var id = String(o.order_id == null ? '' : o.order_id).trim();
    if (!id) return null;
    var src = o.items || [], items = [];
    for (var i = 0; i < src.length; i++) {
      var it = src[i] || {};
      var pid = String(it.product_id == null ? '' : it.product_id).trim();
      if (!pid) continue;
      var price = Number(it.price);
      items.push({
        product_id: pid,
        qty: Number(it.qty) || 1,
        price: (it.price != null && it.price !== '' && !isNaN(price)) ? price : 0,
      });
    }
    var out = { order_id: id, items: items };
    var total = Number(o.total);
    if (o.total != null && o.total !== '' && !isNaN(total)) out.total = total;
    return out;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      cartHash: cartHash, shouldPing: shouldPing, normItem: normItem, normOrder: normOrder,
    };
    return; // под node на этом всё — браузерный бутстрап ниже требует document
  }

  // --- Браузерная обвязка ---

  var doc = root.document;

  function cookie(name, value, days) {
    if (value === undefined) {
      var m = doc.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
      return m ? decodeURIComponent(m[1]) : null;
    }
    var exp = new Date(Date.now() + days * 864e5).toUTCString();
    doc.cookie = name + '=' + encodeURIComponent(value) + '; expires=' + exp +
      '; path=/; SameSite=Lax';
  }

  function uuid() {
    if (root.crypto && root.crypto.randomUUID) return root.crypto.randomUUID();
    return 'sid-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
  }

  var S = {
    endpoint: null,
    intervalSec: 45,
    sid: null,
    user_id: null,
    email: null,
    consent: false,   // явное согласие на письма (галочка на оформлении, 152-ФЗ)
    items: [],
    hash: null,
    timer: null,
    debug: false,
  };

  // Debug-режим для интегратора: ?grdebug=1 в адресе (или init({debug:true})) — печатает
  // каждый вызов и ответ сервера, включая незнакомые каталогу product_id. В прод-режиме
  // не тратим ни строки в консоли и не читаем тело ответа.
  function log() {
    if (!S.debug) return;
    try { console.log.apply(console, ['[groster]'].concat([].slice.call(arguments))); } catch (e) {}
  }

  function post(path, body) {
    if (!S.endpoint) return;
    try {
      var p = root.fetch(S.endpoint + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        keepalive: true,       // долетит даже при закрытии вкладки
        credentials: 'omit',   // session_id в теле, cookie сервису не нужен
      });
      if (!S.debug) { p.catch(function () {}); return; } // fire-and-forget: сбой сети не ломает сайт
      log('POST ' + path, body);
      p.then(function (r) { return r.ok ? r.json() : { http: r.status }; })
        .then(function (d) {
          log('ответ', d);
          if (d && d.unknown && d.unknown.length) {
            console.warn('[groster] нет в каталоге product_id: ' + d.unknown.join(', ') +
              ' — письмо по такой корзине не уйдёт');
          }
          if (d && d.total_mismatch != null) {
            console.warn('[groster] сумма заказа по версии сайта ' + d.total_mismatch +
              ' ≠ сумме позиций ' + d.total + ' — проверьте, что price идёт числом');
          }
        }, function (e) { log('сеть недоступна', e); });
    } catch (e) { /* no-op */ }
  }

  function send() {
    post('/cart-ping', {
      session_id: S.sid,
      user_id: S.user_id || null,
      email: S.email || null,
      cart_items: S.items,
      cart_hash: S.hash,
      consent: S.consent || null,
    });
  }

  function pingIfDue() {
    if (shouldPing(doc.visibilityState !== 'hidden', S.items.length)) send();
  }

  var api = {
    init: function (opts) {
      opts = opts || {};
      S.debug = !!opts.debug || /[?&]grdebug=1/.test(root.location.search);
      if (opts.endpoint) S.endpoint = String(opts.endpoint).replace(/\/+$/, '');
      if (opts.intervalSec) S.intervalSec = Math.max(15, Number(opts.intervalSec) || 45);
      S.sid = cookie('gr_sid') || uuid();
      cookie('gr_sid', S.sid, 365);
      // Cookie-приоритет идентификации (3.4): помним личность между сессиями.
      var saved = cookie('gr_id');
      if (saved) {
        try {
          var p = JSON.parse(saved);
          S.user_id = p.user_id || null;
          S.email = p.email || null;
          S.consent = !!p.consent;
        } catch (e) { /* битый cookie — игнор */ }
      }
      if (S.timer) clearInterval(S.timer);
      S.timer = setInterval(pingIfDue, S.intervalSec * 1000);
      // Возврат на вкладку → сразу heartbeat (иначе ложный «уход» на переключении табов).
      doc.addEventListener('visibilitychange', function () {
        if (doc.visibilityState !== 'hidden') pingIfDue();
      });
      log('init', { endpoint: S.endpoint, intervalSec: S.intervalSec, session_id: S.sid });
      return api;
    },

    cart: function (items) {
      S.items = (items || []).map(normItem);
      var h = cartHash(S.items);
      var changed = h !== S.hash;
      S.hash = h;
      log('cart()', S.items, changed ? '(состав изменился)' : '(без изменений)');
      if (!S.items.length) log('корзина пуста → пинги остановлены (это норма)');
      if (changed) pingIfDue(); // реальная активность → свежий last_ping_at
      return api;
    },

    identify: function (ids) {
      ids = ids || {};
      if (ids.user_id != null) S.user_id = String(ids.user_id);
      if (ids.email != null) S.email = String(ids.email);
      if (ids.consent != null) S.consent = !!ids.consent; // отзыв: identify({consent:false})
      cookie('gr_id', JSON.stringify({ user_id: S.user_id, email: S.email, consent: S.consent }), 365);
      log('identify()', { user_id: S.user_id, email: S.email, consent: S.consent });
      if (S.email && !S.consent) log('email без consent → только идентификация, письма не будет');
      pingIfDue();
      return api;
    },

    // Покупка совершена (thank-you page). Останавливает серию «брошенная корзина» и даёт
    // атрибуции факт заказа с выручкой. Повтор по тому же order_id сервер игнорирует —
    // клиентский дедуп не нужен, F5 на странице «спасибо» безопасен.
    order: function (o) {
      var body = normOrder(o);
      if (!body) { log('order() без order_id — игнор'); return api; }
      body.session_id = S.sid;
      body.user_id = S.user_id || null;
      body.email = S.email || null;
      log('order()', body);
      post('/order', body);
      // Корзины больше нет → heartbeat прекращается сам (shouldPing по itemCount).
      S.items = [];
      S.hash = cartHash(S.items);
      return api;
    },
  };

  root.groster = api;

  // Авто-init из data-атрибутов тега <script>, если заданы.
  var self = doc.currentScript;
  if (self && self.getAttribute('data-endpoint')) {
    api.init({
      endpoint: self.getAttribute('data-endpoint'),
      intervalSec: self.getAttribute('data-interval'),
      debug: self.hasAttribute('data-debug'),
    });
  }
})(typeof window !== 'undefined' ? window : this);
