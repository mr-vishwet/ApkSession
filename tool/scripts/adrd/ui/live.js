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
const plain = t => String(t || '').replace(/^\d+\.\s+/, '');

let SECTIONS = [];
let SECT_SIG = '';
let FEED_ON = false;

function paintSections(j) {
  SECTIONS = j.sections || [];
  const sig = SECTIONS.map(s => s.file).join('|');
  $('cuts-sub').innerHTML = SECTIONS.length
    ? `<b>${SECTIONS.length}</b> · ${dur(j.sections_total || 0)}`
    : '';
  if (sig === SECT_SIG) return;
  SECT_SIG = sig;
  $('cuts').innerHTML = SECTIONS.length ? SECTIONS.map(s => {
    const name = plain(s.title && s.title !== s.module ? s.title : (s.module || 'section'));
    return `<div class="cut">
      <button class="shot" data-n="${s.n}" aria-label="Play ${ht(name)}">
        <img src="${u('/f/' + s.poster)}" alt="" loading="lazy">
        <span class="ord">${String(s.n).padStart(2, '0')}</span>
      </button>
      <div class="cap"><b>${ht(name)}</b><span>${ff(s.t0)}-${ff(s.t1)} · ${dur(s.duration)}</span></div>
    </div>`;
  }).join('') : '<div class="empty">Section cuts appear here as the run is rendered.</div>';
}

function paintDownloads(j) {
  const links = [];
  if (j.video_ready) links.push(`<a href="${u('/f/render/demo_debranded.mp4', {dl: 1})}">Masked master · mp4</a>`);
  if (j.report_ready) links.push(`<a href="${u('/f/render/SESSION_DOC.pdf', {dl: 1})}">Session report · pdf</a>`);
  else if (j.doc_ready) links.push(`<a href="${u('/f/render/SESSION_DOC.md', {dl: 1})}">Session report · md</a>`);
  $('downloads').innerHTML = links.length ? links.join('') : '<span class="n">Nothing to download yet.</span>';
}

function paintRail(chapters) {
  const vid = $('vid');
  $('rail').innerHTML = chapters.length ? chapters.map(c =>
    `<div class="ch"><time>${ff(c.t0)}-${ff(c.t1)}</time>
      <b>${ht(c.title || 'chapter')}</b>
      <button class="btn small" data-t="${c.t0}" ${vid.hidden ? 'disabled' : ''}>Play</button></div>`).join('')
    : '<div class="empty">No chapters yet.</div>';
}

async function tick() {
  let j;
  try {
    j = await (await fetch(u('/state'))).json();
  } catch (e) {
    $('stateText').textContent = 'offline';
    return;
  }
  $('app').textContent = j.app || j.session;
  $('meta').textContent = j.package || '';
  $('title').textContent = j.app || 'Live session';
  $('stage').textContent = j.stale ? 're-render pending' : (j.running ? 'live run' : j.stage);
  $('stage').className = 'pill ' + (j.stale ? 'bad' : j.running ? 'live' : (j.video_ready ? 'ok' : 'warn'));
  $('stateText').textContent = j.running ? 'running' : (j.stage || '-');
  $('len').textContent = j.duration ? Math.round(j.duration) + 's' : '-';
  $('cutCount').textContent = (j.sections || []).length || '-';
  $('updated').textContent = j.updated || '-';
  $('pb').style.width = (j.percent || 0) + '%';
  $('pct').textContent = j.percent != null ? `render ${j.percent}%` : (j.running ? 'take in progress' : 'no render running');

  const vid = $('vid');
  const live = $('live');
  if (j.video_ready) {
    if (!vid.dataset.on) {
      vid.dataset.on = '1';
      vid.src = u('/f/render/demo_debranded.mp4');
    }
    vid.hidden = false;
    live.hidden = true;
    $('feedNote').textContent = 'masked master';
  } else {
    vid.hidden = true;
    $('feedNote').textContent = FEED_ON ? 'live device feed · unmasked' : 'waiting for the first frame';
    if (!FEED_ON) {
      FEED_ON = true;
      live.onload = () => { live.hidden = false; };
      live.onerror = () => {
        live.hidden = true;
        $('feedNote').textContent = 'no live frame yet';
      };
      live.src = u('/stream');
    }
  }

  paintSections(j);
  paintRail(j.chapters || []);
  paintDownloads(j);
  $('events').innerHTML = (j.events || []).map(e =>
    `<li><time>${ht((e.at || '').slice(11, 19))}</time><span><b>${ht(e.event)}</b> ${ht(e.subject || '')}</span></li>`).join('')
    || '<li class="n">No activity yet.</li>';
}

$('cuts').onclick = e => {
  const b = e.target.closest && e.target.closest('.shot');
  if (!b) return;
  const s = SECTIONS.find(x => String(x.n) === String(b.dataset.n));
  if (!s) return;
  $('plTitle').textContent = plain(s.title || s.module || 'section');
  $('plVid').src = u('/f/' + s.file);
  if (typeof $('player').showModal === 'function') $('player').showModal();
};
$('plClose').onclick = () => { $('plVid').pause(); $('player').close(); };
$('player').addEventListener('close', () => { $('plVid').pause(); $('plVid').removeAttribute('src'); });

$('rail').onclick = e => {
  const b = e.target.closest && e.target.closest('button[data-t]');
  if (!b) return;
  const vid = $('vid');
  vid.currentTime = parseFloat(b.dataset.t) || 0;
  vid.play().catch(() => {});
  vid.scrollIntoView({block: 'center', behavior: 'smooth'});
};

$('subForm').onsubmit = async e => {
  e.preventDefault();
  const email = $('email').value.trim();
  const btn = $('bSub');
  btn.disabled = true;
  $('subNote').textContent = 'Adding ' + email + '...';
  try {
    const r = await fetch(u('/subscribe'), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email: email})
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ('http ' + r.status));
    $('subNote').textContent = 'You are on the list. Keep this link - it opens the same session.';
    $('email').value = '';
  } catch (err) {
    $('subNote').textContent = 'Could not add you: ' + err.message;
  } finally {
    btn.disabled = false;
  }
};

tick();
setInterval(tick, 2500);
