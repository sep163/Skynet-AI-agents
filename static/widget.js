/* SkyNet AI — встраиваемый виджет чата для сайта.
   Подключение одной строкой:
   <script src="https://AI-HOST/widget.js" data-mode="auto" async></script>
   data-mode: auto | support | onboarding | b2b; data-client — id клиента, если он вошёл в личный кабинет.
   API: SkyNetAI.open(mode), SkyNetAI.login(clientId), SkyNetAI.logout() */
(() => {
  const me = document.currentScript, API = me.dataset.api || new URL(me.src).origin;
  const GREEN = '#6fb316';
  const MODES = {onboarding: {title: 'Подключение', hello: 'Здравствуйте! Подскажу, есть ли SkyNet в вашем доме, подберу тариф и запишу на монтаж. Напишите адрес — улицу и дом.'},
    support: {title: 'Поддержка', hello: 'Здравствуйте! Я вижу состояние вашей линии и сети. Расскажите, что случилось — можно голосом.'},
    b2b: {title: 'Для бизнеса', hello: 'Здравствуйте! Расскажите о вашем бизнесе: чем занимаетесь, сколько точек и что сейчас не устраивает в связи. Я соберу всё и передам вашему персональному менеджеру — он подготовит решение.'}};
  const autoMode = () => /biznes|business/.test(location.pathname + location.search) ? 'b2b' : state.client ? 'support' : 'onboarding';
  const sid = sessionStorage.skySid || (sessionStorage.skySid = Math.random().toString(36).slice(2));
  const state = {client: me.dataset.client || sessionStorage.skyClient || null, mode: null, hist: JSON.parse(sessionStorage.skyHist || '{}'), voice: null, rec: null};
  state.mode = me.dataset.mode && me.dataset.mode !== 'auto' ? me.dataset.mode : autoMode();

  const host = document.createElement('div'); host.id = 'skynet-ai'; document.body.appendChild(host);
  const root = host.attachShadow({mode: 'open'});
  root.innerHTML = `<style>
  :host{all:initial}*{box-sizing:border-box;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif}
  .fab{position:fixed;right:24px;bottom:24px;width:62px;height:62px;border-radius:50%;background:${GREEN};color:#fff;border:0;box-shadow:0 6px 20px rgba(0,0,0,.25);cursor:pointer;font-size:28px;z-index:2147483000}
  .panel{position:fixed;right:24px;bottom:98px;width:390px;max-width:calc(100vw - 32px);height:600px;max-height:calc(100vh - 120px);background:#fff;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.25);display:none;flex-direction:column;overflow:hidden;z-index:2147483000;color:#1d1d1f}
  .panel.open{display:flex}.hd{background:${GREEN};color:#fff;padding:12px 14px}.hd b{font-size:16px}.hd small{display:block;opacity:.9}
  .tabs{display:flex;gap:4px;margin-top:8px}.tabs button{flex:1;border:0;border-radius:8px;padding:6px;background:rgba(255,255,255,.2);color:#fff;cursor:pointer;font-size:13px}.tabs button.on{background:#fff;color:${GREEN};font-weight:600}
  .msgs{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f6f7f5}
  .m{max-width:88%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;font-size:14px;line-height:1.4}.u{align-self:flex-end;background:${GREEN};color:#fff;border-bottom-right-radius:4px}.a{align-self:flex-start;background:#fff;border:1px solid #e5e7e2;border-bottom-left-radius:4px}
  .card{align-self:stretch;background:#fff;border:1px solid #dfe5d6;border-left:4px solid ${GREEN};border-radius:10px;padding:9px 11px;font-size:13px}.card h4{margin:0 0 5px;font-size:13px}
  .card table{width:100%;border-collapse:collapse}.card td{padding:2px 0}.card td:last-child{text-align:right;white-space:nowrap}.tot td{font-weight:700;border-top:1px solid #e5e7e2;padding-top:4px}
  .next{border-left-color:#2f7de1;background:#f3f8ff}.warn{border-left-color:#e6a23a;background:#fff9ef}.chips{display:flex;flex-wrap:wrap;gap:6px}.chip{border:1px solid ${GREEN};color:${GREEN};background:#fff;border-radius:14px;padding:5px 10px;cursor:pointer;font-size:13px}
  .ft{display:flex;gap:6px;padding:10px;border-top:1px solid #eee;background:#fff}.ft textarea{flex:1;resize:none;height:42px;border:1px solid #ddd;border-radius:10px;padding:9px;font-size:14px}
  .ft button{border:0;border-radius:10px;padding:0 12px;cursor:pointer;background:#eef3e6;font-size:18px}.ft .send{background:${GREEN};color:#fff}.rec{background:#e54848!important;color:#fff}
  .login{padding:10px 12px;font-size:13px;background:#fffbe9;border-bottom:1px solid #f0e6c0}.login select,.login button{font-size:13px;padding:4px 6px;border-radius:6px;border:1px solid #ccc;background:#fff}
  .typing{align-self:flex-start;color:#888;font-size:13px}.muted{color:#777;font-size:12px}</style>
  <button class="fab" title="Чат SkyNet">💬</button>
  <div class="panel"><div class="hd"><b>SkyNet</b><small>ИИ-консультант · отвечает сразу, 24/7</small><div class="tabs"></div></div>
  <div class="login" hidden></div><div class="msgs"></div>
  <div class="ft"><textarea placeholder="Напишите сообщение…"></textarea><button class="mic" title="Надиктовать">🎙</button><button class="send" title="Отправить">➤</button></div></div>`;
  const $ = s => root.querySelector(s), panel = $('.panel'), msgs = $('.msgs'), ta = $('textarea');
  const esc = s => String(s ?? '').replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
  const rub = n => `${Number(n).toLocaleString('ru-RU')} ₽`, tm = s => s ? new Date(s).toLocaleString('ru-RU', {day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit'}) : '';
  const save = () => { sessionStorage.skyHist = JSON.stringify(state.hist); state.client ? sessionStorage.skyClient = state.client : sessionStorage.removeItem('skyClient') };

  function add(cls, html) { const d = document.createElement('div'); d.className = cls; d.innerHTML = html; msgs.appendChild(d); msgs.scrollTop = 1e9; return d }
  function cards(r) {
    for (const t of r.trace || []) {
      const x = t.result || {};
      if (t.tool === 'check_address' && x.status) add('card ' + (x.status === 'connected' ? '' : 'warn'), `<h4>📍 ${esc(x.address || 'Адрес')}</h4>${{connected: `Дом подключён · ${esc(x.tech)} · монтаж через ${x.install_days} дн.`, construction: `Дом подключается, готовность ${esc(x.ready)}`, private: esc(x.tech), none: 'Пока не подключён — можно встать в лист ожидания', unknown: esc(x.message)}[x.status]}`);
      if (t.tool === 'calc_first_payment' && x.monthly) add('card', `<h4>💳 Расчёт</h4><table>${x.monthly.map(i => `<tr><td>${esc(i.name)}</td><td>${rub(i.rub)}/мес</td></tr>`).join('')}${x.one_time.map(i => `<tr><td>${esc(i.name)}</td><td>${i.rub ? rub(i.rub) : 'бесплатно'}</td></tr>`).join('')}<tr class="tot"><td>Каждый месяц</td><td>${rub(x.monthly_total_rub)}</td></tr>${x.promo ? `<tr><td>Первый месяц (${esc(x.promo)})</td><td>${rub(x.first_month_total_rub)}</td></tr>` : ''}</table>`);
      if ((t.tool === 'get_install_slots' || t.tool === 'get_visit_slots') && Array.isArray(x) && x.length) add('card', `<h4>🗓 Выберите время</h4><div class="chips">${x.map(s => `<button class="chip" data-say="Подходит ${tm(s.start)}">${tm(s.start)}${s.tech ? ' · ' + esc(s.tech) : ''}</button>`).join('')}</div>`);
      if (t.tool === 'book_installation' && x.id) add('card', `<h4>✅ Заявка ${esc(x.id)}</h4>Монтаж: ${tm(x.slot.start)}<br>${esc(x.address)}<br><span class="muted">${esc(x.next)}</span>`);
      if (t.tool === 'add_to_waitlist' && x.id) add('card', `<h4>📝 Лист ожидания ${esc(x.id)}</h4><span class="muted">${esc(x.next)}</span>`);
      if (t.tool === 'book_technician_visit' && x.job) add('card', `<h4>🛠 Выезд мастера</h4>${tm(x.job.start)} · ${esc(x.job.tech)}<br><span class="muted">${x.job.status === 'confirmed' ? 'подтверждён' : 'предварительно, диспетчер подтвердит'}</span>`);
      if (t.tool === 'get_network_incidents' && x.incidents?.some(i => i.status === 'open')) add('card warn', `<h4>📡 Мы уже видим проблему</h4>${x.incidents.filter(i => i.status === 'open').map(i => esc(i.title)).join('<br>')}`);
      if (t.tool === 'promise_payment' && x.status === 'done') add('card', `<h4>✅ Обещанный платёж включён</h4>до ${tm(x.details?.until)}`);
    }
    if (r.lead) add('card', `<h4>📨 Запрос ${esc(r.lead.id)} передан менеджеру</h4>${esc(r.lead.manager)} свяжется с вами до ${tm(r.lead.callback_by)}`);
  }
  function render() {
    root.querySelector('.tabs').innerHTML = Object.entries(MODES).map(([k, v]) => `<button data-mode="${k}" class="${k === state.mode ? 'on' : ''}">${v.title}</button>`).join('');
    const lg = $('.login'); lg.hidden = state.mode !== 'support';
    lg.innerHTML = state.client ? `Вы вошли как клиент <b>${esc(state.client)}</b> · <button data-logout>выйти</button>` : `Чтобы я видел вашу линию, войдите в личный кабинет. <span class="muted">(демо: выберите клиента)</span><br><select></select> <button data-login>Войти</button>`;
    if (!state.client && state.mode === 'support') fetch(API + '/api/clients').then(r => r.json()).then(cs => lg.querySelector('select').innerHTML = cs.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join(''));
    msgs.innerHTML = ''; add('m a', esc(MODES[state.mode].hello));
    for (const m of state.hist[state.mode] || []) add('m ' + (m.role === 'user' ? 'u' : 'a'), esc(m.content));
  }
  async function send(text) {
    text = (text ?? ta.value).trim(); if (!text) return;
    if (state.mode === 'support' && !state.client) { add('m a', 'Сначала войдите в личный кабинет — так я увижу вашу линию и баланс.'); return }
    ta.value = ''; const h = state.hist[state.mode] = state.hist[state.mode] || []; h.push({role: 'user', content: text}); add('m u', esc(text));
    const typing = add('typing', 'SkyNet печатает…');
    try {
      const r = await fetch(API + '/api/widget/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: state.mode, messages: h, client_id: state.client, voice: state.voice, sid})}).then(r => r.ok ? r.json() : r.json().catch(() => ({})).then(e => Promise.reject(new Error(typeof e.detail === 'string' ? e.detail : 'сервис временно недоступен'))));
      typing.remove(); state.voice = null; h.push({role: 'assistant', content: r.answer}); save();
      add('m a', esc(r.body || r.answer)); cards(r); if (r.next) add('card next', `<h4>➡️ Что дальше</h4>${esc(r.next)}`);
    } catch (e) { typing.textContent = 'Не получилось ответить: ' + e.message + '. Позвоните 8 (812) 386-20-20.' }
  }
  async function mic(btn) {
    if (state.rec) return state.rec.stop();
    try {
      const st = await navigator.mediaDevices.getUserMedia({audio: true}), rec = new MediaRecorder(st), ch = [];
      rec.ondataavailable = e => ch.push(e.data);
      rec.onstop = async () => { st.getTracks().forEach(t => t.stop()); btn.classList.remove('rec'); btn.textContent = '⏳'; state.rec = null;
        const fd = new FormData(); fd.append('audio', new Blob(ch, {type: rec.mimeType}), 'voice.' + (rec.mimeType.includes('mp4') ? 'mp4' : 'webm'));
        try { const r = await fetch(API + '/api/transcribe', {method: 'POST', body: fd}).then(r => r.json()); ta.value = (ta.value + ' ' + (r.text || '')).trim(); state.voice = r } catch (e) {}
        btn.textContent = '🎙' };
      rec.start(); state.rec = rec; btn.classList.add('rec'); btn.textContent = '⏹';
    } catch (e) { alert('Микрофон недоступен') }
  }
  root.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.classList.contains('fab')) { panel.classList.toggle('open'); if (panel.classList.contains('open')) { render(); ta.focus() } }
    else if (b.dataset.mode) { state.mode = b.dataset.mode; render() }
    else if (b.dataset.say) send(b.dataset.say)
    else if (b.hasAttribute('data-login')) { state.client = root.querySelector('.login select').value; state.hist.support = []; save(); render() }
    else if (b.hasAttribute('data-logout')) { state.client = null; state.hist.support = []; save(); render() }
    else if (b.classList.contains('send')) send()
    else if (b.classList.contains('mic')) mic(b)
  });
  ta.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } });
  window.SkyNetAI = {open(mode) { if (mode) state.mode = mode; panel.classList.add('open'); render() },
    login(id) { state.client = id; state.hist.support = []; save(); render() }, logout() { state.client = null; save(); render() }};
})();
