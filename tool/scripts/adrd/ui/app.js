const T = new URLSearchParams(location.search).get('t') || '';
const S = location.pathname.replace(/\/$/, '');
const u = (p, q) => {
  const o = Object.assign({t: T}, q || {});
  return S + p + '?' + Object.keys(o).map(k => k + '=' + encodeURIComponent(o[k])).join('&');
};

const $ = id => document.getElementById(id);
const ht = s => String(s == null ? '' : s).split('').map(c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c] || c)).join('');
const ff = s => {
  s = Math.max(0, s || 0);
  return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(Math.floor(s % 60)).padStart(2, '0');
};
const dur = s => s >= 60 ? Math.floor(s / 60) + 'm ' + String(Math.round(s % 60)).padStart(2, '0') + 's' : Math.round(s) + 's';
const kb = n => n > 1e6 ? (n / 1e6).toFixed(1) + ' MB' : Math.round((n || 0) / 1024) + ' KB';

let boxes = [];
let BOXES = [];
let masked = true;
let ART = {};
let SECTIONS = [];
let SECT_SIG = '__';
let PKS = {size: [0, 0], stills: []};
let PKS_SIG = '';
let pkDrag = null;
let pkMv = null;

async function post(p, x) {
  const r = await fetch(u(p, x), {method: 'POST'});
  const j = await r.json().catch(() => ({}));
  return r.ok ? j : Promise.reject(new Error(j.error || ('http ' + r.status)));
}

function note(m, ok) {
  const e = $('inote');
  if (e) e.innerHTML = m ? `<span class="pill ${ok ? 'ok' : 'bad'}">${ht(m)}</span>` : '';
}

function editing(el) {
  const ae = document.activeElement;
  return !!ae && ['INPUT', 'SELECT', 'TEXTAREA'].includes(ae.tagName) && el.contains(ae);
}

/* ---- live frame + mask overlay ------------------------------------------ */

function draw() {
  const v = $('v');
  v.querySelectorAll('.box').forEach(b => b.remove());
  boxes.forEach(b => {
    const f = b.frac;
    if (!f || f[2] <= 0) return;
    const d = document.createElement('div');
    d.className = 'box' + (masked ? ' maskbox' : '');
    d.style.cssText = `left:${f[0] * 100}%;top:${f[1] * 100}%;width:${f[2] * 100}%;height:${f[3] * 100}%`;
    const tag = document.createElement('i');
    tag.textContent = b.label || '';
    d.appendChild(tag);
    v.appendChild(d);
  });
}

$('mt') && ($('mt').onchange = e => { masked = e.target.checked; draw(); });

/* ---- state --------------------------------------------------------------- */

function stageChips(rs) {
  return (rs || []).map(h => {
    const cls = h.state === 'ok' ? 'ok' : h.state === 'running' ? 'warn'
      : (h.state === 'failed' || h.state === 'interrupted') ? 'bad' : 'idle';
    return `<span class="stage-chip ${cls}"><i></i>${ht(h.name)}${h.seconds ? ' ' + h.seconds + 's' : ''}</span>`;
  }).join('');
}

function paintRail(chapters, step) {
  const cur = step && step.i !== undefined
    ? chapters.findIndex(c => step.chapter && c.module === step.chapter) : -1;
  $('rail').innerHTML = chapters.map((c, i) => {
    const name = c.module || c.title || 'chapter';
    const open = c.opened_with || c.title || '';
    return `<button class="ch ${i === cur ? 'on' : ''}" data-t="${c.t0 || 0}" title="Play from ${ff(c.t0)}">
      <time>${ff(c.t0)}-${ff(c.t1)}</time><b>${ht(name)}</b><span>${ht(open)}</span></button>`;
  }).join('') || '<div class="empty">No timeline yet. A take writes the chapters.</div>';
}

function paintSteps(steps) {
  const miss = steps.filter(e => e.mismatch || e.stuck).length;
  $('steps-sub').textContent = steps.length
    ? `${steps.length} replayed, ${miss} off-screen` : '';
  $('steps').innerHTML = steps.length ? steps.map(e =>
    `<div class="step">
      <time>${ff(e.t)}</time>
      <span class="scr" title="${ht(e.screen)}">${ht(e.screen || '-')}</span>
      <span class="grow">${ht(e.label || '')}</span>
      ${e.mismatch ? '<span class="pill bad">missed screen</span>'
        : e.stuck ? '<span class="pill bad">dialog in front</span>'
        : '<span class="pill ok">ok</span>'}
    </div>`).join('')
    : '<div class="empty">No steps replayed yet. The walk is read from the app source and saved with the take.</div>';
}

function paintMasks(j) {
  const mkEl = $('mask');
  if (editing(mkEl)) return;
  const p = j.proof || {};
  const bad = Object.keys(p).filter(k => k.startsWith('unchanged'));
  const scr = j.screens || {};
  const sn = ['global'].concat(Object.keys(scr));
  const win = b => {
    if (b.scope === 'global') return 'whole film';
    const w = scr[b.scope.replace('screen:', '')] || [];
    return w.length ? w.map(x => `${Math.round(x[0])}-${Math.round(x[1])}s`).join(' ')
      : 'no window - masks nothing';
  };
  $('mask-sub').textContent = `${boxes.length} box${boxes.length === 1 ? '' : 'es'}`;
  mkEl.innerHTML = (boxes.length ? boxes.map(b =>
    `<div class="maskrow">
      <div class="who"><b>${ht(b.label)}</b><span>${ht(b.mode)} · ${ht(win(b))}</span></div>
      <select data-sc="${b.i}" aria-label="Screen for ${ht(b.label)} on click">
        ${sn.map(n => `<option${n === b.scope.replace('screen:', '') ? ' selected' : ''}>${ht(n)}</option>`).join('')}
      </select>
      <button class="icon-btn" data-rm="${b.i}">Remove</button>
    </div>`).join('')
    : '<div class="empty">No mask boxes. Place one on the still beside this panel.</div>')
    + `<div class="proof">${Object.entries(p).map(([k, v]) =>
      `<span class="pill ${k.startsWith('strongly') ? 'ok' : k.startsWith('unchanged') ? 'bad' : ''}">${v} ${ht(k)}</span>`).join('')}</div>`
    + (bad.length ? `<div class="warnbox" style="margin-top:10px">${bad.length} box covers nothing.
        Move it or point it at the right screen, then re-render.</div>` : '')
    + `<div class="warnbox" style="margin-top:10px">This console shows unmasked client content.
        The only file for sharing is the masked master or a section cut.</div>`;
}

function paintWalk(walk) {
  const el = $('walk');
  const sig = walk.map(x => x.i + '|' + x.screen + '|' + x.label).join(';');
  if (el.dataset.sig === sig || editing(el)) return;
  el.dataset.sig = sig;
  el.innerHTML = walk.length ? walk.map(x =>
    `<div class="walk-row">
      <span class="ops">${String(x.i).padStart(2, '0')} ${ht(x.ops || '')}</span>
      <input value="${ht(x.screen)}" data-ws="${x.i}" aria-label="Screen name for step ${x.i}" placeholder="screen">
      <input value="${ht(x.label)}" data-wl="${x.i}" aria-label="Caption for step ${x.i}" placeholder="caption">
      <button class="icon-btn" data-rmw="${x.i}">Drop</button>
    </div>`).join('')
    + `<p class="n" style="margin-top:8px">The screen name ties mask boxes to the seconds of a step.
       The caption is burned into the video. Re-render after editing.</p>`
    : '<div class="empty">No walk yet. Start a run to replay the app, or it is built from the source scan.</div>';
}

function paintArtifacts(arts) {
  $('arts').innerHTML = arts.length ? `<table>${arts.map(a =>
    `<tr><td>${a.masked ? `<a href="${u('/f/' + a.rel, {dl: 1})}">${ht(a.name)}</a>` : ht(a.name)}</td>
      <td class="what">${ht(a.what)}</td><td class="what">${kb(a.bytes)}</td>
      <td>${a.masked ? '' : '<span class="pill bad">unmasked</span>'}</td></tr>`).join('')}</table>`
    : '<div class="empty">Nothing produced yet. The bundle appears here after the run.</div>';
}

/* Chapter titles arrive as "3. signup" (timeline.py builds them that way); the card
   already carries its own ordinal, and sorting on the number would make every order
   look like scene order. */
const plain = t => String(t || '').replace(/^\d+\.\s+/, '');

function paintSections(j) {
  SECTIONS = j.sections || [];
  const total = j.sections_total || 0;
  $('rush-sub').innerHTML = SECTIONS.length
    ? `<b>${SECTIONS.length}</b> cut${SECTIONS.length === 1 ? '' : 's'} · ${dur(total)}`
    : 'not cut yet';
  const sig = SECTIONS.map(s => s.file + '|' + s.n).join(';');
  if (!SECTIONS.length) {
    if (SECT_SIG !== sig) {
      SECT_SIG = sig;
      $('rushes').innerHTML = '<div class="empty">Section cuts appear here after a render. Each one is cut from the masked master.</div>';
    }
    return;
  }
  if (sig !== SECT_SIG) {
    SECT_SIG = sig;
    paintRushes();
  }
}

function paintRushes() {
  if (!SECTIONS.length) return;
  const mode = $('sort').value;
  const list = SECTIONS.slice();
  if (mode === 'longest') list.sort((a, b) => b.duration - a.duration);
  else if (mode === 'shortest') list.sort((a, b) => a.duration - b.duration);
  else if (mode === 'name') list.sort((a, b) => plain(a.title || a.module).localeCompare(plain(b.title || b.module)));
  else list.sort((a, b) => a.t0 - b.t0);
  $('rushes').innerHTML = list.map(s => {
    const name = plain(s.title && s.title !== s.module ? s.title : (s.module || 'section'));
    return `<div class="cut">
      <button class="shot" data-n="${s.n}" aria-label="Play ${ht(name)}">
        <img src="${u('/f/' + s.poster)}" alt="" loading="lazy">
        <span class="ord">${String(s.n).padStart(2, '0')}</span>
      </button>
      <div class="cap"><b>${ht(name)}</b><span>${ff(s.t0)}-${ff(s.t1)} · ${dur(s.duration)}</span>
        <a class="dl" href="${u('/f/' + s.file, {dl: 1})}">Download</a></div>
    </div>`;
  }).join('');
}

function openCut(n) {
  const s = SECTIONS.find(x => String(x.n) === String(n));
  if (!s) return;
  $('plTitle').textContent = plain(s.title || s.module || 'section');
  $('plVid').src = u('/f/' + s.file);
  $('plNote').textContent = `${ff(s.t0)}-${ff(s.t1)} · ${dur(s.duration)} · cut from the masked master`;
  if (typeof $('player').showModal === 'function') $('player').showModal();
}

async function tick() {
  let j;
  try {
    j = await (await fetch(u('/state'))).json();
  } catch (e) {
    $('nowTitle').textContent = 'state fetch failed';
    $('nowSub').textContent = String(e);
    return;
  }
  $('app').textContent = j.app || j.session;
  $('meta').textContent = [j.package, j.duration ? Math.round(j.duration) + 's' : ''].filter(Boolean).join(' · ');
  $('take-sub').textContent = j.device ? `${j.device} ${j.size ? j.size.join('x') : ''}` : 'no device attached';

  const st = $('stage');
  st.textContent = j.stale ? 're-render needed' : (j.run && j.run.stages && j.run.stages.some(x => x.state === 'running') ? 'running' : j.stage);
  const live = j.run && j.run.stages && j.run.stages.some(x => x.state === 'running');
  st.className = 'pill ' + (j.stale ? 'bad' : live ? 'live' : (j.video_ready ? 'ok' : 'warn'));

  const rb = $('reRender');
  rb.hidden = !j.stale;
  rb.title = 'The boxes, the walk or the timings changed after this video was rendered';
  if (j.stale && !rb.dataset.wired) {
    rb.dataset.wired = '1';
    rb.onclick = () => {
      rb.disabled = true;
      post('/api/render').then(x => {
        note('re-rendering: ' + x.stages.join(' then '), true);
        loadInputs();
      }).catch(e => note('re-render failed: ' + String(e).slice(0, 120)))
        .finally(() => { rb.disabled = false; });
    };
  }

  const rs = (j.run && j.run.stages) || [];
  $('strip').innerHTML = stageChips(rs) || '<span class="stage-chip idle"><i></i>no run yet</span>';
  $('strip').insertAdjacentHTML('beforeend',
    `<span class="run-meta">${j.run && j.run.started ? 'run ' + ht(j.run.started) : 'session ' + ht(j.session)}
     ${j.run && j.run.raw ? ' · export carries the raw capture' : ''}</span>`);

  const s = j.step || {};
  const running = rs.length && rs.some(x => x.state === 'running');
  if (s.i !== undefined) {
    $('nowTitle').textContent = s.chapter || 'in take';
    $('nowSub').textContent = `step ${s.i} · ${s.label || 'working'}`;
  } else if (running) {
    const on = rs.find(x => x.state === 'running');
    $('nowTitle').textContent = on ? on.name : 'working';
    $('nowSub').textContent = 'stage in progress';
  } else {
    $('nowTitle').textContent = j.video_ready ? 'master ready' : 'nothing running';
    $('nowSub').textContent = j.video_ready ? 'play it below, or cut a share link' : 'start a run from the call sheet';
  }
  $('pb').style.width = (j.percent || 0) + '%';
  $('pct').textContent = j.percent != null ? `render ${j.percent}%` : '';

  paintRail(j.chapters || [], s);
  paintSteps(j.steps || []);
  paintArtifacts(j.artifacts || []);
  boxes = j.boxes || [];
  BOXES = boxes;
  draw();
  paintMasks(j);
  paintWalk(j.walk || []);
  paintSections(j);

  ART = {};
  (j.artifacts || []).forEach(a => { ART[a.name] = a.rel; });
  const want = $('vRaw').checked ? 'recording.mp4' : 'demo_debranded.mp4';
  const vid = $('vid');
  if (ART[want] && vid.dataset.rel !== ART[want]) {
    vid.dataset.rel = ART[want];
    vid.src = u('/f/' + ART[want], {dl: 1});
    vid.load();
  }
  if (!ART[want] && vid.dataset.rel) {
    vid.removeAttribute('src');
    delete vid.dataset.rel;
    vid.load();
  }
  $('vinfo').textContent = ART[want]
    ? `${want} · ${kb((j.artifacts.find(a => a.name === want) || {}).bytes || 0)}`
    : 'not built yet';

  const liveImg = $('live');
  if (!liveImg.dataset.on) {
    liveImg.dataset.on = '1';
    liveImg.onerror = () => {
      liveImg.hidden = true;
      $('err').textContent = 'No live device and no still captured yet. The frame appears once adb is attached or a take writes a still.';
    };
    liveImg.onload = () => { liveImg.hidden = false; $('err').textContent = ''; };
    liveImg.src = u('/stream');
  }

  if (!$('evi').dataset.n) {
    $('evi').dataset.n = '1';
    fetch(u('/evidences')).then(r => r.json()).then(l => {
      $('evi').innerHTML = l.slice(0, 400).map(e =>
        `<a href="${u('/f/' + e.rel, {dl: 1})}" target="_blank" rel="noopener">${ht(e.rel)}</a>`).join('')
        + (l.length > 400 ? `<span class="n">...${l.length - 400} more - use download all</span>` : '')
        || '<span class="n">no evidence stills yet</span>';
    }).catch(() => {});
  }
}

/* ---- chapters seek ------------------------------------------------------- */

$('rail').onclick = e => {
  const b = e.target.closest && e.target.closest('[data-t]');
  if (!b) return;
  const vid = $('vid');
  if (!vid.src) { note('no master video yet', false); return; }
  vid.currentTime = parseFloat(b.dataset.t) || 0;
  vid.play().catch(() => {});
  vid.scrollIntoView({block: 'center', behavior: 'smooth'});
};

/* ---- rushes + player ----------------------------------------------------- */

$('sort').onchange = paintRushes;
$('rushes').onclick = e => {
  const b = e.target.closest && e.target.closest('.shot');
  if (b) openCut(b.dataset.n);
};
$('plClose').onclick = () => { $('plVid').pause(); $('player').close(); };
$('player').addEventListener('close', () => { $('plVid').pause(); $('plVid').removeAttribute('src'); });

/* ---- master swap --------------------------------------------------------- */

$('vMask').onchange = tick;
$('vRaw').onchange = tick;

/* ---- picker -------------------------------------------------------------- */

function pkPos(ev) {
  const im = $('pkImg');
  const r = im.getBoundingClientRect();
  const W = PKS.size[0] || im.naturalWidth || 1;
  const H = PKS.size[1] || im.naturalHeight || 1;
  return [Math.round((ev.clientX - r.left) / r.width * W), Math.round((ev.clientY - r.top) / r.height * H)];
}

function pkDraw() {
  const b = $('pkBox');
  if (!pkDrag || !pkDrag.w || !pkDrag.h) { b.style.display = 'none'; return; }
  const W = PKS.size[0] || 1;
  const H = PKS.size[1] || 1;
  b.style.display = 'block';
  b.style.left = (pkDrag.x / W * 100) + '%';
  b.style.top = (pkDrag.y / H * 100) + '%';
  b.style.width = (pkDrag.w / W * 100) + '%';
  b.style.height = (pkDrag.h / H * 100) + '%';
}

function pkOld() {
  const o = $('pkOld');
  if (!o) return;
  const s = PKS.stills[+($('pkSel').value || 0)];
  if (!s) { o.innerHTML = ''; return; }
  o.innerHTML = BOXES.filter(b => b.scope === 'screen:' + s.screen || b.scope === 'global').map(b =>
    `<div data-i="${b.i}" title="Drag to move - ${ht(b.label)}"
      style="left:${b.frac[0] * 100}%;top:${b.frac[1] * 100}%;width:${b.frac[2] * 100}%;height:${b.frac[3] * 100}%"></div>`).join('');
}

function pkMove(e) {
  if (!pkMv) return true;
  const p = pkPos(e);
  const W = PKS.size[0] || 1;
  const H = PKS.size[1] || 1;
  pkMv.x = Math.max(0, Math.min(W - pkMv.w, pkMv.sx + p[0] - pkMv.px));
  pkMv.y = Math.max(0, Math.min(H - pkMv.h, pkMv.sy + p[1] - pkMv.py));
  pkMv.d.style.left = (pkMv.x / W * 100) + '%';
  pkMv.d.style.top = (pkMv.y / H * 100) + '%';
  return false;
}

async function pkDrop() {
  const m = pkMv;
  pkMv = null;
  if (!m) return;
  try {
    const j = await post('/api/box', {
      op: 'set', i: m.i, x: Math.round(m.x), y: Math.round(m.y),
      w: Math.round(m.w), h: Math.round(m.h)
    });
    $('pkNote').textContent = 'Box moved to ' + j.rect[0] + ',' + j.rect[1] + ' - re-render to apply.';
  } catch (err) {
    $('pkNote').textContent = 'Move failed: ' + String(err).slice(0, 120);
  }
  tick();
}

const pkW = $('pkWrap');
pkW.onpointerdown = e => {
  const d = e.target.closest && e.target.closest('[data-i]');
  if (d) {
    const b = BOXES.find(x => String(x.i) === d.dataset.i);
    if (!b) return;
    const p = pkPos(e);
    const W = PKS.size[0] || 1;
    const H = PKS.size[1] || 1;
    pkMv = {d: d, i: b.i, sx: b.frac[0] * W, sy: b.frac[1] * H, w: b.frac[2] * W, h: b.frac[3] * H,
            px: p[0], py: p[1], x: b.frac[0] * W, y: b.frac[1] * H};
    return;
  }
  e.preventDefault();
  const p = pkPos(e);
  pkDrag = {ax: p[0], ay: p[1], x: p[0], y: p[1], w: 0, h: 0};
  pkW.setPointerCapture(e.pointerId);
};
pkW.onpointermove = e => {
  if (pkMv) { pkMove(e); return; }
  if (!pkDrag) return;
  const p = pkPos(e);
  pkDrag.x = Math.min(pkDrag.ax, p[0]);
  pkDrag.y = Math.min(pkDrag.ay, p[1]);
  pkDrag.w = Math.abs(p[0] - pkDrag.ax);
  pkDrag.h = Math.abs(p[1] - pkDrag.ay);
  pkDraw();
};
pkW.onpointerup = () => {
  if (pkMv) { pkDrop(); return; }
  pkDraw();
};

async function loadPick() {
  const el = $('pkSel');
  try {
    const j = await (await fetch(u('/api/picklist'))).json();
    const sig = (j.stills || []).map(s => s.still).join('|') + '@' + (j.size || []).join('x');
    PKS = j;
    if (sig !== PKS_SIG) {
      PKS_SIG = sig;
      el.innerHTML = j.stills.length
        ? j.stills.map((s, i) => `<option value="${i}">${ht(s.still)} - ${ht(s.screen || '?')}</option>`).join('')
        : '<option>no stills captured yet</option>';
      if (j.stills.length) { $('pkImg').src = u('/f/stills/' + j.stills[0].still); $('pkImg').hidden = false; }
      pkDrag = null;
      pkDraw();
      pkOld();
    } else {
      pkOld();
    }
  } catch (e) {
    /* picker is optional until a take writes a still */
  }
}

$('pkSel').onchange = () => {
  const s = PKS.stills[+($('pkSel').value || 0)];
  if (!s) return;
  $('pkImg').src = u('/f/stills/' + s.still);
  $('pkImg').hidden = false;
  pkDrag = null;
  pkDraw();
  pkOld();
};

$('pkAdd').onclick = async () => {
  const n = $('pkNote');
  if (!pkDrag || pkDrag.w < 8 || pkDrag.h < 8) { n.textContent = 'Drag a box over the mark first.'; return; }
  const s = PKS.stills[+($('pkSel').value || 0)];
  if (!s) { n.textContent = 'No still selected.'; return; }
  const btn = $('pkAdd');
  btn.disabled = true;
  n.textContent = 'Adding...';
  try {
    const r = await fetch(u('/api/box', {
      screen: s.screen, x: pkDrag.x, y: pkDrag.y, w: pkDrag.w, h: pkDrag.h,
      label: $('pkLabel').value || 'brand mark', mode: $('pkMode').value
    }), {method: 'POST'});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('http ' + r.status));
    n.textContent = 'Added ' + j.mode + ' box on ' + j.screen + ' - re-render to apply.';
    pkDrag = null;
    pkDraw();
    tick();
  } catch (e) {
    n.textContent = 'Failed: ' + e.message;
  } finally {
    btn.disabled = false;
  }
};

$('mask').onclick = e => {
  const b = e.target.closest && e.target.closest('button[data-rm]');
  if (!b) return;
  b.disabled = true;
  post('/api/box', {op: 'rm', i: b.dataset.rm}).then(j => {
    $('pkNote').textContent = 'Removed ' + (j.label || 'box') + ' - re-render again.';
    tick();
  }).catch(() => { b.disabled = false; });
};
$('mask').onchange = e => {
  const s = e.target.closest && e.target.closest('select[data-sc]');
  if (!s) return;
  post('/api/box', {op: 'set', i: s.dataset.sc, screen: s.value}).then(j => {
    $('pkNote').textContent = 'Box now on ' + j.scope + ' - re-render again.';
    tick();
  }).catch(() => tick());
};

/* ---- walk editing -------------------------------------------------------- */

$('walk').onchange = e => {
  const t = e.target;
  const si = t.dataset.ws;
  const li = t.dataset.wl;
  if (si === undefined && li === undefined) return;
  const q = {op: 'set', i: si !== undefined ? si : li};
  if (si !== undefined) q.screen = t.value; else q.label = t.value;
  post('/api/walk', q).then(x => {
    note('step ' + x.i + ' saved - re-render to move the windows', true);
    tick();
  }).catch(err => note('walk edit failed: ' + String(err).slice(0, 120), false));
};
$('walk').onclick = e => {
  const b = e.target.closest && e.target.closest('button[data-rmw]');
  if (!b) return;
  b.disabled = true;
  post('/api/walk', {op: 'rm', i: b.dataset.rmw}).then(x => {
    note('step ' + x.i + ' dropped - re-render', true);
    tick();
  }).catch(err => { note('drop failed: ' + String(err).slice(0, 120), false); b.disabled = false; });
};

/* ---- inputs -------------------------------------------------------------- */

async function loadInputs() {
  let j;
  try {
    j = await (await fetch(u('/api/inputs'))).json();
  } catch (e) {
    $('facts').innerHTML = `<dt>Inputs</dt><dd><span class="pill bad">fetch failed</span></dd>`;
    return;
  }
  const apk = j.apk ? `${ht(j.apk.name)} · ${kb(j.apk.bytes)}` : '<span class="n">none uploaded</span>';
  const brief = j.brief ? `${ht(j.brief.name)} · ${kb(j.brief.bytes)}` : '<span class="n">none</span>';
  const c = j.cases;
  const scope = (j.scope && j.scope.name) || 'focused';
  const focusedNeeds = scope === 'focused' && !j.brief;
  const cases = c
    ? `${c.count} from ${ht(c.origin)}${c.counts && c.counts.from_spec ? ` · ${c.counts.from_spec} spec` : ''}${c.counts && c.counts.from_flow ? ` · ${c.counts.from_flow} app` : ''}
       <a href="${u('/f/CASES.md')}" target="_blank" rel="noopener">open</a>
       ${c.stale ? ' <span class="pill warn">rebuild for this scope</span>' : ''}`
    : '<span class="n">not built yet</span>';
  $('facts').innerHTML =
    `<dt>APK</dt><dd>${apk}</dd>
     <dt>Spec</dt><dd>${brief}</dd>
     <dt>Cases</dt><dd>${cases}</dd>
     <dt>Run</dt><dd>${j.running ? '<span class="pill live">running</span>' : '<span class="n">idle</span>'}</dd>`;
  if (focusedNeeds) {
    $('facts').insertAdjacentHTML('beforeend',
      '<dt></dt><dd><div class="warnbox" style="margin-top:6px">Focused scope needs a spec. Upload a .md, .txt, .xml or .pdf, or switch to Full app.</div></dd>');
  }
  $('call-sub').textContent = (j.scope && j.scope.label ? j.scope.label : scope) + ' scope';
  document.querySelectorAll('input[name=scope]').forEach(r => { r.checked = r.value === scope; });
  $('ilog').textContent = j.log || 'nothing logged yet';
  $('bRun').disabled = !!j.running || !j.apk;
  $('bRun').title = j.apk ? '' : 'Upload an APK first';
}

async function uploadFile(kind, inputId) {
  const el = $(inputId);
  const file = el.files[0];
  if (!file) { note('Pick a ' + kind + ' file first.', false); return; }
  const btn = $(kind === 'apk' ? 'bApk' : 'bBrief');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Uploading...';
  note('Sending ' + kb(file.size) + '...', true);
  try {
    const r = await fetch(u('/api/upload', {kind: kind, name: file.name}), {method: 'POST', body: file});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('http ' + r.status));
    note(kind + ' uploaded: ' + j.name + ' · ' + kb(j.bytes), true);
    el.value = '';
    loadInputs();
  } catch (e) {
    note('Upload rejected: ' + e.message, false);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

$('bApk').onclick = () => uploadFile('apk', 'fApk');
$('bBrief').onclick = () => uploadFile('brief', 'fBrief');

document.querySelectorAll('input[name=scope]').forEach(r => {
  r.onchange = () => {
    const scope = r.value;
    note('Switching to ' + scope + ' scope...', true);
    post('/api/scope', {scope: scope}).then(j => {
      note(j.label + ' scope - rebuild the cases before the next run.', true);
      loadInputs();
    }).catch(e => note('scope change failed: ' + String(e).slice(0, 120), false));
  };
});

$('bCases').onclick = () => {
  const btn = $('bCases');
  btn.disabled = true;
  note('Building cases...', true);
  post('/api/cases').then(j => {
    const parts = [`${j.count} case(s) from the ${j.origin}`];
    if (j.counts && j.counts.from_spec) parts.push(j.counts.from_spec + ' spec');
    if (j.counts && j.counts.from_flow) parts.push(j.counts.from_flow + ' app');
    note(parts.join(' · '), true);
    if (j.warning) note(j.warning, false);
    loadInputs();
  }).catch(e => note('Cases failed: ' + String(e).slice(0, 160), false))
    .finally(() => { btn.disabled = false; });
};

$('bRun').onclick = () => {
  const btn = $('bRun');
  btn.disabled = true;
  note('Run queued...', true);
  const raw = $('raw') && $('raw').checked;
  post('/api/run', raw ? {raw: 1} : {}).then(j => {
    note('Run started: ' + j.stages.join(' → ')
      + (j.export_includes_raw ? ' · the export will carry the unmasked capture' : ' · the export excludes the unmasked capture'), true);
    loadInputs();
  }).catch(e => note('Run failed: ' + String(e).slice(0, 200), false))
    .finally(() => loadInputs());
};

$('ex').onclick = async e => {
  e.target.disabled = true;
  const label = e.target.textContent;
  e.target.textContent = 'Exporting...';
  try {
    const raw = $('raw') && $('raw').checked;
    const j = await post('/api/export', raw ? {raw: 1} : {});
    note('Bundle rebuilt ' + (j.raw ? 'with' : 'without')
      + ' the unmasked capture (' + kb(j.zip) + ').', !!j.raw);
    e.target.textContent = 'Exported';
    e.target.disabled = false;
    setTimeout(() => { e.target.textContent = label; }, 1400);
  } catch (err) {
    note('Export failed: ' + String(err).slice(0, 140), false);
    e.target.textContent = 'Export failed';
    setTimeout(() => { e.target.textContent = label; e.target.disabled = false; }, 1600);
  }
};

$('allimg').onclick = e => { e.preventDefault(); location = u('/images.zip'); };

/* ---- share --------------------------------------------------------------- */

function paintShare(j) {
  $('smtp').textContent = j.smtp ? 'SMTP set' : 'local outbox';
  $('smtp').className = 'pill ' + (j.smtp ? 'ok' : 'warn');
  $('smtp').title = j.smtp
    ? 'Triggers are sent through ADRD_SMTP_* configuration'
    : 'No SMTP configured. Every trigger is written to session/share/outbox and can be sent later.';
  if (j.link) {
    $('shareUrl').value = j.link;
    $('bShare').hidden = true;
    $('bCopy').hidden = false;
  }
  const ev = j.events;
  if (ev) {
    $('events').innerHTML = ev.map(e =>
      `<li><time>${ht((e.at || '').slice(11, 19))}</time><span><b>${ht(e.event)}</b> ${ht(e.subject || '')}</span></li>`).join('');
  }
}

async function loadShare() {
  try {
    const j = await (await fetch(u('/api/share'))).json();
    paintShare(j);
  } catch (e) {
    $('smtp').textContent = 'share unavailable';
    $('smtp').className = 'pill bad';
  }
}

$('bShare').onclick = () => {
  post('/api/share').then(j => {
    paintShare(j);
    $('shareNote').textContent = 'Live link ready. Send it, or add an email below.';
  }).catch(e => { $('shareNote').textContent = 'Could not create the link: ' + String(e).slice(0, 120); });
};

$('bCopy').onclick = async () => {
  const v = $('shareUrl').value;
  try {
    await navigator.clipboard.writeText(v);
    $('shareNote').textContent = 'Link copied.';
  } catch (e) {
    $('shareUrl').select();
    document.execCommand('copy');
    $('shareNote').textContent = 'Link copied.';
  }
};

$('bMail').onclick = () => {
  const email = $('shareEmail').value.trim();
  if (!email) { $('shareNote').textContent = 'Type an email first.'; return; }
  const btn = $('bMail');
  btn.disabled = true;
  $('shareNote').textContent = 'Adding ' + email + '...';
  post('/api/share', {email: email}).then(j => {
    paintShare(j);
    $('shareNote').textContent = j.smtp
      ? 'Link sent to ' + email + '. They will get a note when a run starts and when it finishes.'
      : 'Link queued for ' + email + ' in the local outbox. Set ADRD_SMTP_* to deliver it.';
    $('shareEmail').value = '';
  }).catch(e => { $('shareNote').textContent = 'Could not add the email: ' + String(e).slice(0, 120); })
    .finally(() => { btn.disabled = false; });
};

/* ---- boot ---------------------------------------------------------------- */

loadPick();
loadInputs();
loadShare();
tick();
setInterval(tick, 2000);
setInterval(loadPick, 20000);
setInterval(loadInputs, 5000);
setInterval(loadShare, 15000);
