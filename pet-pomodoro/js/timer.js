// 番茄钟状态机
//
// 计时以「结束时间戳」为准，而不是累加 setInterval 的次数，
// 这样切到后台被节流、或者刷新页面，回来时间依然是对的。

export const PHASES = {
  focus: { label: '专注', key: 'focus' },
  short: { label: '小憩', key: 'short' },
  long: { label: '长休', key: 'long' },
};

export const DEFAULT_SETTINGS = {
  focus: 25,
  short: 5,
  long: 15,
  longEvery: 4,
  autoStart: true,
  sound: true,
  notify: false,
};

export class Pomodoro {
  constructor(settings = {}, snapshot = null) {
    this.settings = { ...DEFAULT_SETTINGS, ...settings };
    this.listeners = {};
    this.phase = 'focus';
    this.round = 0;              // 本轮已完成的专注个数
    this.running = false;
    this.remaining = this.duration();
    this.endAt = 0;
    if (snapshot) this.restore(snapshot);
    this.interval = setInterval(() => this.poll(), 250);
  }

  on(event, fn) {
    (this.listeners[event] ||= []).push(fn);
    return this;
  }

  emit(event, payload) {
    for (const fn of this.listeners[event] || []) fn(payload);
  }

  duration(phase = this.phase) {
    return Math.max(1, Math.round(this.settings[phase] * 60)) * 1000;
  }

  updateSettings(patch) {
    const wasIdlePhase = !this.running;
    this.settings = { ...this.settings, ...patch };
    if (wasIdlePhase) {
      this.remaining = this.duration();
      this.emit('tick', this.snapshot());
    }
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.endAt = Date.now() + this.remaining;
    this.emit('tick', this.snapshot());
    this.emit('state', this.snapshot());
  }

  pause() {
    if (!this.running) return;
    this.remaining = Math.max(0, this.endAt - Date.now());
    this.running = false;
    this.emit('tick', this.snapshot());
    this.emit('state', this.snapshot());
  }

  toggle() {
    this.running ? this.pause() : this.start();
  }

  /** 重置当前阶段 */
  reset() {
    this.running = false;
    this.remaining = this.duration();
    this.emit('tick', this.snapshot());
    this.emit('state', this.snapshot());
  }

  /** 手动跳到下一阶段，不计入统计 */
  skip() {
    this.advance(false);
  }

  /** 直接切到某个阶段 */
  setPhase(phase) {
    if (!PHASES[phase] || phase === this.phase) return;
    this.phase = phase;
    this.running = false;
    this.remaining = this.duration();
    this.emit('tick', this.snapshot());
    this.emit('state', this.snapshot());
  }

  poll() {
    if (!this.running) return;
    const left = this.endAt - Date.now();
    this.remaining = Math.max(0, left);
    if (left <= 0) this.advance(true);
    else this.emit('tick', this.snapshot());
  }

  advance(completed) {
    const finished = this.phase;
    const minutes = this.settings[finished];
    if (completed && finished === 'focus') this.round += 1;

    let next;
    if (finished === 'focus') {
      next = this.round > 0 && this.round % this.settings.longEvery === 0 ? 'long' : 'short';
    } else {
      next = 'focus';
    }

    this.phase = next;
    this.remaining = this.duration();
    this.running = this.settings.autoStart && completed;
    this.endAt = Date.now() + this.remaining;

    if (completed) this.emit('complete', { phase: finished, minutes, next, round: this.round });
    this.emit('tick', this.snapshot());
    this.emit('state', this.snapshot());
  }

  snapshot() {
    return {
      phase: this.phase,
      running: this.running,
      round: this.round,
      remaining: this.running ? Math.max(0, this.endAt - Date.now()) : this.remaining,
      duration: this.duration(),
      endAt: this.running ? this.endAt : 0,
    };
  }

  restore(s) {
    if (!s || !PHASES[s.phase]) return;
    this.phase = s.phase;
    this.round = s.round || 0;
    if (s.running && s.endAt > Date.now()) {
      this.running = true;
      this.endAt = s.endAt;
      this.remaining = s.endAt - Date.now();
    } else {
      this.running = false;
      this.remaining = Math.min(s.remaining ?? this.duration(), this.duration());
    }
  }

  destroy() {
    clearInterval(this.interval);
  }
}

export function formatTime(ms) {
  const total = Math.ceil(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
