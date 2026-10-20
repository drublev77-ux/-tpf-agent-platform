/* Google Analytics only with the visitor's consent (GDPR). Until "Accept" is pressed nothing is loaded from
   Google and no cookie is set; the choice is kept in this browser (localStorage "tpf_consent") and can be
   changed from the privacy page (tpfConsent.open()). Vercel Web Analytics, which sets no cookies, runs as before. */
(function () {
  var ID = 'G-P32QFSFR4D', KEY = 'tpf_consent';
  var T = {
    ru: { text: 'Мы хотим подключить Google Analytics, чтобы понимать, как посетители пользуются сайтом. Он ставит cookie — только если вы согласны.', ok: 'Принять', no: 'Отклонить', more: 'Подробнее' },
    en: { text: 'We would like to use Google Analytics to understand how visitors use the site. It sets cookies — only if you agree.', ok: 'Accept', no: 'Reject', more: 'Learn more' },
    nl: { text: 'Wij willen Google Analytics gebruiken om te begrijpen hoe bezoekers de site gebruiken. Het plaatst cookies — alleen als u akkoord gaat.', ok: 'Accepteren', no: 'Weigeren', more: 'Meer informatie' },
    zh: { text: '我们希望使用 Google Analytics 来了解访客如何使用本网站。它会设置 Cookie——仅在您同意后。', ok: '接受', no: '拒绝', more: '了解更多' }
  };
  function lang() { var l = (document.documentElement.lang || 'en').slice(0, 2); return T[l] ? l : 'en'; }
  function get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function set(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function load() {
    if (window.__tpfGa) return; window.__tpfGa = true;
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    window.gtag('js', new Date());
    window.gtag('config', ID);
    var s = document.createElement('script'); s.async = true; s.src = 'https://www.googletagmanager.com/gtag/js?id=' + ID;
    document.head.appendChild(s);
  }
  function forget() {           // a withdrawn consent also removes the cookies Google Analytics already set
    document.cookie.split(';').forEach(function (c) {
      var name = c.split('=')[0].trim();
      if (/^_ga/.test(name)) ['', '; domain=.' + location.hostname.replace(/^www\./, '')].forEach(function (d) {
        document.cookie = name + '=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/' + d; });
    });
  }
  function close() { var b = document.getElementById('tpf-consent'); if (b) b.remove(); }
  function open() {
    close();
    var t = T[lang()], box = document.createElement('div');
    box.id = 'tpf-consent'; box.setAttribute('role', 'dialog'); box.setAttribute('aria-label', 'Cookie');
    box.style.cssText = 'position:fixed;left:16px;right:16px;bottom:16px;z-index:50;max-width:720px;margin:0 auto;padding:18px 20px;' +
      'background:#0B0F14;border:1px solid #2A3844;border-radius:16px;color:#E6EDF2;font:15px/1.5 Manrope,"Noto Sans SC",system-ui,sans-serif;' +
      'box-shadow:0 10px 40px rgba(0,0,0,.5);display:flex;flex-wrap:wrap;gap:12px 16px;align-items:center';
    var p = document.createElement('p'); p.style.cssText = 'margin:0;flex:1 1 320px;color:#B7C3CC';
    p.textContent = t.text + ' ';
    var a = document.createElement('a'); a.href = '/privacy'; a.textContent = t.more; a.style.color = '#5CE1F0'; p.appendChild(a);
    function button(label, value) {
      var b = document.createElement('button'); b.type = 'button'; b.textContent = label;
      b.style.cssText = 'height:42px;padding:0 20px;border-radius:999px;font-weight:600;font-size:15px;font-family:inherit;cursor:pointer;border:1px solid #5CE1F0;' +
        (value === 'granted' ? 'background:#5CE1F0;color:#04161A' : 'background:transparent;color:#E6EDF2');
      b.onclick = function () {
        var before = get(); set(value); close();
        if (value === 'granted') load(); else if (before === 'granted') { forget(); location.reload(); }
      };
      return b;
    }
    var row = document.createElement('div'); row.style.cssText = 'display:flex;gap:10px';
    row.append(button(t.no, 'denied'), button(t.ok, 'granted'));     // refusing is as easy as accepting
    box.append(p, row); document.body.appendChild(box);
  }
  window.tpfConsent = { open: open };
  if (navigator.webdriver) return;   // scripts/prerender_site.py: the saved pages must not carry the banner
  var choice = get();
  if (choice === 'granted') load();
  else if (choice !== 'denied') { if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', open); else open(); }
})();
