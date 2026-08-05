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
 *   4. autocart: НЕ универсальный DOM-скрейп (хрупко). Даёшь селекторы в grConfig.cart —
 *      трекер их читает; иначе состав корзины по-прежнему шлёт сайт через groster.cart().
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

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { isEmail: isEmail, findEmail: findEmail, normEvent: normEvent };
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
  function installDataLayer(name) {
    var q = root[name] = root[name] || [];
    function process(e) {
      var m = normEvent(e);
      if (!m || !root.groster) return;
      if (m.call === 'cart') root.groster.cart(m.items);
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

  // --- Bootstrap ---

  loadScript('/trigger.js', function () {
    if (root.groster) root.groster.init({ endpoint: ENDPOINT, intervalSec: cfg.interval });
    if (cfg.autoform !== false) installAutoForm();
    if (cfg.datalayer !== false) installDataLayer(cfg.datalayer || 'grDataLayer');
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
