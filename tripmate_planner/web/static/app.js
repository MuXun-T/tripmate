// TripMate workbench: renders /api/state and talks to /api/turn. No framework, no build step.
// Money is summed in whole fen (cents), so the day totals, the breakdown and the headline total always agree.
(() => {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const nf2 = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf0 = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });
  const fen = (v) => Math.round((Number(v) || 0) * 100);
  const yuan = (f) => nf2.format(f / 100);
  const secs = (ms) => (ms / 1000).toFixed(1);
  const DASH = '\u2014';  // a missing value (app-screens 11), never punctuation
  // memory timestamps are stored in UTC; show them in the viewer's local time
  const when = (iso) => { if (!iso) return DASH; const d = new Date(iso); return Number.isNaN(+d) ? DASH : `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };  // taste-skill-ignore: a clock
  const KIND = { train: '火车', airplane: '飞机', attraction: '景点', breakfast: '早餐', lunch: '午餐', dinner: '晚餐', accommodation: '入住' };
  const MODE = { walk: '步行', metro: '地铁', taxi: '打车' };
  const MEALS = ['breakfast', 'lunch', 'dinner'];
  // Official ChinaTravel meal windows (Is_restaurants_correct): a meal must overlap its window.
  const MEAL_WINDOWS = [['早餐时段', 360, 540], ['午餐时段', 660, 840], ['晚餐时段', 1020, 1200]];
  const EXAMPLES = [
    '当前位置上海。我们两个人想去杭州玩两天，预算 3000 元，想吃杭帮菜。',
    '当前位置广州。我一个人想去深圳玩一天，预算 1000 元。',
    '当前位置北京。三个人去成都玩三天，坐高铁往返，想去看大熊猫，市内打车。',
  ];
  const CN_NUM = ['零', '一', '两', '三', '四', '五', '六', '七'];
  // follow-ups built from the trip on screen, so a suggestion never repeats what is already true
  function followUps(r) {
    const out = [];
    if (r.days && r.days < 7) out.push(`改成${CN_NUM[r.days + 1]}天`);
    const mode = (r.spec.find((f) => f.key === 'innercity_mode') || {}).value;
    out.push(mode === '地铁' ? '市内交通改成打车' : '市内交通只坐地铁');
    if (r.budget) out.push(`预算降到 ${nf0.format(Math.max(100, Math.round((r.budget * 0.7) / 100) * 100))} 元`);
    else out.push('总预算控制在 2000 元');
    return out;
  }

  // The timeline's marks; the legend is drawn from the same map, so a swatch is always the mark it explains.
  const MARKS = {
    intercity: { label: '城际交通', fill: 'var(--chart-ink)', stroke: 'none', ink: '#ffffff' },
    attraction: { label: '景点', fill: 'var(--accent)', stroke: 'none', ink: '#ffffff' },
    meal: { label: '用餐', fill: 'var(--surface)', stroke: 'var(--accent)', ink: 'var(--text)' },
    stay: { label: '住宿', fill: 'var(--active)', stroke: 'none', ink: 'var(--text)' },
  };
  const markOf = (a) => (a.type === 'train' || a.type === 'airplane' ? 'intercity' : MEALS.includes(a.type) ? 'meal' : a.type === 'accommodation' ? 'stay' : 'attraction');

  const state = { engine: null, conversations: [], conversation: null, preferences: [], history: [], view: 'trip', cid: null, day: 0, busy: false, error: null, pending: '', started: 0 };
  const view = $('#view');
  const announce = (t) => { $('#announce').textContent = t; };

  // ---------- data helpers ----------
  function minutes(t) {
    if (t == null || t === '') return null;
    let s = String(t), add = 0;
    if (s.startsWith('次日')) { add = 1440; s = s.slice(2); }
    const [h, m] = s.split(':').map(Number);
    return h * 60 + (m || 0) + add;
  }
  // 24:00 stays 24:00 (the official end of a day); later times belong to the next day
  const clock = (m) => {
    const next = m > 1440, t = next ? m - 1440 : m;
    return `${next ? '次日' : ''}${String(Math.floor(t / 60)).padStart(2, '0')}:${String(t % 60).padStart(2, '0')}`;  // taste-skill-ignore: a clock, not a counter
  };
  const span = (a) => { const s = minutes(a.start_time); let e = minutes(a.end_time); if (e < s) e += 1440; return [s, e]; };
  const legFen = (a) => (a.transports || []).reduce((s, t) => s + fen(t.cost), 0);
  function costs(plan) {
    const c = { intercity: 0, hotel: 0, meals: 0, attractions: 0, innercity: 0 };
    const days = plan.itinerary.map((d) => {
      let day = 0;
      for (const a of d.activities) {
        const v = fen(a.cost);
        if (a.type === 'train' || a.type === 'airplane') c.intercity += v;
        else if (a.type === 'accommodation') c.hotel += v;
        else if (MEALS.includes(a.type)) c.meals += v;
        else c.attractions += v;
        c.innercity += legFen(a);
        day += v + legFen(a);
      }
      return day;
    });
    c.total = c.intercity + c.hotel + c.meals + c.attractions + c.innercity;
    console.assert(days.reduce((s, v) => s + v, 0) === c.total, 'day totals must add up to the trip total');
    return { ...c, days };
  }
  function verdict(r) {
    const from = r.start_city ? `从${r.start_city}出发` : '';
    const who = r.people ? `${from ? '，' : ''}${r.people} 人` : '';
    const lead = from || who ? `${from}${who}。` : '';
    if (!r.plan) return { text: `${lead}没有排出可行的行程。试试放宽预算、换一种交通方式，或少列几个必去的地方。`, bad: true };
    const cs = r.commonsense.filter((c) => c.passed === false), req = r.constraints.filter((c) => !c.passed);
    const total = `总费用 ${yuan(costs(r.plan).total)} 元`;
    if (!cs.length && !req.length) return { text: `${lead}8 组常识检查和 ${r.constraints.length} 项要求全部通过，${total}。`, bad: false };
    const names = [...req.map((c) => c.label), ...cs.map((c) => c.label.split('：')[0])];
    return { text: `${lead}给出了最接近的行程，${total}；还有 ${names.length} 项没满足：${names.join('、')}。`, bad: true };
  }
  const lastResult = () => { const t = (state.conversation?.turns || []).filter((x) => x.role === 'assistant'); return t.length ? t[t.length - 1] : null; };

  // ---------- api ----------
  async function api(path, body) {
    const res = await fetch(path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    const data = await res.json().catch(() => ({ error: `服务返回了 ${res.status}` }));
    if (!res.ok) throw new Error(data.error || `服务返回了 ${res.status}`);
    return data;
  }
  async function load() {
    const q = state.cid ? `?conversation=${encodeURIComponent(state.cid)}` : '';
    try {
      const s = await api(`/api/state${q}`);
      Object.assign(state, { engine: s.engine, conversations: s.conversations, conversation: s.conversation, preferences: s.preferences, history: s.history });
      if (state.cid && !s.conversation) { location.hash = '#/new'; return; }
      state.error = null;
    } catch (e) { state.error = `读取数据失败（${e.message}）。确认终端里的服务还在运行，然后刷新页面。`; }
    render();
  }
  async function send(text) {
    text = text.trim();
    if (!text || state.busy) return;
    state.busy = true; state.error = null; state.pending = text; state.started = performance.now();
    render();
    const tick = setInterval(() => { const el = $('#elapsed'); if (el) el.textContent = Math.floor((performance.now() - state.started) / 1000); }, 1000);
    try {
      const { conversation } = await api('/api/turn', { conversation_id: state.cid, text });
      state.conversation = conversation; state.cid = conversation.id; state.day = 0;
      const msg = $('#msg'); if (msg) msg.value = '';
      history.replaceState(null, '', `#/trip/${conversation.id}`);
      const r = lastResult().result;
      announce(`${r.title}：${verdict(r).text}`);
      const s = await api(`/api/state?conversation=${conversation.id}`);
      state.conversations = s.conversations; state.preferences = s.preferences; state.history = s.history;
    } catch (e) {
      state.error = `没有拿到结果（${e.message}）。确认终端里的服务还在运行，然后重试。`;
      announce(state.error);
    } finally {
      clearInterval(tick); state.busy = false; state.pending = state.error ? text : '';
      render();
      if (state.error) $('#msg')?.focus();
    }
  }
  async function decide(turn, index, accept) {
    try {
      const out = await api('/api/preferences/decide', { conversation_id: state.cid, turn, index, accept });
      state.conversation = out.conversation; state.preferences = out.preferences; state.history = out.history;
      const c = out.conversation.turns[turn].result.candidates[index];
      announce(accept ? `已记住：${c.label} ${c.value_text}` : `不会记住：${c.label} ${c.value_text}`);
    } catch (e) { state.error = `保存偏好失败（${e.message}）。`; }
    render();
  }
  async function forget(key, label) {
    if (!window.confirm(`忘记“${label}”？之后的行程不再默认使用它。`)) return;
    try { const out = await api('/api/preferences/forget', { key }); state.preferences = out.preferences; state.history = out.history; announce(`已忘记：${label}`); }
    catch (e) { state.error = `删除偏好失败（${e.message}）。`; }
    render();
  }

  // ---------- sidebar ----------
  function renderSidebar() {
    document.querySelectorAll('[data-nav]').forEach((a) => {
      if (a.dataset.nav === state.view && !(state.view === 'trip' && state.cid)) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    const statusText = { passed: '全部通过', best_effort: '有未满足的要求', no_plan: '没有排出行程' };
    $('#trips').innerHTML = state.conversations.length ? state.conversations.map((c) => `
      <a class="trip-link" href="#/trip/${esc(c.id)}"${c.id === state.cid && state.view === 'trip' ? ' aria-current="page"' : ''}>
        <span class="t">${esc(c.title)}</span><span class="s">${esc(statusText[c.status] || DASH)}，${esc(c.updated_at.slice(5, 16).replace('T', ' '))}</span>
      </a>`).join('') : '<p class="none">还没有行程。</p>';
    const e = state.engine;
    $('#engine').innerHTML = e ? `<span><b>${e.mode === 'llm' ? esc(e.model) : '规则模式'}</b>${e.mode === 'llm' ? '，thinking 关闭' : '，没有调用 LLM'}</span><span>冻结配置 <span class="id">${esc(e.config_hash)}</span></span>` : '';
  }

  // ---------- timeline ----------
  const measureCtx = document.createElement('canvas').getContext('2d');
  const textWidth = (t) => { measureCtx.font = `12px ${getComputedStyle(document.body).fontFamily}`; return measureCtx.measureText(t).width; };
  function fit(t, w) { if (textWidth(t) <= w) return t; let s = t; while (s.length > 1 && textWidth(s + '…') > w) s = s.slice(0, -1); return s.length > 1 ? s + '…' : ''; }
  function nameOf(a) { return a.type === 'train' || a.type === 'airplane' ? (a.TrainID || a.FlightID || '') : a.position || ''; }
  function timeline(el, acts, dayNo) {
    const W = Math.max(280, el.clientWidth), H = 76, laneY = 24, barH = 24;
    const spans = acts.map(span);
    const allLegs = acts.flatMap((a) => (a.transports || []).map((t) => [minutes(t.start_time), minutes(t.end_time)]));
    const lo = Math.min(360, ...spans.map((s) => s[0]), ...allLegs.map((l) => l[0]));
    const hi = Math.max(1320, ...spans.map((s) => s[1]));
    const m0 = Math.floor(lo / 60) * 60, m1 = Math.ceil(hi / 60) * 60;
    const x = (m) => ((m - m0) / (m1 - m0)) * W;
    const step = [60, 120, 180, 240, 360].find((s) => (s / (m1 - m0)) * W >= 56) || 360;
    const NS = 'http://www.w3.org/2000/svg';
    const parts = [];
    for (let m = Math.ceil(m0 / step) * step; m <= m1; m += step) {
      parts.push(`<line x1="${x(m)}" x2="${x(m)}" y1="${laneY - 4}" y2="${laneY + barH + 4}" stroke="var(--grid)" stroke-width="1"/>`);
      const anchor = m === m0 ? 'start' : m === m1 ? 'end' : 'middle';
      parts.push(`<text x="${x(m)}" y="${H - 4}" text-anchor="${anchor}" font-size="12" fill="var(--muted)">${clock(m)}</text>`);
    }
    for (const [label, a, b] of MEAL_WINDOWS) {
      if (b <= m0 || a >= m1) continue;
      const xa = x(Math.max(a, m0)), xb = x(Math.min(b, m1));
      parts.push(`<rect x="${xa}" y="${laneY - 4}" width="${xb - xa}" height="${barH + 8}" fill="var(--chart-ink)" opacity="var(--chart-span)"/>`);
      if (textWidth(label) + 4 <= xb - xa) parts.push(`<text x="${xa + 2}" y="${laneY - 10}" font-size="12" fill="var(--muted)">${label}</text>`);
    }
    acts.forEach((a) => {
      const legs = a.transports || [];
      if (!legs.length) return;
      const s = minutes(legs[0].start_time), e = minutes(legs[legs.length - 1].end_time);
      parts.push(`<line x1="${x(s)}" x2="${Math.max(x(e), x(s) + 2)}" y1="${laneY + barH / 2}" y2="${laneY + barH / 2}" stroke="var(--chart-grey)" stroke-width="2"><title>${esc(legs.map((t) => MODE[t.mode] || t.mode).join('、'))} ${clock(s)}–${clock(e)}</title></line>`);
    });
    acts.forEach((a, i) => {
      const [s, e] = spans[i], k = MARKS[markOf(a)];
      const xa = x(s), w = Math.max(3, x(e) - xa);
      const label = fit(`${a.type === 'accommodation' ? '入住 ' : ''}${nameOf(a)}`, w - 12);
      parts.push(`<g><title>${esc(KIND[a.type] || a.type)} ${esc(nameOf(a))} ${clock(s)}–${clock(e)}</title>`
        + `<rect x="${xa + (k.stroke === 'none' ? 0 : 0.75)}" y="${laneY + (k.stroke === 'none' ? 0 : 0.75)}" width="${w - (k.stroke === 'none' ? 1 : 1.5)}" height="${barH - (k.stroke === 'none' ? 0 : 1.5)}" rx="4" fill="${k.fill}" stroke="${k.stroke}" stroke-width="1.5"/>`
        + (label ? `<text x="${xa + 6}" y="${laneY + 16}" font-size="12" fill="${k.ink}">${esc(label)}</text>` : '') + '</g>');
    });
    const first = acts[0], last = acts[acts.length - 1];
    const summary = `第 ${dayNo} 天时间轴：${clock(spans[0][0])} 开始，${clock(spans[spans.length - 1][1])} 结束，共 ${acts.length} 项安排，第一项是${KIND[first.type] || ''}${nameOf(first)}，最后一项是${KIND[last.type] || ''}${nameOf(last)}。`;
    el.innerHTML = `<svg xmlns="${NS}" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(summary)}">${parts.join('')}</svg>`;
  }
  function legend() {
    const sw = (k) => `<svg viewBox="0 0 16 10" aria-hidden="true"><rect x="0.75" y="0.75" width="14.5" height="8.5" rx="2" fill="${k.fill}" stroke="${k.stroke}" stroke-width="1.5"/></svg>`;
    return `<div class="legend">${Object.values(MARKS).map((k) => `<span>${sw(k)}${k.label}</span>`).join('')}`
      + '<span><svg viewBox="0 0 16 10" aria-hidden="true"><line x1="0" x2="16" y1="5" y2="5" stroke="var(--chart-grey)" stroke-width="2"/></svg>市内交通</span>'
      + '<span><svg viewBox="0 0 16 10" aria-hidden="true"><rect width="16" height="10" fill="var(--chart-ink)" opacity="0.12"/></svg>官方用餐时段</span></div>';
  }

  // ---------- itinerary ----------
  function howTo(a) {
    const legs = a.transports || [];
    if (!legs.length) return `<span class="meta">${DASH}</span>`;
    const modes = [...new Set(legs.map((t) => MODE[t.mode] || t.mode))];
    const s = minutes(legs[0].start_time), e = minutes(legs[legs.length - 1].end_time), c = legFen(a);
    return `${esc(modes.join('、'))}，${e - s} 分钟<span class="sub">${clock(s)} 出发${c ? `，${yuan(c)} 元` : ''}</span>`;
  }
  function whatOf(a) {
    if (a.type === 'train' || a.type === 'airplane') {
      return `<span class="kind">${KIND[a.type]}</span><span class="name">${esc(a.start)} 到 ${esc(a.end)}</span><span class="sub"><span class="id">${esc(a.TrainID || a.FlightID || '')}</span>，${a.tickets} 张票，每张 ${yuan(fen(a.price))} 元</span>`;
    }
    let sub = '';
    if (a.type === 'accommodation') sub = `${a.rooms} 间，${Number(a.room_type) === 1 ? '一张床' : '两张床'}，每间 ${yuan(fen(a.price))} 元`;
    else if (MEALS.includes(a.type)) sub = fen(a.price) ? `人均 ${yuan(fen(a.price))} 元` : '酒店早餐，不另收费';
    else sub = fen(a.price) ? `门票每张 ${yuan(fen(a.price))} 元` : '免费';
    return `<span class="kind">${KIND[a.type] || esc(a.type)}</span><span class="name">${esc(a.position)}</span><span class="sub">${sub}</span>`;
  }
  function itineraryPanel(r) {
    if (state.busy && !r) return `<section class="panel" aria-labelledby="h-trip"><header><h2 id="h-trip">逐日行程</h2></header>
      <span class="skeleton" style="height:76px;margin-bottom:12px"></span>${'<span class="skeleton" style="height:20px;margin:12px 0"></span>'.repeat(5)}</section>`;
    if (!r || !r.plan) {
      const msg = r ? '这一轮没有排出可行的行程，校验面板里列出了原因。' : '行程会出现在这里。可以先在右侧选一条示例需求。';
      return `<section class="panel" aria-labelledby="h-trip"><header><h2 id="h-trip">逐日行程</h2></header><p class="empty">${msg}</p></section>`;
    }
    const plan = r.plan, c = costs(plan), d = Math.min(state.day, plan.itinerary.length - 1), day = plan.itinerary[d];
    const tabs = `<div class="days" role="tablist" aria-label="选择第几天">${plan.itinerary.map((x, i) => `<button type="button" role="tab" aria-selected="${i === d}" aria-controls="day-panel" data-day="${i}">第 ${x.day} 天</button>`).join('')}</div>`;
    const rows = day.activities.map((a) => { const [s, e] = span(a); return `<tr><td class="time"><time>${clock(s)}–${clock(e)}</time></td><td class="what">${whatOf(a)}</td><td class="how">${howTo(a)}</td><td class="n">${yuan(fen(a.cost))}</td></tr>`; }).join('');
    const parts = [['城际交通', c.intercity], ['住宿', c.hotel], ['餐饮', c.meals], ['景点门票', c.attractions], ['市内交通', c.innercity]];
    return `<section class="panel" aria-labelledby="h-trip">
      <header><h2 id="h-trip">逐日行程</h2>${tabs}</header>
      <div id="day-panel" role="tabpanel">
        <div class="day-head"><h3>第 ${day.day} 天，${day.activities.length} 项安排</h3>${legend()}</div>
        <div class="timeline" id="timeline" data-day="${d}"></div>
        <div class="table-wrap"><table class="acts">
          <thead><tr><th>时间</th><th>安排</th><th>怎么过去</th><th class="n">费用（元）</th></tr></thead>
          <tbody>${rows}</tbody>
          <tfoot><tr><td class="time"></td><td class="what">当日合计（含市内交通）</td><td class="how"></td><td class="n">${yuan(c.days[d])}</td></tr></tfoot>
        </table></div>
      </div>
      <dl class="breakdown" aria-label="全程费用构成">${parts.map(([k, v]) => `<div><dt>${k}</dt><dd class="num">${yuan(v)} 元</dd></div>`).join('')}<div class="sum"><dt>全程合计</dt><dd class="num">${yuan(c.total)} 元</dd></div></dl>
    </section>`;
  }

  // ---------- KPIs, checks, spec ----------
  function kpis(r) {
    const none = (label, note) => `<div class="kpi"><p class="kpi-label">${label}</p><p class="kpi-value none">${DASH}</p><p class="kpi-note">${note}</p></div>`;
    if (!r || !r.plan) {
      return `<section class="kpis" aria-label="费用与校验概览">${none('总费用', '生成行程后计算')}${none('预算剩余', r && r.budget ? `预算 ${yuan(fen(r.budget))} 元` : '还没有预算')}${r ? `<div class="kpi"><p class="kpi-label">常识检查</p><p class="kpi-value none">${DASH}</p><p class="kpi-note">没有行程可检查</p></div>` : none('常识检查', '官方 8 组检查函数')}${none('你的要求', '说出需求后编译')}</section>`;
    }
    const c = costs(r.plan), passed = r.commonsense.filter((x) => x.passed).length, req = r.constraints, ok = req.filter((x) => x.passed).length;
    let budget = none('预算剩余', '没有设预算');
    if (r.budget) {
      const b = fen(r.budget), left = b - c.total, over = left < 0;
      budget = `<div class="kpi"><p class="kpi-label">${over ? '超出预算' : '预算剩余'}</p><p class="kpi-value num">${yuan(Math.abs(left))}<small>元</small></p>
        <div class="track${over ? ' over' : ''}" role="img" aria-label="已用 ${yuan(c.total)} 元，预算 ${yuan(b)} 元"><span style="width:${Math.min(100, (c.total / b) * 100).toFixed(1)}%"></span></div>
        <p class="kpi-note${over ? ' bad' : ''}">预算 ${yuan(b)} 元</p></div>`;
    }
    const failedNames = req.filter((x) => !x.passed).map((x) => x.label);
    return `<section class="kpis" aria-label="费用与校验概览">
      <div class="kpi lead"><p class="kpi-label">总费用</p><p class="kpi-value num">${yuan(c.total)}<small>元</small></p><p class="kpi-note">${r.people || 1} 人 ${r.plan.itinerary.length} 天的全部花费</p></div>
      ${budget}
      <div class="kpi"><p class="kpi-label">常识检查</p><p class="kpi-value num">${passed}<small>/ ${r.commonsense.length}</small></p><p class="kpi-note${passed < r.commonsense.length ? ' bad' : ''}">${passed < r.commonsense.length ? '有检查未通过' : '官方检查函数全部通过'}</p></div>
      <div class="kpi"><p class="kpi-label">你的要求</p><p class="kpi-value num">${ok}<small>/ ${req.length}</small></p><p class="kpi-note${failedNames.length ? ' bad' : ''}">${failedNames.length ? `未满足：${esc(failedNames.join('、'))}` : '全部满足'}</p></div>
    </section>`;
  }
  const pill = (p) => (p === null || p === undefined ? '<span class="pill">待运行</span>' : p ? '<span class="pill pos">通过</span>' : '<span class="pill neg">未通过</span>');
  const GROUPS = ['活动类型与字段完整', '城际交通：车次、时刻与票价一致', '景点：开放时间内、票价正确、不重复', '酒店：价格与房型正确，多日行程有住宿', '餐饮：营业时间与用餐时段，不重复', '市内交通：与路线工具的结果一致', '时间：每项有起止，前后不冲突', '位置：上一站到下一站有交通衔接'];
  function checksPanel(r) {
    const cs = r ? r.commonsense : GROUPS.map((label) => ({ label, passed: null, failed: [] }));
    const left = cs.map((c) => `<div class="row"><span class="what">${esc(c.label)}${c.failed.length ? `<span class="sub">${esc(c.failed.join('；'))}</span>` : ''}</span>${pill(c.passed)}</div>`).join('');
    let right = '<p class="sentence">说出需求后，这里会列出从需求编译出的每一项约束。</p>', repairs = '';
    if (r) {
      right = r.constraints.length ? r.constraints.map((c) => `<div class="row"><span class="what">${esc(c.label)}</span>${pill(c.passed)}</div>`).join('') : '<p class="sentence">这一轮没有编译出约束。</p>';
      const rows = r.repairs.map((x) => `<div class="row"><span class="what">${x.reflect ? '反思后' : ''}第 ${x.round} 轮：${esc(x.actions.join('；') || '没有可用的修复动作')}<span class="sub">修复前 ${x.failures} 项未通过</span></span></div>`).join('');
      repairs = `<div class="block"><h3>修复过程</h3>${rows || `<p class="sentence">${r.plan && !r.repairs.length ? '第一次规划就通过了校验，没有修复。' : '没有修复记录。'}</p>`}${r.reflect_used ? '<p class="sentence">修复用完后，让 LLM 带着失败原因重新理解了一次需求。</p>' : ''}</div>`;
    }
    return `<section class="panel" aria-labelledby="h-check"><header><h2 id="h-check">规则校验</h2><span class="meta">使用 ChinaTravel 官方校验函数</span></header>
      <div class="split"><div><h3>常识检查</h3><div class="rows">${left}</div></div><div><h3>你的要求</h3><div class="rows">${right}</div>${repairs}</div></div></section>`;
  }
  function specPanel(r) {
    if (!r) return `<section class="panel" aria-labelledby="h-spec"><header><h2 id="h-spec">系统理解的需求</h2></header><p class="sentence">TripMate 会先把你的话整理成结构化的需求：出发地、目的地、天数、人数、预算和各项偏好，再据此检索和规划。</p></section>`;
    const kv = r.spec.map((f) => `<div class="kv"><dt>${esc(f.label)}</dt><dd>${esc(f.value)}</dd><span class="src">${esc(f.source_label)}</span></div>`).join('');
    const run = r.run, max = Math.max(...run.nodes.map((n) => n.ms), 1);
    const bars = run.nodes.slice(0, 6).map((n) => `<li><span>${esc(n.label)}</span><span class="num">${secs(n.ms)} 秒</span><span class="bar" style="width:${((n.ms / max) * 100).toFixed(1)}%"></span></li>`).join('');
    const llm = run.llm_calls ? `LLM 调用 ${run.llm_calls} 次，输入 ${nf0.format(run.prompt_tokens)} token，输出 ${nf0.format(run.completion_tokens)} token。` : '这一轮没有调用 LLM，需求由规则解析得到。';
    const fallback = r.issues.some((x) => x.startsWith('llm fallback')) ? '<p class="sentence">LLM 没有返回可用结果，这一轮按规则解析的结果规划。</p>' : '';
    return `<section class="panel" aria-labelledby="h-spec"><header><h2 id="h-spec">系统理解的需求</h2><span class="meta">每一项都标出了来源</span></header>
      <div class="split"><dl>${kv || '<p class="sentence">没有识别出需求字段。</p>'}</dl>
      <div><h3>这一轮做了什么</h3><ol class="bars">${bars}</ol><p class="sentence">${llm}</p>${fallback}</div></div></section>`;
  }

  // ---------- conversation rail ----------
  function thread() {
    const turns = state.conversation?.turns || [];
    const out = [];
    turns.forEach((t, i) => {
      if (t.role === 'user') { out.push(`<div class="msg user"><span class="who">你</span><p class="body">${esc(t.text)}</p></div>`); return; }
      const r = t.result, v = verdict(r);
      out.push(`<div class="msg assistant"><span class="who">TripMate，${secs(r.wall_ms || r.run.total_ms)} 秒</span><p class="body${v.bad ? ' bad' : ''}">${esc(r.title)}：${esc(v.text)}</p></div>`);
      if (r.candidates.length) {
        const items = r.candidates.map((c, j) => `<div class="pref-item"><span>${esc(c.label)}：${esc(c.value_text)}<span class="sub">依据：“${esc(c.evidence)}”</span></span>
          ${c.decision ? `<span class="done">${c.decision === 'accepted' ? '已记住，之后的行程会默认使用。' : '不会记住这条。'}</span>` : `<span class="acts"><button class="btn small" type="button" data-decide="${i},${j},1">记住</button><button class="btn small ghost" type="button" data-decide="${i},${j},0">不记住</button></span>`}</div>`).join('');
        out.push(`<div class="pref-card"><p class="q">要记住这条偏好吗？</p>${items}</div>`);
      }
    });
    if (state.busy) {
      out.push(`<div class="msg user"><span class="who">你</span><p class="body">${esc(state.pending)}</p></div>`);
      out.push('<div class="msg assistant"><span class="who">TripMate</span><p class="body">正在规划：理解需求、检索候选、排行程、规则校验。已用 <span id="elapsed">0</span> 秒，通常需要几秒到二十秒。</p></div>');
    } else if (state.error) {
      out.push(`<div class="msg assistant"><span class="who">TripMate</span><p class="body bad">${esc(state.error)}</p></div>`);
    }
    if (!turns.length && !state.busy) {
      out.push(`<div class="examples"><p class="meta">试试这些需求</p>${EXAMPLES.map((e) => `<button class="example" type="button" data-send="${esc(e)}">${esc(e)}</button>`).join('')}</div>`);
    } else if (!state.busy && lastResult()?.result.plan) {
      out.push(`<div class="suggest" aria-label="继续追问">${followUps(lastResult().result).map((f) => `<button class="chip" type="button" data-send="${esc(f)}">${esc(f)}</button>`).join('')}</div>`);
    }
    return out.join('');
  }
  function rail() {
    const n = (state.conversation?.turns || []).filter((t) => t.role === 'user').length;
    const first = !n;
    return `<aside class="panel rail" aria-labelledby="h-chat"><header><h2 id="h-chat">对话</h2>${n ? `<span class="meta">${n} 轮</span>` : ''}</header>
      <div class="thread" id="thread">${thread()}</div>
      <form class="composer" id="composer">
        <label for="msg">${first ? '你的需求' : '继续修改'}</label>
        <textarea id="msg" name="msg" rows="3" maxlength="800" placeholder="${first ? '例如：当前位置上海。我们两个人想去杭州玩两天，预算 3000 元。' : '例如：改成三天，市内只坐地铁'}"${state.busy ? ' disabled' : ''}>${esc(state.error ? state.pending : '')}</textarea>
        <div class="foot"><span class="hint">Enter 发送，Shift + Enter 换行</span><button class="btn primary" type="submit"${state.busy ? ' disabled' : ''}>${state.busy ? '正在规划…' : first ? '生成行程' : '更新行程'}</button></div>
      </form></aside>`;
  }

  // ---------- views ----------
  function tripView() {
    const last = lastResult(), r = last?.result || null;
    const v = r ? verdict(r) : null;
    const at = last ? last.at.slice(11, 16) : '';
    const sub = state.busy ? '<p class="sub busy">正在规划这一轮需求……</p>' : v ? `<p class="sub">${esc(v.text)}</p>` : '<p class="sub">用一句话说出发地、目的地、天数和人数，之后可以继续追问修改。</p>';
    // the rail starts level with the title, so the composer (the screen's main job) is on the first screen
    return `<div class="layout"><div class="main">
        <div class="title-row"><div><h1>${esc(r ? r.title : state.conversation ? state.conversation.title : '新行程')}</h1>${sub}</div>
        ${r ? `<p class="stamp">规划于 ${esc(at)}，用时 ${secs(r.wall_ms || r.run.total_ms)} 秒</p>` : ''}</div>
        <div class="work" aria-busy="${state.busy}">${kpis(r)}${itineraryPanel(r)}${checksPanel(r)}${specPanel(r)}</div>
      </div>${rail()}</div>`;
  }
  function prefsView() {
    const rows = state.preferences.map((p) => `<tr><td>${esc(p.label)}</td><td>${esc(p.value_text)}</td><td>“${esc(p.evidence)}”</td><td class="n"><time>${when(p.valid_from)}</time></td><td class="n"><button class="btn small ghost danger" type="button" data-forget="${esc(p.key)}" data-label="${esc(`${p.label}：${p.value_text}`)}">忘记</button></td></tr>`).join('');
    const pillOf = (st) => (st === '生效中' ? 'pos' : '');
    const hist = state.history.map((h) => `<tr><td>${esc(h.label)}</td><td>${esc(h.value_text)}</td><td><span class="pill ${pillOf(h.status)}">${esc(h.status)}</span></td><td class="n"><time>${when(h.valid_from)}</time></td><td class="n"><time>${h.valid_to ? when(h.valid_to) : DASH}</time></td></tr>`).join('');
    return `<div class="title-row"><div><h1>长期偏好</h1><p class="sub">确认过的偏好会用在之后的每次规划里；你在对话里明确说的要求优先。</p></div></div>
      <div class="layout prefs"><div class="work">
      <section class="panel" aria-labelledby="h-prefs"><header><h2 id="h-prefs">已记住的偏好</h2><span class="meta">${state.preferences.length} 条生效中</span></header>
      ${rows ? `<div class="table-wrap"><table><thead><tr><th>偏好</th><th>取值</th><th>依据</th><th class="n">记住于</th><th class="n"><span class="visually-hidden">操作</span></th></tr></thead><tbody>${rows}</tbody></table></div>`
        : '<p class="empty">还没有记住的偏好。在对话里说出习惯，比如“我出门一般坐地铁”，确认后会出现在这里。</p>'}</section>
      <section class="panel" aria-labelledby="h-hist"><header><h2 id="h-hist">变更记录</h2><span class="meta">旧值不会被覆盖，只记下失效时间</span></header>
      ${hist ? `<div class="table-wrap"><table><thead><tr><th>偏好</th><th>取值</th><th>状态</th><th class="n">生效</th><th class="n">失效</th></tr></thead><tbody>${hist}</tbody></table></div>`
        : '<p class="empty">还没有变更记录。</p>'}</section></div>
      <aside class="panel" aria-labelledby="h-how"><header><h2 id="h-how">偏好怎么用</h2></header>
        <p class="sentence">每次规划前，按下面的顺序决定每一项的取值，排在前面的优先：</p>
        <ol class="priority"><li>这一轮对话里你明确说的要求</li><li>已记住的长期偏好</li><li>系统默认值</li></ol>
        <p class="sentence">偏好的菜系只用来帮助检索餐厅，不会变成必须满足的约束。新的偏好要经过你确认才会写入；同一项偏好任何时候只有一个值在生效。</p>
        <p class="sentence">点“忘记”会让这条偏好立即失效，删除的时间也会记进变更记录。</p>
      </aside></div>`;
  }
  function drawTimeline() {
    const tl = $('#timeline');
    if (!tl) return;
    const day = lastResult().result.plan.itinerary[+tl.dataset.day];
    timeline(tl, day.activities, day.day);
  }
  function render() {
    const draft = $('#msg')?.value || '';   // a half-typed message survives a re-render
    renderSidebar();
    view.innerHTML = state.view === 'prefs' ? prefsView() : tripView();
    const msg = $('#msg');
    if (msg && draft && !state.busy && !msg.value) msg.value = draft;
    drawTimeline();
    const th = $('#thread'); if (th) th.scrollTop = th.scrollHeight;
  }

  // ---------- events ----------
  view.addEventListener('submit', (e) => { if (e.target.id === 'composer') { e.preventDefault(); send($('#msg').value); } });
  view.addEventListener('keydown', (e) => { if (e.target.id === 'msg' && e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(e.target.value); } });
  view.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    if (b.dataset.send) send(b.dataset.send);
    else if (b.dataset.day) { state.day = +b.dataset.day; render(); $(`[data-day="${state.day}"]`)?.focus(); }
    else if (b.dataset.decide) { const [t, i, a] = b.dataset.decide.split(',').map(Number); decide(t, i, a === 1); }
    else if (b.dataset.forget) forget(b.dataset.forget, b.dataset.label);
  });
  view.addEventListener('keydown', (e) => {
    if (!e.target.matches('.days [role="tab"]') || !['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    const n = lastResult().result.plan.itinerary.length; state.day = (state.day + (e.key === 'ArrowRight' ? 1 : n - 1)) % n; render(); $(`[data-day="${state.day}"]`)?.focus();
  });
  const sidebar = $('#sidebar'), openBtn = $('#menu-open');
  const menu = (open) => { sidebar.classList.toggle('open', open); openBtn.setAttribute('aria-expanded', String(open)); if (open) $('#menu-close').focus(); };
  openBtn.addEventListener('click', () => menu(true));
  $('#menu-close').addEventListener('click', () => { menu(false); openBtn.focus(); });
  sidebar.addEventListener('click', (e) => { if (e.target.closest('a')) menu(false); });
  let resizeTimer; window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawTimeline, 150); });
  function route() {
    const h = location.hash;
    if (h.startsWith('#/prefs')) { state.view = 'prefs'; state.cid = null; }
    else if (h.startsWith('#/trip/')) { state.view = 'trip'; state.cid = h.slice(7); }
    else { state.view = 'trip'; state.cid = null; state.conversation = null; }
    state.day = 0; state.error = null;
    load();
  }
  window.addEventListener('hashchange', route);
  document.fonts?.ready.then(drawTimeline);   // label widths are measured with the loaded face
  route();
})();
