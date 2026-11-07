const $ = (s) => document.querySelector(s);
const ctx = new (window.AudioContext || window.webkitAudioContext)();
const titles = new Set([...document.querySelectorAll('#titles option')].map(o => o.value.toLowerCase()));
let state = null, buffer = null, source = null, raf = null;

const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

async function api(path, body) {
  const opts = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

const MESSAGES = {
  correct: (s) => `Richtig, ${s.history.at(-1).points} Punkte bei ${s.history.at(-1).time}s.`,
  wrong:   (s) => `Leider falsch. Jetzt hörst du ${s.stages[s.stage]}s.`,
  skipped: (s) => `Übersprungen. Jetzt hörst du ${s.stages[s.stage]}s.`,
  failed:  ()  => 'Nicht erraten, 0 Punkte.',
};

// ---------- Anzeige ----------
function render(s) {
  state = s;
  const n = s.stages.length;
  const last = s.done ? s.history.at(-1) : null;

  $('#round').textContent = s.round;
  $('#rounds').textContent = s.rounds;
  $('#score').textContent = s.score;

  $('#stages').innerHTML = s.stages.map((sec, i) => {
    let cls = '';
    if (i < s.stage) cls = 'open miss';
    else if (i === s.stage) cls = !s.done ? 'open current' : (last.points ? 'open hit' : 'open miss');
    else if (s.done) cls = 'open';
    return `<div class="stage ${cls}" style="--h:${(i + 1) / n * 100}%">
              <div class="bar"><i></i></div><b>${sec}s</b><small>${s.points[i]} Punkte</small>
            </div>`;
  }).join('');

  $('#next-len').textContent = s.stage < n - 1 ? `(${s.stages[s.stage + 1]}s hören)` : '(aufgeben)';
  $('#feedback').textContent = s.result ? MESSAGES[s.result](s) : '';
  $('#feedback').className = s.result || '';

  $('#ask').hidden = s.done;
  $('#reveal').hidden = !s.done;
  $('#next').hidden = s.finished;
  if (s.done) {
    $('#reveal .answer').innerHTML =
      `${esc(s.answer)}<a href="https://youtu.be/${esc(s.youtube_id)}" target="_blank" rel="noopener">Auf YouTube anhören</a>`;
  }

  $('#end').hidden = !s.finished;
  document.body.classList.toggle('is-finished', s.finished);
  if (s.finished) {
    $('#reveal').hidden = true;
    $('#final').textContent = s.score;
    $('#summary').innerHTML = s.history.map(h => `
      <li><span class="t">${esc(h.title)}</span>
          <span class="r ${h.points ? '' : 'zero'}">${h.time !== null ? h.time + 's, ' : ''}<b>${h.points}</b></span></li>`).join('');
    $('#save').hidden = s.saved;
  }
}

// Fortschritt beim Abspielen: Balken füllen sich nacheinander, Ring läuft mit
function setProgress(t) {
  const S = state.stages;
  const bars = document.querySelectorAll('#stages .bar i');
  let prev = 0;
  S.forEach((sec, i) => {
    const f = Math.min(Math.max((t - prev) / (sec - prev), 0), 1);
    bars[i]?.style.setProperty('--fill', f);
    prev = sec;
  });
  $('#play').style.setProperty('--p', buffer ? Math.min(t / buffer.duration, 1) : 0);
}

// ---------- Audio ----------
function stop() {
  if (source) { source.onended = null; try { source.stop(); } catch {} source = null; }
  cancelAnimationFrame(raf);
  $('#play').classList.remove('is-playing');
  if (state) setProgress(0);
}

async function play() {
  if (source) return stop();          // zweiter Klick stoppt
  await ctx.resume();
  if (!buffer) {
    $('#play').disabled = true;
    try {
      const data = await (await fetch('/api/clip')).arrayBuffer();
      buffer = await ctx.decodeAudioData(data);
    } finally {
      $('#play').disabled = false;
    }
  }
  source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(ctx.destination);
  source.onended = stop;
  const t0 = ctx.currentTime;
  source.start();
  $('#play').classList.add('is-playing');
  const tick = () => { setProgress(ctx.currentTime - t0); raf = requestAnimationFrame(tick); };
  tick();
}

// ---------- Aktionen ----------
async function guess() {
  const g = $('#guess').value.trim();
  if (!g) return;
  if (!titles.has(g.toLowerCase())) {
    $('#feedback').textContent = 'Wähl einen Titel aus der Vorschlagsliste aus.';
    $('#feedback').className = 'wrong';
    return;
  }
  stop(); buffer = null;
  $('#guess').value = '';
  render(await api('/api/guess', {guess: g}));
}

async function skip() {
  stop(); buffer = null;
  render(await api('/api/skip', {}));
}

async function next() {
  stop(); buffer = null;
  render(await api('/api/next', {}));
  $('#guess').focus();
}

$('#play').onclick = play;
$('#send').onclick = guess;
$('#skip').onclick = skip;
$('#next').onclick = next;
$('#guess').addEventListener('keydown', e => { if (e.key === 'Enter') guess(); });
$('#save').onsubmit = async (e) => {
  e.preventDefault();
  const {id} = await api('/api/highscore', {name: $('#name').value});
  location.href = `${HS_URL}&hl=${id}`;
};

api('/api/state').then(render);
