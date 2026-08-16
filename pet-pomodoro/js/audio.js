// 用 WebAudio 现场合成提示音，不用外部音频文件。
// 浏览器要求先有用户手势才能出声，所以 AudioContext 是懒创建的。

let ctx = null;

function context() {
  if (!ctx) {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    ctx = new Ctor();
  }
  if (ctx.state === 'suspended') ctx.resume();
  return ctx;
}

export function unlockAudio() {
  context();
}

function tone(freq, start, duration, gain = 0.18, type = 'sine') {
  const ac = context();
  if (!ac) return;
  const osc = ac.createOscillator();
  const amp = ac.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(freq, ac.currentTime + start);
  amp.gain.setValueAtTime(0, ac.currentTime + start);
  amp.gain.linearRampToValueAtTime(gain, ac.currentTime + start + 0.02);
  amp.gain.exponentialRampToValueAtTime(0.0001, ac.currentTime + start + duration);
  osc.connect(amp).connect(ac.destination);
  osc.start(ac.currentTime + start);
  osc.stop(ac.currentTime + start + duration + 0.05);
}

/** 专注结束：上行的小铃铛 */
export function playFocusDone() {
  [523.25, 659.25, 783.99, 1046.5].forEach((f, i) => tone(f, i * 0.11, 0.65, 0.16));
}

/** 休息结束：柔和的两声 */
export function playBreakDone() {
  tone(659.25, 0, 0.5, 0.14);
  tone(523.25, 0.16, 0.6, 0.13);
}

/** 开始 / 暂停的轻点击 */
export function playClick(up = true) {
  tone(up ? 880 : 620, 0, 0.12, 0.09, 'triangle');
}
