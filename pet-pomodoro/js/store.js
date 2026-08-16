// 本地存档：形象、设置、计时快照、每日统计
// 全部放 localStorage —— 头像用 webp 压到 384px，一个大约 30~60KB，够存十几只。

const KEY = 'pet-pomodoro:v1';

const EMPTY = {
  characters: [],   // { id, name, species, palette, head(dataURL), createdAt, source }
  activeId: null,
  settings: {},
  timer: null,
  stats: {},        // { 'YYYY-MM-DD': { sessions, minutes } }
  theme: 'auto',
};

let cache = null;

function read() {
  if (cache) return cache;
  try {
    cache = { ...EMPTY, ...JSON.parse(localStorage.getItem(KEY) || '{}') };
  } catch {
    cache = { ...EMPTY };
  }
  return cache;
}

function write() {
  try {
    localStorage.setItem(KEY, JSON.stringify(cache));
    return true;
  } catch (err) {
    console.warn('存档失败（可能是空间不足）', err);
    return false;
  }
}

export const store = {
  get all() {
    return read();
  },

  get settings() {
    return read().settings;
  },
  saveSettings(patch) {
    const s = read();
    s.settings = { ...s.settings, ...patch };
    write();
  },

  get theme() {
    return read().theme;
  },
  setTheme(theme) {
    read().theme = theme;
    write();
  },

  get characters() {
    return read().characters;
  },
  get active() {
    const s = read();
    return s.characters.find((c) => c.id === s.activeId) || s.characters[0] || null;
  },
  setActive(id) {
    read().activeId = id;
    write();
  },
  addCharacter(character) {
    const s = read();
    const record = { id: `p${Date.now().toString(36)}`, createdAt: Date.now(), ...character };
    s.characters.unshift(record);
    s.activeId = record.id;
    if (!write()) {
      // 存不下就把最老的挤掉再试一次
      while (s.characters.length > 1 && !write()) s.characters.pop();
    }
    return record;
  },
  updateCharacter(id, patch) {
    const s = read();
    const c = s.characters.find((x) => x.id === id);
    if (!c) return null;
    Object.assign(c, patch);
    write();
    return c;
  },
  removeCharacter(id) {
    const s = read();
    s.characters = s.characters.filter((c) => c.id !== id);
    if (s.activeId === id) s.activeId = s.characters[0]?.id || null;
    write();
  },

  get timerSnapshot() {
    return read().timer;
  },
  saveTimer(snapshot) {
    read().timer = snapshot;
    write();
  },

  get stats() {
    return read().stats;
  },
  recordSession(minutes) {
    const s = read();
    const day = todayKey();
    const entry = (s.stats[day] ||= { sessions: 0, minutes: 0 });
    entry.sessions += 1;
    entry.minutes += minutes;
    // 只留最近 60 天
    const keys = Object.keys(s.stats).sort();
    while (keys.length > 60) delete s.stats[keys.shift()];
    write();
    return entry;
  },
  today() {
    return read().stats[todayKey()] || { sessions: 0, minutes: 0 };
  },
  streak() {
    const s = read().stats;
    let n = 0;
    const d = new Date();
    for (;;) {
      const key = dayKey(d);
      if (!s[key] || s[key].sessions === 0) {
        // 今天还没开始不算断签
        if (n === 0 && key === todayKey()) {
          d.setDate(d.getDate() - 1);
          continue;
        }
        break;
      }
      n += 1;
      d.setDate(d.getDate() - 1);
    }
    return n;
  },
};

function dayKey(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function todayKey() {
  return dayKey(new Date());
}
