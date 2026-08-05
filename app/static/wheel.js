/* groster.me — «Колесо фортуны»: скидки/промокоды одним спином.
 *
 * Идиома как у trigger.js: чистые функции (выбор приза + угловая математика)
 * тестируются под node (scripts/test_wheel.js), браузерная обвязка ниже — рисует
 * SVG-колесо и крутит его CSS-трансформом.
 *
 * Подключение:
 *   <div id="wheel"></div>
 *   <script src="wheel.js"></script>
 *   <script>grosterWheel.mount('#wheel', { prizes: [...] });</script>
 *
 * Геометрия: сектор 0 начинается сверху (12 часов) и идёт по часовой. Центр
 * сектора i на угле (i+0.5)*seg. Стрелка неподвижна сверху (0°). Колесо крутим
 * по часовой на R градусов — чтобы центр сектора i встал под стрелку, нужно
 * R ≡ -(i+0.5)*seg (mod 360). sectorAtPointer — обратная функция (для теста).
 */
(function (root) {
  'use strict';

  // --- Чистые функции (node-тестируемые) ---

  // Взвешенный выбор индекса приза. rnd ∈ [0,1). Вес <=0 — сектор недостижим.
  function pickWeighted(prizes, rnd) {
    var total = prizes.reduce(function (s, p) { return s + Math.max(0, p.weight || 0); }, 0);
    if (total <= 0) return 0; // все веса нулевые — деградируем в первый сектор
    var x = rnd * total;
    for (var i = 0; i < prizes.length; i++) {
      x -= Math.max(0, prizes[i].weight || 0);
      if (x < 0) return i;
    }
    return prizes.length - 1; // страховка от накопленной погрешности float
  }

  // Угол поворота (град), чтобы центр сектора index встал под стрелку сверху.
  // turns — сколько полных оборотов «накрутить» для эффекта (>=0).
  function targetRotation(index, n, turns) {
    var seg = 360 / n;
    return (turns || 0) * 360 - (index + 0.5) * seg;
  }

  // Какой сектор сейчас под стрелкой при повороте R. Обратна targetRotation.
  function sectorAtPointer(R, n) {
    var seg = 360 / n;
    var a = ((-R % 360) + 360) % 360;      // угол центра, попавшего наверх
    var i = Math.round(a / seg - 0.5);      // (i+0.5)*seg = a  →  i
    return ((i % n) + n) % n;
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { pickWeighted: pickWeighted, targetRotation: targetRotation, sectorAtPointer: sectorAtPointer };
    return; // под node дальше не идём — браузерной обвязке нужен document
  }

  // --- Браузерная обвязка ---

  var SVGNS = 'http://www.w3.org/2000/svg';
  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/; // тот же смысл, что valid_email на бэке

  function validEmail(v) { return !!v && EMAIL_RE.test(String(v).trim()) && v.length <= 254; }

  // Все стили виджета живут здесь (single source): на чужом сайте нет wheel.html,
  // поэтому CSS инжектится сам, один раз. Классы с префиксом gw- не конфликтуют с сайтом.
  var STYLE = `
.gw-root, .gw-modal-card {
  --purple:#bc39e5; --purple-d:#6a12a0; --purple-ink:#3a1152;
  --green:#35cc00; --green-d:#23a000; --yellow:#fecc00;
  font-family: Montserrat, -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
}
.gw-stage { position:relative; width:min(88vw,420px); aspect-ratio:1; margin:0 auto; }
.gw-svg { width:100%; height:100%; display:block; filter:drop-shadow(0 16px 34px rgba(0,0,0,.35)); border-radius:50%; }
.gw-wheel { transform-origin:250px 250px; transition:transform 5.2s cubic-bezier(.12,.67,.12,.99); }
.gw-label { font:800 16px Montserrat,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; }
.gw-pointer { position:absolute; top:-8px; left:50%; transform:translateX(-50%); width:0; height:0; border-left:15px solid transparent; border-right:15px solid transparent; border-top:30px solid #fff; z-index:3; filter:drop-shadow(0 4px 4px rgba(0,0,0,.3)); }
.gw-btn { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:104px; height:104px; border-radius:50%; cursor:pointer; z-index:2; border:6px solid #fff; color:#fff; font:800 16px/1 inherit; letter-spacing:.5px; background:radial-gradient(circle at 35% 30%,#4fe01a,var(--green) 55%,var(--green-d)); box-shadow:0 8px 18px rgba(0,0,0,.28),inset 0 -3px 8px rgba(0,0,0,.18); transition:transform .12s ease,box-shadow .12s ease; }
.gw-btn:hover:not(:disabled){ transform:translate(-50%,-50%) scale(1.07); }
.gw-btn:active:not(:disabled){ transform:translate(-50%,-50%) scale(.96); }
.gw-btn:disabled{ cursor:default; filter:grayscale(.55) brightness(.95); }
.gw-result { margin-top:26px; min-height:74px; opacity:0; transform:translateY(8px); transition:opacity .35s ease,transform .35s ease; }
.gw-result.gw-show{ opacity:1; transform:none; }
.gw-won{ font-size:21px; font-weight:700; margin-bottom:12px; }
.gw-won b{ color:var(--yellow); }
.gw-code{ display:inline-flex; align-items:center; gap:12px; cursor:pointer; padding:12px 18px; border-radius:50px; font:800 20px ui-monospace,"SF Mono",Menlo,monospace; letter-spacing:2px; color:var(--purple-ink); background:#fff; border:2px dashed var(--purple); user-select:all; transition:transform .12s ease,border-color .2s ease; }
.gw-code:hover{ transform:translateY(-1px); }
.gw-code.gw-copied{ border-style:solid; border-color:var(--green); }
.gw-copy{ font:800 11px/1 Montserrat,-apple-system,Arial,sans-serif; letter-spacing:.5px; color:#fff; background:var(--green); padding:5px 10px; border-radius:50px; }
.gw-hint{ margin-top:10px; color:rgba(255,255,255,.82); font-size:13px; }
.gw-lead{ max-width:380px; margin:0 auto 22px; text-align:left; }
.gw-email{ width:100%; padding:14px 18px; border-radius:50px; font-size:15px; font-family:inherit; color:var(--purple-ink); background:#fff; border:2px solid transparent; outline:none; transition:border-color .2s ease,box-shadow .2s ease; }
.gw-email::placeholder{ color:#a97ac0; }
.gw-email:focus{ border-color:var(--green); box-shadow:0 0 0 4px rgba(53,204,0,.25); }
.gw-consent{ display:flex; align-items:flex-start; gap:9px; margin-top:12px; padding:0 6px; color:rgba(255,255,255,.9); font-size:12.5px; line-height:1.4; cursor:pointer; user-select:none; }
.gw-consent input{ margin-top:2px; accent-color:var(--green); width:17px; height:17px; flex:none; }
.gw-error{ min-height:16px; margin-top:8px; padding:0 6px; color:#ffe08a; font-weight:600; font-size:12.5px; }
.gw-modal-overlay{ position:fixed; inset:0; z-index:99999; display:flex; align-items:center; justify-content:center; padding:16px; background:rgba(30,6,40,.62); -webkit-backdrop-filter:blur(3px); backdrop-filter:blur(3px); opacity:0; transition:opacity .25s ease; }
.gw-modal-overlay.gw-open{ opacity:1; }
.gw-modal-card{ position:relative; width:100%; max-width:640px; max-height:94vh; overflow-y:auto; border-radius:26px; padding:30px 24px 36px; text-align:center; color:#fff; background:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='46' height='46'><g fill='none' stroke='white' stroke-width='3' stroke-linecap='round' opacity='0.13'><path d='M12 6v12M6 12h12'/></g></svg>") 0 0/46px 46px, radial-gradient(120% 120% at 50% -10%,#d05cf5 0%,var(--purple) 44%,var(--purple-d) 100%); box-shadow:0 30px 70px rgba(0,0,0,.45); transform:translateY(14px) scale(.98); transition:transform .28s cubic-bezier(.2,.9,.3,1.2); }
.gw-modal-overlay.gw-open .gw-modal-card{ transform:none; }
.gw-modal-card .gw-stage{ width:min(82vw,460px); }
.gw-close{ position:absolute; top:14px; right:14px; width:34px; height:34px; border-radius:50%; border:0; cursor:pointer; background:rgba(255,255,255,.2); color:#fff; font-size:19px; line-height:1; display:grid; place-items:center; z-index:6; transition:background .15s ease; }
.gw-close:hover{ background:rgba(255,255,255,.35); }
.gw-m-title{ margin:4px 22px 6px; font-size:clamp(21px,4.5vw,29px); font-weight:900; line-height:1.1; }
.gw-m-title .acc{ color:var(--yellow); }
.gw-m-sub{ margin:0 0 20px; font-size:14px; font-weight:500; opacity:.92; }
@media (max-width:600px){ .gw-modal-card{ padding:26px 16px 30px; border-radius:22px; } }
`;

  function injectStyles() {
    if (document.getElementById('gw-style')) return;
    var s = document.createElement('style');
    s.id = 'gw-style';
    s.textContent = STYLE;
    document.head.appendChild(s);
  }

  function el(tag, attrs) {
    var e = document.createElementNS(SVGNS, tag);
    for (var k in attrs) if (attrs.hasOwnProperty(k)) e.setAttribute(k, attrs[k]);
    return e;
  }

  // Точка на окружности радиуса r под углом deg (0 = верх, по часовой).
  function polar(cx, cy, r, deg) {
    var rad = (deg - 90) * Math.PI / 180;
    return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
  }

  // SVG-путь одного сектора [a0, a1] (в градусах от верха).
  function sectorPath(cx, cy, r, a0, a1) {
    var p0 = polar(cx, cy, r, a0), p1 = polar(cx, cy, r, a1);
    var large = (a1 - a0) > 180 ? 1 : 0;
    return 'M' + cx + ',' + cy + ' L' + p0[0].toFixed(2) + ',' + p0[1].toFixed(2) +
      ' A' + r + ',' + r + ' 0 ' + large + ' 1 ' + p1[0].toFixed(2) + ',' + p1[1].toFixed(2) + ' Z';
  }

  // Фирменная палитра Гростера (совпадает с баннером): фиолетовый/жёлтый/зелёный/оранжевый.
  var DEFAULT_PRIZES = [
    { label: 'Скидка 5%',   code: 'GROSTER5',   weight: 30, color: '#bc39e5' },
    { label: 'Скидка 10%',  code: 'GROSTER10',  weight: 22, color: '#fecc00' },
    { label: 'Промокод 7%', code: 'LUCKY7',     weight: 20, color: '#35cc00' },
    { label: 'Скидка 15%',  code: 'GROSTER15',  weight: 12, color: '#fc6631' },
    { label: 'Подарок 🎁',  code: 'GIFTBOX',    weight: 8,  color: '#bc39e5' },
    { label: 'Скидка 25%',  code: 'GROSTER25',  weight: 5,  color: '#fecc00' },
    { label: 'Ещё разок',   code: null,         weight: 2,  color: '#35cc00', respin: true },
    { label: 'Скидка 50%',  code: 'JACKPOT50',  weight: 1,  color: '#fc6631' },
  ];

  // Цвет подписи под яркость сектора: тёмный на светлом (жёлтый), белый на насыщенном.
  function textOn(hex) {
    var c = String(hex).replace('#', '');
    if (c.length === 3) c = c.replace(/./g, '$&$&');
    var r = parseInt(c.substr(0, 2), 16), g = parseInt(c.substr(2, 2), 16), b = parseInt(c.substr(4, 2), 16);
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150 ? '#3a1152' : '#ffffff';
  }

  function mount(target, opts) {
    opts = opts || {};
    injectStyles();
    var host = typeof target === 'string' ? document.querySelector(target) : target;
    if (!host) throw new Error('grosterWheel: контейнер не найден');
    var prizes = (opts.prizes && opts.prizes.length) ? opts.prizes : DEFAULT_PRIZES;
    var n = prizes.length, seg = 360 / n;
    var R = 220, CX = 250, CY = 250; // радиус колеса и центр в системе viewBox 500x500
    var rnd = opts.random || Math.random;
    var onWin = opts.onWin || function () {};

    // --- SVG колеса ---
    var svg = el('svg', { viewBox: '0 0 500 500', class: 'gw-svg' });
    var wheel = el('g', { class: 'gw-wheel' }); // вращаем этот слой
    for (var i = 0; i < n; i++) {
      var a0 = i * seg, a1 = (i + 1) * seg, mid = a0 + seg / 2;
      var fill = prizes[i].color || (i % 2 ? '#fecc00' : '#bc39e5');
      wheel.appendChild(el('path', {
        d: sectorPath(CX, CY, R, a0, a1),
        fill: fill, stroke: '#ffffff', 'stroke-width': 3,
      }));
      var tp = polar(CX, CY, R * 0.62, mid);
      var txt = el('text', {
        x: tp[0].toFixed(1), y: tp[1].toFixed(1),
        transform: 'rotate(' + mid + ' ' + tp[0].toFixed(1) + ' ' + tp[1].toFixed(1) + ')',
        'text-anchor': 'middle', 'dominant-baseline': 'middle', class: 'gw-label',
        fill: prizes[i].text || textOn(fill),
      });
      txt.textContent = prizes[i].label;
      wheel.appendChild(txt);
    }
    svg.appendChild(wheel);
    svg.appendChild(el('circle', { cx: CX, cy: CY, r: R, fill: 'none', stroke: '#ffffff', 'stroke-width': 8 }));

    host.innerHTML = '';
    host.classList.add('gw-root');

    // endpoint === false → офлайн-демо без захвата; иначе POST /wheel-lead
    // (по умолчанию same-origin, если страницу отдаёт сам API).
    var endpoint = opts.endpoint === false ? false : String(opts.endpoint || '').replace(/\/+$/, '');

    // --- Email-gate: собираем почту ДО выдачи скидки (152-ФЗ: согласие обязательно) ---
    var lead = document.createElement('form');
    lead.className = 'gw-lead';
    lead.innerHTML =
      '<input class="gw-email" type="email" name="email" autocomplete="email" ' +
        'placeholder="Ваш email — и крутите колесо" required>' +
      '<label class="gw-consent"><input type="checkbox" class="gw-consent-cb"> ' +
        '<span>Согласен на обработку персональных данных и получение писем</span></label>' +
      '<div class="gw-error" role="alert"></div>';
    host.appendChild(lead);
    var emailEl = lead.querySelector('.gw-email');
    var consentEl = lead.querySelector('.gw-consent-cb');
    var errEl = lead.querySelector('.gw-error');

    var stage = document.createElement('div');
    stage.className = 'gw-stage';
    var pointer = document.createElement('div');
    pointer.className = 'gw-pointer';
    var btn = document.createElement('button');
    btn.className = 'gw-btn';
    btn.type = 'button';
    btn.textContent = 'Крутить';
    btn.disabled = true; // разблокируется, когда email валиден И согласие отмечено
    stage.appendChild(svg);
    stage.appendChild(pointer);
    stage.appendChild(btn);
    host.appendChild(stage);
    var result = document.createElement('div');
    result.className = 'gw-result';
    result.setAttribute('aria-live', 'polite');
    host.appendChild(result);

    // Длительность анимации. Держать в синхроне с transition у .gw-wheel в wheel.html.
    // ponytail: жёстко заданная константа — если поменяете css-transition, поменяйте и тут.
    var DURATION_MS = opts.durationMs || 5200;
    var spinning = false, spent = 0; // spent — накопленный угол, чтобы всегда крутить «вперёд»
    var captured = false;            // лид уже отправлен → повторные спины без нового POST
    var done = false;                // попытка израсходована (реальный приз) → колесо заблокировано
    var bonus = false;               // выпал «Ещё разок» → разрешён один доп. прокрут

    function gateOk() { return validEmail(emailEl.value) && consentEl.checked; }
    function syncBtn() { if (!spinning && !done && !captured) btn.disabled = !gateOk(); }
    emailEl.addEventListener('input', function () { errEl.textContent = ''; syncBtn(); });
    consentEl.addEventListener('change', function () { errEl.textContent = ''; syncBtn(); });

    // Отправка лида на бэк. cb(ok, detail). detail прокидываем сырым для разбора в onSpin.
    function postLead(email, cb) {
      var base = endpoint === false ? null : endpoint; // '' → same-origin
      if (base === null) { cb(true); return; }          // офлайн-демо: пропускаем захват
      root.fetch(base + '/wheel-lead', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email, consent: true }),
      }).then(function (r) {
        if (r.ok) { cb(true); return; }
        return r.json().catch(function () { return {}; }).then(function (j) { cb(false, j.detail); });
      }).catch(function () { cb(false, null); }); // сеть легла
    }

    // Клик по «Крутить»: гейт → (первый раз) захват+проверка на бэке → физический спин.
    function onSpin() {
      if (spinning || done) return;
      if (bonus) { doSpin(); return; }          // бонус-прокрут за «Ещё разок» — без POST
      if (!gateOk()) { errEl.textContent = 'Введите email и отметьте согласие'; syncBtn(); return; }
      if (captured) { doSpin(); return; }
      btn.disabled = true; errEl.textContent = '';
      postLead(emailEl.value.trim(), function (ok, detail) {
        if (ok) {
          captured = true;
          emailEl.disabled = true; consentEl.disabled = true; // фиксируем личность
          doSpin();
          return;
        }
        if (detail === 'already_spun') {                       // сервер: этот email уже крутил
          done = true; btn.disabled = true;
          errEl.textContent = 'С этого email колесо уже крутили 🎡';
          return;
        }
        errEl.textContent = detail === 'invalid_email' ? 'Проверьте email'
          : detail === 'consent_required' ? 'Нужно согласие'
          : 'Не удалось отправить. Попробуйте ещё раз.';
        btn.disabled = false;
      });
    }

    function doSpin() {
      if (spinning) return;
      spinning = true;
      btn.disabled = true;
      result.className = 'gw-result';
      result.innerHTML = '';

      var idx = pickWeighted(prizes, rnd());
      var turns = 5 + Math.floor(rnd() * 3);           // 5–7 полных оборотов
      var jitter = (rnd() - 0.5) * seg * 0.7;           // не робот: ±0.35 сектора, стрелка остаётся внутри
      var base = targetRotation(idx, n, turns) + jitter;
      // Крутим всегда вперёд от текущего накопленного угла.
      var next = spent + ((base - (spent % 360)) % 360 + 360) % 360 + turns * 360;
      spent = next;
      wheel.style.transform = 'rotate(' + next.toFixed(2) + 'deg)';
      // transitionend на SVG <g> ненадёжен (не везде стреляет) — завершаем по таймеру.
      setTimeout(finish, DURATION_MS + 80);
    }

    function finish() {
      if (!spinning) return;
      spinning = false;
      var idx = sectorAtPointer(spent, n);
      var prize = prizes[idx];
      showResult(prize);
      onWin(prize, idx);
      // «Ещё разок» → бонус-прокрут (попытка не израсходована); иначе колесо заблокировано.
      if (prize.respin) { bonus = true; done = false; btn.disabled = false; }
      else { bonus = false; done = true; btn.disabled = true; }
    }

    function showResult(prize) {
      result.className = 'gw-result gw-show';
      if (!prize.code) { // «пустой» сектор (напр. «Ещё разок»)
        result.innerHTML = '<div class="gw-won">' + prize.label + '</div>' +
          '<div class="gw-hint">Крутаните ещё раз 🎡</div>';
        return;
      }
      result.innerHTML =
        '<div class="gw-won">🎉 Ваш приз: <b>' + prize.label + '</b></div>' +
        '<div class="gw-code" role="button" tabindex="0" title="Нажмите, чтобы скопировать">' +
          prize.code + '<span class="gw-copy">копировать</span></div>' +
        '<div class="gw-hint">Промокод скопируется по клику</div>';
      var codeEl = result.querySelector('.gw-code');
      function copy() {
        var done = function () {
          codeEl.classList.add('gw-copied');
          codeEl.querySelector('.gw-copy').textContent = 'скопировано ✓';
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(prize.code).then(done, function () {});
        } else { done(); } // нет Clipboard API — просто подсветим, код виден
      }
      codeEl.addEventListener('click', copy);
      codeEl.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); copy(); } });
    }

    btn.addEventListener('click', onSpin);
    return { spin: onSpin, prizes: prizes };
  }

  // Попап-оверлей: колесо всплывает поверх сайта (адаптивно), а не отдельной страницей.
  // opts те же, что у mount (endpoint/onWin/prizes/random) + title/subtitle/onceDays/force/onClose.
  function popup(opts) {
    opts = opts || {};
    injectStyles();
    var KEY = 'gw_popup_shown';
    // «Раз в N дней»: не открываем повторно в окне onceDays. force:true — игнорит лимит.
    if (!opts.force && opts.onceDays) {
      try {
        var last = +localStorage.getItem(KEY) || 0;
        if (Date.now() - last < opts.onceDays * 864e5) return null;
      } catch (e) { /* приватный режим — просто покажем */ }
    }
    if (document.querySelector('.gw-modal-overlay')) return null; // уже открыт

    var overlay = document.createElement('div');
    overlay.className = 'gw-modal-overlay';
    var card = document.createElement('div');
    card.className = 'gw-modal-card';
    card.innerHTML =
      '<button class="gw-close" type="button" aria-label="Закрыть">✕</button>' +
      '<h2 class="gw-m-title">' + (opts.title || 'Колесо фортуны — <span class="acc">скидка до 50%</span>') + '</h2>' +
      '<p class="gw-m-sub">' + (opts.subtitle || 'Оставьте email, крутаните колесо и забирайте персональный промокод.') + '</p>' +
      '<div class="gw-popup-host"></div>';
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    var prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden'; // фон не скроллится под модалкой

    function close() {
      overlay.classList.remove('gw-open');
      document.body.style.overflow = prevOverflow;
      document.removeEventListener('keydown', onKey);
      setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 280);
      if (opts.onClose) opts.onClose();
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    card.querySelector('.gw-close').addEventListener('click', close);
    overlay.addEventListener('click', function (e) { if (e.target === overlay) close(); }); // клик по фону
    document.addEventListener('keydown', onKey);

    var api = mount(card.querySelector('.gw-popup-host'), opts);
    api.close = close;

    try { localStorage.setItem(KEY, String(Date.now())); } catch (e) { /* no-op */ }
    requestAnimationFrame(function () { overlay.classList.add('gw-open'); }); // плавный вход
    return api;
  }

  // Подтягивает конфиг из админки (GET /wheel-config) и открывает попап с этими призами/текстами.
  // Явно переданные opts всегда важнее сервера. Сеть легла / нет fetch → попап на дефолтах (fallback).
  function open(opts) {
    opts = opts || {};
    var base = opts.endpoint === false ? '' : String(opts.endpoint || '').replace(/\/+$/, '');
    function show(cfg) {
      var merged = {};
      for (var k in opts) if (opts.hasOwnProperty(k)) merged[k] = opts[k];
      if (cfg) {
        if (cfg.prizes && cfg.prizes.length && !opts.prizes) merged.prizes = cfg.prizes;
        if (cfg.title && opts.title == null) merged.title = cfg.title;
        if (cfg.subtitle && opts.subtitle == null) merged.subtitle = cfg.subtitle;
        if (cfg.once_days != null && opts.onceDays == null) merged.onceDays = cfg.once_days;
      }
      return popup(merged);
    }
    if (!root.fetch) return show(null);
    root.fetch(base + '/wheel-config')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(show, function () { show(null); });
    return null; // попап откроется асинхронно, когда придёт конфиг
  }

  root.grosterWheel = {
    mount: mount, popup: popup, open: open,
    pickWeighted: pickWeighted, targetRotation: targetRotation, sectorAtPointer: sectorAtPointer,
  };

  // Авто-открытие из data-атрибутов тега <script> (как у trigger.js):
  //   <script src="wheel.js" data-endpoint="https://..." data-auto="12" data-once-days="7"></script>
  // data-auto — задержка в секундах перед показом; data-once-days — не чаще раза в N дней.
  var self = document.currentScript;
  if (self && self.hasAttribute('data-auto')) {
    var delay = Math.max(0, parseInt(self.getAttribute('data-auto'), 10) || 0);
    var onceDays = parseInt(self.getAttribute('data-once-days'), 10);
    var endpoint = self.getAttribute('data-endpoint');
    var launch = function () {
      var o = { endpoint: endpoint || '' };
      if (!isNaN(onceDays)) o.onceDays = onceDays; // не задан в теге → возьмём once_days из конфига
      open(o);
    };
    var arm = function () { setTimeout(launch, delay * 1000); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', arm);
    else arm();
  }
})(typeof window !== 'undefined' ? window : this);
