const $ = (s) => document.querySelector(s);
const ctx = new (window.AudioContext || window.webkitAudioContext)();
const master = ctx.createGain();            // volume
master.connect(ctx.destination);
const FADE = 0.008;                         // seconds of fade-out at the end of a snippet, so the cut doesn't click
let state = null, clip = null, source = null, raf = null, duration = 0;

const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
// 'Beyoncé - Déjà Vu' -> 'beyoncedejavu': typing without accents, punctuation or spaces still finds the song
const fold = (s) => s.normalize('NFKD').replace(/[^\p{L}\p{N}]+/gu, '').toLowerCase();
const store = {
  get: (k) => { try { return localStorage.getItem(`songless-${k}`); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(`songless-${k}`, v); } catch {} },
};

const SONGS = TITLES.map(t => ({t, f: fold(t)}));
const byFold = new Map(SONGS.map(s => [s.f, s.t]));
let tried = new Set();                      // titles already guessed for this song: no longer suggested

async function api(path, body) {
  const opts = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

const MESSAGES = {
  correct: (s) => `Correct! ${s.history.at(-1).points} points at ${s.history.at(-1).time}s.`,
  artist:  (s) => `Right artist, wrong song. You now hear ${s.stages[s.stage]}s.`,
  wrong:   (s) => `Wrong. You now hear ${s.stages[s.stage]}s.`,
  skipped: (s) => `Skipped. You now hear ${s.stages[s.stage]}s.`,
  failed:  (s) => s.mode === 'survival' ? 'Not guessed. Your run ends here.' : 'Not guessed, 0 points.',
};
const MARK_CLASS = {x: 'miss', s: 'skip', a: 'artist', o: 'hit'};
const EMOJI = {x: '🟥', s: '⬛', a: '🟨', o: '🟩'};
const grid = (h, n) => h.marks.map(m => EMOJI[m]).join('') + '⬜'.repeat(n - h.marks.length);

function flash(text, cls = 'wrong') {
  $('#feedback').textContent = text;
  $('#feedback').className = cls;
}

// ---------- Rendering ----------
function render(s) {
  state = s;
  const n = s.stages.length;
  const survival = s.mode === 'survival';

  $('#round').textContent = s.round;
  $('#rounds').textContent = s.rounds;
  $('#of').hidden = survival;               // survival has no fixed number of songs
  $('#score').textContent = s.score;

  $('#stages').style.setProperty('--n', n);
  $('#stages').innerHTML = s.stages.map((sec, i) => {
    let cls = '';
    if (i < s.marks.length) cls = `open ${MARK_CLASS[s.marks[i]]}`;
    else if (i === s.stage && !s.done) cls = 'open current';
    else if (s.done) cls = 'open';
    return `<div class="stage ${cls}" style="--h:${(i + 1) / n * 100}%">
              <div class="bar"><i></i></div><b>${sec}s</b><small>${s.points[i]} points</small>
            </div>`;
  }).join('');

  $('#next-len').textContent = s.stage < n - 1 ? `(hear ${s.stages[s.stage + 1]}s)` : '(give up)';
  flash(s.result ? MESSAGES[s.result](s) : '', s.result || '');

  $('#ask').hidden = s.done;
  $('#reveal').hidden = !s.done || s.finished;
  if (s.done && !s.finished) {
    $('#thumb').hidden = false;
    $('#thumb').src = `https://i.ytimg.com/vi/${encodeURIComponent(s.youtube_id)}/mqdefault.jpg`;
    $('#reveal .answer').innerHTML =
      `${esc(s.answer)}<a href="https://youtu.be/${esc(s.youtube_id)}" target="_blank" rel="noopener">Listen on YouTube</a>`;
    $('#stat').textContent = s.plays > 1 ? `Guessed in ${s.hits} of ${s.plays} plays (${Math.round(100 * s.hits / s.plays)}%).` : '';
  }

  $('#end').hidden = !s.finished;
  document.body.classList.toggle('is-finished', s.finished);
  if (s.finished) {
    const hits = s.history.filter(h => h.points).length;
    $('#final').textContent = s.score;
    $('#end-note').textContent =
      survival ? `You guessed ${hits} ${hits === 1 ? 'song' : 'songs'} in a row.`
      : s.mode === 'daily' ? `Daily challenge ${s.day}. New songs tomorrow.` : '';
    $('#summary').innerHTML = s.history.map(h => `
      <li><span class="t">${esc(h.title)}</span><span class="m" aria-hidden="true">${grid(h, n)}</span>
          <span class="r ${h.points ? '' : 'zero'}">${h.time !== null ? h.time + 's, ' : ''}<b>${h.points}</b></span></li>`).join('');
    $('#save').hidden = s.saved || !s.score;      // nothing to save with 0 points
  }

  // fetch the unlocked snippet right away, so playback starts without a delay
  clip = s.done ? null : loadClip();
}

// Playback progress: bars fill one after another, the ring follows along
function setProgress(t) {
  const bars = document.querySelectorAll('#stages .bar i');
  let prev = 0;
  state.stages.forEach((sec, i) => {
    const f = Math.min(Math.max((t - prev) / (sec - prev), 0), 1);
    bars[i]?.style.setProperty('--fill', f);
    prev = sec;
  });
  $('#play').style.setProperty('--p', duration ? Math.min(t / duration, 1) : 0);
}

// ---------- Audio ----------
function loadClip() {
  const p = fetch('/api/clip')
    .then(r => { if (!r.ok) throw new Error(`clip ${r.status}`); return r.arrayBuffer(); })
    .then(data => ctx.decodeAudioData(data));
  p.catch(() => {});                        // shown when the player presses play
  return p;
}

function stop() {
  if (source) { source.onended = null; try { source.stop(); } catch {} source = null; }
  cancelAnimationFrame(raf);
  $('#play').classList.remove('is-playing');
  if (state) setProgress(0);
}

async function play() {
  if (source) return stop();                // second press stops
  if (!state || state.finished) return;
  const mine = (clip ??= loadClip());
  let buffer;
  $('#play').disabled = true;
  try {
    await ctx.resume();
    buffer = await mine;
  } catch {
    if (mine === clip) { clip = null; flash('Could not load the snippet. Press play to try again.'); }
    return;
  } finally {
    $('#play').disabled = false;
  }
  if (mine !== clip || source) return;      // guessed or skipped while it was loading

  const fade = ctx.createGain();
  fade.connect(master);
  source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(fade);
  source.onended = stop;
  duration = buffer.duration;
  const t0 = ctx.currentTime, end = t0 + duration;
  fade.gain.setValueAtTime(1, Math.max(t0, end - FADE));
  fade.gain.linearRampToValueAtTime(0, end);
  source.start();
  $('#play').classList.add('is-playing');
  const tick = () => { setProgress(ctx.currentTime - t0); raf = requestAnimationFrame(tick); };
  tick();
}

// ---------- Title search ----------
const input = $('#guess'), list = $('#suggest');
let shown = [], active = -1;

function drawSuggest() {
  list.hidden = !shown.length;
  list.innerHTML = shown.map((s, i) =>
    `<li role="option" id="opt-${i}" data-i="${i}" aria-selected="${i === active}">${esc(s.t)}</li>`).join('');
  input.setAttribute('aria-expanded', String(shown.length > 0));
  input.setAttribute('aria-activedescendant', active >= 0 ? `opt-${active}` : '');
}

function closeSuggest() { shown = []; active = -1; drawSuggest(); }

function suggest() {
  const words = input.value.split(/\s+/).map(fold).filter(Boolean);
  shown = !words.length ? [] : SONGS
    .filter(s => !tried.has(s.f) && words.every(w => s.f.includes(w)))
    .sort((a, b) => a.f.indexOf(words[0]) - b.f.indexOf(words[0]) || a.t.localeCompare(b.t))
    .slice(0, 8);
  active = shown.length ? 0 : -1;
  drawSuggest();
}

function pick(i) {
  input.value = shown[i].t;
  closeSuggest();
  input.focus();
}

// ---------- Actions ----------
async function act(path, body = {}) {
  stop();
  try { render(await api(path, body)); }
  catch { flash('That did not work. Reload the page or start a new game.'); }
}

function guess() {
  if (!input.value.trim()) return;
  const title = byFold.get(fold(input.value));
  if (!title) return flash('Pick a title from the list.');
  tried.add(fold(title));
  input.value = '';
  closeSuggest();
  return act('/api/guess', {guess: title});
}

const skip = () => act('/api/skip');

async function next() {
  tried = new Set();
  await act('/api/next');
  input.focus();
}

function shareText() {
  const s = state, n = s.stages.length;
  const hits = s.history.filter(h => h.points).length;
  const rows = s.history.map(h => grid(h, n));
  const more = rows.length - 10;            // long survival runs: only the last 10 songs
  if (more > 0) rows.splice(0, more, `… ${more} more`);
  return [
    `Songless · ${GAME.title} · ${GAME.mode}${s.day ? ' ' + s.day : ''}`,
    ...rows,
    s.mode === 'survival' ? `${s.score} points, ${hits} in a row` : `${s.score}/${s.history.length * s.points[0]} points`,
    ...(['localhost', '127.0.0.1'].includes(location.hostname) ? [] : [location.origin]),
  ].join('\n');
}

async function share() {
  const text = shareText();
  try {
    await navigator.clipboard.writeText(text);
  } catch {                                 // the clipboard API needs https or localhost: fall back to the old way
    const ta = Object.assign(document.createElement('textarea'), {value: text});
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  $('#share').textContent = 'Copied!';
  setTimeout(() => { $('#share').textContent = 'Copy result'; }, 2000);
}

// ---------- Events ----------
$('#play').onclick = play;
$('#send').onclick = guess;
$('#skip').onclick = skip;
$('#next').onclick = next;
$('#share').onclick = share;
$('#thumb').onerror = () => { $('#thumb').hidden = true; };    // no cover: just leave it out

input.addEventListener('input', suggest);
input.addEventListener('blur', () => setTimeout(closeSuggest, 150));
input.addEventListener('keydown', e => {
  if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && shown.length) {
    e.preventDefault();
    active = (active + (e.key === 'ArrowDown' ? 1 : shown.length - 1)) % shown.length;
    drawSuggest();
  } else if (e.key === 'Enter') {
    e.preventDefault();                     // first Enter takes the highlighted title, the next one sends the guess
    if (active >= 0 && fold(shown[active].t) !== fold(input.value)) pick(active); else guess();
  } else if (e.key === 'Escape') {
    closeSuggest();
  } else if (e.key === ' ' && !input.value) {
    e.preventDefault();                     // Space in the empty field plays the snippet
    play();
  }
});
list.addEventListener('mousedown', e => e.preventDefault());    // keep the focus in the field
list.addEventListener('click', e => {
  const li = e.target.closest('li');
  if (li) pick(+li.dataset.i);
});

document.addEventListener('keydown', e => {
  if (!state || state.finished || e.repeat || e.target.closest('input, button, a, select, textarea')) return;
  if (e.key === ' ') { e.preventDefault(); play(); }
  if (e.key === 'Enter' && state.done) next();
});

const volume = $('#volume');
volume.value = store.get('volume') ?? 1;
master.gain.value = Number(volume.value);
volume.oninput = () => { master.gain.value = Number(volume.value); store.set('volume', volume.value); };

$('#name').value = store.get('name') || '';
$('#save').onsubmit = async (e) => {
  e.preventDefault();
  try {
    const {id} = await api('/api/highscore', {name: $('#name').value});
    store.set('name', $('#name').value.trim());
    location.href = `${GAME.hs}&hl=${id}`;
  } catch {
    $('#save button').textContent = 'Could not save, try again';
  }
};

api('/api/state').then(render).catch(() => { location.href = '/'; });
