/* groster.me — универсальный загрузчик-трекер (аналог track.leadhit.io/track.js).
 *
 * Идея LeadHIT: клиент вставляет ОДИН тонкий тег с идентификатором кабинета, а вся
 * логика живёт на сервере в track.js. Тег на сайте не меняется — трекер правится тут.
 *
 * Подключение (один тег, аналог lh_clid у LeadHIT):
 *   <script>
 *     window.grConfig = { clid: 'ВАШ_ID', wheel: true };
 *     (function(){ var s=document.createElement('script'); s.async=true;
 *       s.src='https://groster.skypath.fun/track.js?ver='+Math.floor(Date.now()/1e8);
 *       var f=document.getElementsByTagName('script')[0]; f.parentNode.insertBefore(s,f); })();
 *   </script>
 *
 * Что делает автоматически (в отличие от «намеренно тупого» trigger.js, который сайт
 * кормит руками):
 *   1. Грузит trigger.js и инициализирует groster (session-ping корзины).
 *   2. autoform (деф. вкл): ловит submit форм и вытаскивает email → groster.identify({email}).
 *      БЕЗ consent — только деанонимизация сессии (email в cart_sessions), письма НЕ шлём
 *      (152-ФЗ: подписчик заводится лишь при явной галочке на оформлении/колесе).
 *   3. wheel (деф. выкл): грузит wheel.js и открывает попап колеса.
 *   4. datalayer (деф. вкл): дренит window.grDataLayer → cart()/identify(). Состав корзины
 *      из вёрстки НЕ скрейпим (хрупко) — сайт пушит его сам (docs/site_integration.md).
 *   5. ga4 (деф. вкл): читает ecommerce-события GA4/GTM из window.dataLayer, если они на
 *      сайте уже есть → корзина без единой строки кода на витрине. Явный пуш в grDataLayer
 *      старше: как только сайт сказал состав сам, адаптер замолкает.
 *
 * clid: сейчас бэкенд однотенантный — clid принимается, но не влияет на маршрутизацию.
 * ponytail: мультитенант по clid добавим, когда появится второй клиент (YAGNI).
 */
(function (root) {
  'use strict';

  // --- Чистые функции (node-тестируемые, см. scripts/test_track.js) ---

  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/; // тот же смысл, что valid_email на бэке

  function isEmail(v) {
    return !!v && EMAIL_RE.test(String(v).trim()) && String(v).length <= 254;
  }

  // Достаём email из полей формы. fields: [{type,name,id,placeholder,autocomplete,value}].
  // Приоритет: type=email → autocomplete=email → «почтовое» имя/id/placeholder → любое
  // поле со значением-email. Возвращает нормализованный email или null.
  function findEmail(fields) {
    fields = fields || [];
    var hinted = null, anyValid = null;
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i] || {};
      var v = String(f.value == null ? '' : f.value).trim();
      if (!isEmail(v)) continue;
      anyValid = anyValid || v;
      var type = String(f.type || '').toLowerCase();
      var ac = String(f.autocomplete || '').toLowerCase();
      var tag = (String(f.name || '') + ' ' + String(f.id || '') + ' ' +
                 String(f.placeholder || '')).toLowerCase();
      if (type === 'email' || ac === 'email') return v;               // самый надёжный сигнал
      if (!hinted && /mail|почт|e-?mail/.test(tag)) hinted = v;        // намёк по имени поля
    }
    return hinted || anyValid || null; // hinted важнее «случайного» валидного значения
  }

  // Маппер события dataLayer → вызов groster. Возвращает {call,...} или null (мусор).
  // Контракт для кастомной витрины (пуш в window.grDataLayer):
  //   { event:'cart', items:[{product_id,category_id,price,qty}] }  → groster.cart(items)
  //   { event:'clear' }                                             → groster.cart([])
  //   { event:'identify', user_id, email, consent }                 → groster.identify(...)
  function normEvent(e) {
    if (!e || typeof e !== 'object') return null;
    if (e.event === 'cart') return { call: 'cart', items: Array.isArray(e.items) ? e.items : [] };
    if (e.event === 'clear') return { call: 'cart', items: [] };
    if (e.event === 'identify') {
      var ids = {};
      if (e.user_id != null) ids.user_id = e.user_id;
      if (e.email != null) ids.email = e.email;
      if (e.consent != null) ids.consent = e.consent;
      return { call: 'identify', ids: ids };
    }
    return null;
  }

  // --- Адаптер GA4/GTM: используем разметку, которая на витрине уже есть ---
  // Битрикс, Woo, Shopify, любая GTM-разметка электронной торговли пушат ecommerce-события
  // GA4 в window.dataLayer. Читаем их — тогда витрине не нужно писать ни строки под нас.
  // Состав корзины GA4 не отдаёт целиком, поэтому копим его сами: add/remove — дельты,
  // view_cart/begin_checkout — полный снапшот (перезаписывает состояние), purchase — очистка.
  var GA4_KIND = {
    add_to_cart: 'add', remove_from_cart: 'remove',
    view_cart: 'set', begin_checkout: 'set', purchase: 'clear',
  };

  // Событие бывает объектом ({event, ecommerce:{items}}) и gtag-массивом
  // (['event','add_to_cart',{items}]). Возвращает {kind, items} или null (не про корзину).
  function ga4Event(raw) {
    if (!raw || typeof raw !== 'object') return null;
    var name, payload;
    if (typeof raw.length === 'number' && raw[0] === 'event') {
      name = raw[1]; payload = raw[2] || {};            // gtag('event', 'add_to_cart', {...})
    } else {
      name = raw.event; payload = raw.ecommerce || raw; // GTM dataLayer.push({...})
    }
    var kind = GA4_KIND[name];
    if (!kind) return null;
    var src = payload.items || (payload.ecommerce && payload.ecommerce.items) || [];
    var items = [];
    for (var i = 0; i < src.length; i++) {
      var it = src[i] || {};
      // GA4 зовёт id товара item_id; UA-разметка и часть тем — id/item_sku.
      var id = it.item_id != null ? it.item_id : (it.id != null ? it.id : it.item_sku);
      if (id == null || id === '') continue;
      var qty = Number(it.quantity != null ? it.quantity : it.qty) || 1;
      var out = { product_id: String(id), qty: qty };
      if (it.item_category) out.category_id = String(it.item_category);
      var price = Number(it.price);
      if (it.price != null && it.price !== '' && !isNaN(price)) out.price = price;
      items.push(out);
    }
    return { kind: kind, items: items };
  }

  function copyItem(i) {
    var o = { product_id: i.product_id, qty: i.qty };
    if (i.category_id != null) o.category_id = i.category_id;
    if (i.price != null) o.price = i.price;
    return o;
  }

  // Чистый редьюсер: (состояние, событие) → новое состояние или null (событие не наше).
  function applyGa4(state, raw) {
    var e = ga4Event(raw);
    if (!e) return null;
    if (e.kind === 'clear') return [];
    if (e.kind === 'set') return e.items;
    var out = (state || []).map(copyItem);
    for (var i = 0; i < e.items.length; i++) {
      var it = e.items[i], hit = null;
      for (var j = 0; j < out.length; j++) {
        if (out[j].product_id === it.product_id) { hit = out[j]; break; }
      }
      if (e.kind === 'add') {
        if (hit) hit.qty += it.qty;
        else out.push(copyItem(it));
      } else if (hit) {
        hit.qty -= it.qty;
        if (hit.qty <= 0) out.splice(out.indexOf(hit), 1);
      }
    }
    return out;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      isEmail: isEmail, findEmail: findEmail, normEvent: normEvent,
      ga4Event: ga4Event, applyGa4: applyGa4,
    };
    return; // под node дальше не идём — браузерной обвязке нужен document
  }

  // --- Браузерная обвязка ---

  var doc = root.document;
  var cfg = root.grConfig || {};

  // Endpoint = origin самого track.js (как LeadHIT берёт track.leadhit.io). Явный
  // grConfig.endpoint важнее — на случай, если тег и API на разных доменах.
  function selfEndpoint() {
    if (cfg.endpoint) return String(cfg.endpoint).replace(/\/+$/, '');
    // currentScript у динамически вставленного тега бывает null → фолбэк: ищем свой src.
    var s = doc.currentScript;
    if (!s) {
      var all = doc.getElementsByTagName('script');
      for (var i = 0; i < all.length; i++) {
        if (/\/track\.js(\?|$)/.test(all[i].src)) { s = all[i]; break; }
      }
    }
    try { return s ? new URL(s.src).origin : ''; } catch (e) { return ''; }
  }
  var ENDPOINT = selfEndpoint();

  function loadScript(path, cb) {
    var s = doc.createElement('script');
    s.async = true;
    s.src = ENDPOINT + path;
    s.onload = function () { if (cb) cb(); };
    s.onerror = function () { /* сеть легла — трекер деградирует молча, сайт не ломаем */ };
    var f = doc.getElementsByTagName('script')[0];
    f.parentNode.insertBefore(s, f);
  }

  // --- Авто-захват email из форм (деанонимизация, 152-ФЗ-safe: без consent) ---

  function fieldsOf(scope) {
    var out = [], nodes = scope.querySelectorAll('input, textarea');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      out.push({
        type: n.type, name: n.name, id: n.id,
        placeholder: n.placeholder, autocomplete: n.autocomplete, value: n.value,
      });
    }
    return out;
  }

  function installAutoForm() {
    var lastSent = null;
    function capture(scope) {
      var email = findEmail(fieldsOf(scope));
      if (email && email !== lastSent && root.groster) {
        lastSent = email;
        root.groster.identify({ email: email }); // без consent → только идентификация
      }
    }
    // Основной сигнал — submit формы (пользователь осознанно отдал данные).
    doc.addEventListener('submit', function (e) {
      if (e.target && e.target.tagName === 'FORM') capture(e.target);
    }, true);
    // Плюс blur email-поля: ловим и до сабмита (частый кейс — уход со страницы оформления).
    doc.addEventListener('blur', function (e) {
      var t = e.target;
      if (t && (t.type === 'email' || String(t.autocomplete).toLowerCase() === 'email')) {
        capture(t.form || doc);
      }
    }, true);
  }

  // --- dataLayer: единый контракт для кастомной витрины ---
  // Сайт пушит события в массив (деф. window.grDataLayer), даже ДО загрузки track.js —
  // очередь дренится на старте. Классический паттерн: переопределяем push().
  // Явный пуш корзины сайтом старше вывода из GA4: если витрина сама говорит состав,
  // адаптер замолкает, чтобы дельты GA4 не боролись со снапшотами сайта.
  var siteOwnsCart = false;

  function installDataLayer(name) {
    var q = root[name] = root[name] || [];
    function process(e) {
      var m = normEvent(e);
      if (!m || !root.groster) return;
      if (m.call === 'cart') { siteOwnsCart = true; root.groster.cart(m.items); }
      else root.groster.identify(m.ids);
    }
    var buffered = q.slice(); // то, что сайт напушил до нас
    q.length = 0;
    q.push = function () {
      for (var i = 0; i < arguments.length; i++) {
        Array.prototype.push.call(q, arguments[i]);
        process(arguments[i]);
      }
      return q.length;
    };
    for (var j = 0; j < buffered.length; j++) process(buffered[j]);
  }

  // Подписка на чужой (GA4/GTM) dataLayer. Массив НЕ трогаем и не чистим — он принадлежит
  // GTM: только оборачиваем push, сохраняя исходное поведение, и разбираем уже накопленное.
  function installGa4(name) {
    var dl = root[name] = root[name] || [];
    var state = [];
    function process(raw) {
      if (siteOwnsCart || !root.groster) return;
      var next = applyGa4(state, raw);
      if (!next) return;
      state = next;
      root.groster.cart(state);
    }
    var origPush = dl.push;
    dl.push = function () {
      var r = origPush.apply(dl, arguments);
      for (var i = 0; i < arguments.length; i++) process(arguments[i]);
      return r;
    };
    for (var j = 0; j < dl.length; j++) process(dl[j]);
  }

  // --- Bootstrap ---

  loadScript('/trigger.js', function () {
    if (root.groster) {
      root.groster.init({ endpoint: ENDPOINT, intervalSec: cfg.interval, debug: cfg.debug });
    }
    if (cfg.autoform !== false) installAutoForm();
    if (cfg.datalayer !== false) installDataLayer(cfg.datalayer || 'grDataLayer');
    // Адаптер GA4 включён по умолчанию (решение владельца): интеграция «из коробки» важнее,
    // чем независимость от чужой разметки. Принятый риск — правка item_id в GTM ломает
    // корзину; ловится диагностикой «товары не из каталога» в админке. Выкл: grConfig.ga4=false.
    var ga4 = cfg.ga4 === undefined ? 'dataLayer' : cfg.ga4;
    if (ga4) installGa4(typeof ga4 === 'string' ? ga4 : 'dataLayer');
  });

  if (cfg.wheel) {
    loadScript('/wheel.js', function () {
      if (root.grosterWheel) {
        var w = (cfg.wheel === true) ? {} : cfg.wheel; // wheel:true → дефолты; объект → opts
        root.grosterWheel.open({ endpoint: ENDPOINT, onceDays: w.onceDays, force: w.force });
      }
    });
  }
})(typeof window !== 'undefined' ? window : this);
