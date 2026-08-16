// 应用主线：把 计时器 / 角色动画 / 存档 / 工作室 串起来

import { Character } from './character.js';
import { Pomodoro, PHASES, DEFAULT_SETTINGS, formatTime } from './timer.js';
import { store } from './store.js';
import { Studio } from './studio.js';
import { unlockAudio, playFocusDone, playBreakDone, playClick } from './audio.js';

const $ = (sel) => document.querySelector(sel);

const els = {
  charHost: $('#charHost'),
  ring: $('#ringProgress'),
  time: $('#timeText'),
  phase: $('#phaseText'),
  dots: $('#roundDots'),
  tabs: $('#phaseTabs'),
  start: $('#startBtn'),
  reset: $('#resetBtn'),
  skip: $('#skipBtn'),
  stats: $('#statsLine'),
  petList: $('#petList'),
  addPet: $('#addPetBtn'),
  themeBtn: $('#themeBtn'),
  settingsBtn: $('#settingsBtn'),
  studioDialog: $('#studioDialog'),
  settingsDialog: $('#settingsDialog'),
};

const RING_LENGTH = 2 * Math.PI * 148;
els.ring.style.strokeDasharray = `${RING_LENGTH}`;

// —— 角色 ——
const active = store.active;
const character = new Character(els.charHost, {
  species: active?.species || 'cat',
  palette: active?.palette || 'orange',
  head: active?.head || null,
});

// —— 计时器 ——
const timer = new Pomodoro({ ...DEFAULT_SETTINGS, ...store.settings }, store.timerSnapshot);

let cheerUntil = 0;      // 庆祝动画的保护期，期间不被普通状态覆盖
let pausedSince = timer.running ? 0 : Date.now();

timer.on('tick', (s) => {
  renderTimer(s);
  throttledSave(s);
});

timer.on('state', (s) => {
  pausedSince = s.running ? 0 : Date.now();
  store.saveTimer(s);
  renderTimer(s);
});

timer.on('complete', ({ phase, minutes, next }) => {
  if (phase === 'focus') {
    store.recordSession(minutes);
    if (timer.settings.sound) playFocusDone();
    notify('番茄完成！', `休息一下吧，接下来是${PHASES[next].label}`);
    cheerUntil = Date.now() + 4200;
    character.setState('cheer');
  } else {
    if (timer.settings.sound) playBreakDone();
    notify('休息结束', '回来继续专注');
    cheerUntil = Date.now() + 1200;
    character.setState('idle');
  }
  renderStats();
});

function renderTimer(s) {
  els.time.textContent = formatTime(s.remaining);
  const progress = 1 - s.remaining / s.duration;
  els.ring.style.strokeDashoffset = `${RING_LENGTH * (1 - progress)}`;
  els.ring.dataset.phase = s.phase;

  els.phase.textContent = s.running
    ? { focus: '专注中', short: '小憩中', long: '长休中' }[s.phase]
    : `${PHASES[s.phase].label} · 已暂停`;
  els.start.textContent = s.running ? '暂停' : '开始';
  els.start.classList.toggle('running', s.running);

  for (const tab of els.tabs.querySelectorAll('.tab')) {
    tab.classList.toggle('on', tab.dataset.phase === s.phase);
  }

  const every = timer.settings.longEvery;
  const done = s.round % every;
  els.dots.textContent = '●'.repeat(done) + '○'.repeat(Math.max(0, every - done));

  document.title = s.running
    ? `${formatTime(s.remaining)} · ${PHASES[s.phase].label} · 番茄伙伴`
    : '番茄伙伴';

  syncCharacterState(s);
}

/** 计时状态 -> 角色动画状态 */
function syncCharacterState(s) {
  if (Date.now() < cheerUntil) return;
  if (s.running) {
    character.setState(s.phase === 'focus' ? 'focus' : 'break');
  } else if (pausedSince && Date.now() - pausedSince > 90_000) {
    character.setState('sleep');   // 长时间没动就打瞌睡
  } else {
    character.setState('idle');
  }
}
setInterval(() => syncCharacterState(timer.snapshot()), 2000);

let lastSaved = 0;
function throttledSave(s) {
  if (Date.now() - lastSaved < 5000) return;
  lastSaved = Date.now();
  store.saveTimer(s);
}

// —— 统计 ——
function renderStats() {
  const today = store.today();
  const streak = store.streak();
  const parts = [`今天 ${today.sessions} 个番茄 · ${today.minutes} 分钟`];
  if (streak > 1) parts.push(`连续 ${streak} 天`);
  els.stats.textContent = parts.join(' · ');
}

// —— 伙伴列表 ——
function renderPets() {
  const list = store.characters;
  const activeId = store.active?.id;
  if (!list.length) {
    els.petList.innerHTML = '<span class="empty-hint">还没有伙伴，上传一张照片做一个吧</span>';
    return;
  }
  els.petList.innerHTML = list
    .map(
      (c) => `
      <div class="pet ${c.id === activeId ? 'on' : ''}" data-id="${c.id}" title="${escapeHtml(c.name)}">
        <img src="${c.head}" alt="${escapeHtml(c.name)}">
        <span class="pet-name">${escapeHtml(c.name)}</span>
        <button class="pet-del" data-del="${c.id}" title="删除">✕</button>
      </div>`
    )
    .join('');
}

els.petList.addEventListener('click', (e) => {
  const del = e.target.closest('[data-del]');
  if (del) {
    const c = store.characters.find((x) => x.id === del.dataset.del);
    if (c && confirm(`删除「${c.name}」？`)) {
      store.removeCharacter(c.id);
      applyActiveCharacter();
      renderPets();
    }
    return;
  }
  const pet = e.target.closest('[data-id]');
  if (!pet) return;
  store.setActive(pet.dataset.id);
  applyActiveCharacter();
  renderPets();
  character.burst('heart', 2);
});

function applyActiveCharacter() {
  const c = store.active;
  character.setSpecies(c?.species || 'cat');
  character.setPalette(c?.palette || 'orange');
  character.setHead(c?.head || null);
  character.setState(character.state);
}

// —— 工作室 ——
const studio = new Studio(els.studioDialog, () => {
  applyActiveCharacter();
  renderPets();
});
els.addPet.addEventListener('click', () => studio.open());

// —— 控制 ——
els.start.addEventListener('click', () => {
  unlockAudio();
  if (timer.settings.sound) playClick(!timer.running);
  timer.toggle();
});
els.reset.addEventListener('click', () => timer.reset());
els.skip.addEventListener('click', () => timer.skip());
els.tabs.addEventListener('click', (e) => {
  const tab = e.target.closest('.tab');
  if (tab) timer.setPhase(tab.dataset.phase);
});

document.addEventListener('keydown', (e) => {
  if (e.target.matches('input, textarea') || document.querySelector('dialog[open]')) return;
  if (e.code === 'Space') {
    e.preventDefault();
    els.start.click();
  } else if (e.key === 'r' || e.key === 'R') {
    timer.reset();
  } else if (e.key === 'n' || e.key === 'N') {
    timer.skip();
  }
});

// —— 设置 ——
els.settingsBtn.addEventListener('click', () => {
  for (const input of els.settingsDialog.querySelectorAll('[data-s]')) {
    const key = input.dataset.s;
    if (input.type === 'checkbox') input.checked = !!timer.settings[key];
    else input.value = timer.settings[key];
  }
  els.settingsDialog.showModal();
});

els.settingsDialog.addEventListener('input', (e) => {
  const input = e.target.closest('[data-s]');
  if (!input) return;
  const key = input.dataset.s;
  const value = input.type === 'checkbox' ? input.checked : Number(input.value);
  if (input.type === 'number' && (!Number.isFinite(value) || value < 1)) return;
  timer.updateSettings({ [key]: value });
  store.saveSettings({ [key]: value });
  if (key === 'notify' && value && 'Notification' in window) Notification.requestPermission();
  renderTimer(timer.snapshot());
});

$('#clearStatsBtn').addEventListener('click', () => {
  if (!confirm('清空所有专注统计？形象不会被删除。')) return;
  store.all.stats = {};
  store.saveSettings({});
  renderStats();
});

// —— 主题 ——
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  store.setTheme(theme);
}
els.themeBtn.addEventListener('click', () => {
  const order = ['auto', 'light', 'dark'];
  const next = order[(order.indexOf(store.theme || 'auto') + 1) % order.length];
  applyTheme(next);
});
applyTheme(store.theme || 'auto');

function notify(title, body) {
  if (!timer.settings.notify || !('Notification' in window)) return;
  if (Notification.permission === 'granted') new Notification(title, { body, icon: store.active?.head });
}

function escapeHtml(s = '') {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 页面隐藏时把进度落盘，回来时按时间戳纠正
document.addEventListener('visibilitychange', () => {
  if (document.hidden) store.saveTimer(timer.snapshot());
  else renderTimer(timer.snapshot());
});

renderTimer(timer.snapshot());
renderStats();
renderPets();

// 首次访问自动打开工作室，引导做第一个形象
if (!store.characters.length) {
  setTimeout(() => studio.open(), 500);
}
