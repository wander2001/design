// 形象工作室：上传照片 -> 取景 -> 风格化 -> 选身体 -> 存成伙伴

import { STYLE_PRESETS, fileToBitmap, stylize, canvasToDataURL } from './cartoonify.js';
import { Character, SPECIES, PALETTES } from './character.js';
import { store } from './store.js';

const PREVIEW_SIZE = 288;   // 调参时的预览分辨率（求快）
const EXPORT_SIZE = 384;    // 保存时的分辨率（求清晰）

export class Studio {
  constructor(dialog, onSaved) {
    this.dialog = dialog;
    this.onSaved = onSaved;
    this.source = null;
    this.editingId = null;
    this.params = defaultParams();
    this.build();
  }

  build() {
    this.dialog.innerHTML = `
      <form method="dialog" class="studio">
        <header class="studio-head">
          <h2>形象工作室</h2>
          <button class="icon-btn" value="cancel" aria-label="关闭">✕</button>
        </header>

        <div class="studio-body">
          <section class="studio-col">
            <div class="drop-zone" data-drop>
              <input type="file" accept="image/*" hidden data-file>
              <div class="drop-inner">
                <div class="drop-emoji">🐱🐶🧑</div>
                <p>把猫 / 狗 / 人的照片拖进来</p>
                <div class="row">
                  <button type="button" class="btn primary" data-pick>选择照片</button>
                  <button type="button" class="btn ghost" data-sample>没有照片？试试示例</button>
                </div>
                <p class="hint">照片只在你的浏览器里处理，不会上传到任何服务器</p>
              </div>
            </div>

            <div class="framer" data-framer hidden>
              <canvas data-preview width="${PREVIEW_SIZE}" height="${PREVIEW_SIZE}"></canvas>
              <div class="framer-hint">在圆里拖动移图，滚轮缩放</div>
            </div>

            <div class="controls-grid" data-controls hidden>
              <label>缩放<input type="range" data-p="zoom" min="0.6" max="3" step="0.02"></label>
              <label>旋转<input type="range" data-p="rotation" min="-45" max="45" step="1"></label>
              <label>色阶<input type="range" data-p="levels" min="2" max="10" step="1"></label>
              <label>描边<input type="range" data-p="edge" min="0" max="1.4" step="0.05"></label>
              <label>饱和<input type="range" data-p="saturation" min="0.4" max="2.2" step="0.05"></label>
              <label>亮度<input type="range" data-p="brightness" min="0.7" max="1.4" step="0.02"></label>
              <label>柔边<input type="range" data-p="feather" min="0" max="1" step="0.05"></label>
              <label class="check"><input type="checkbox" data-p="cutout"> 去掉背景（纯色背景效果最好）</label>
              <button type="button" class="btn ghost small" data-reset>恢复默认参数</button>
            </div>
          </section>

          <section class="studio-col">
            <div class="pet-preview" data-stage></div>

            <div class="picker">
              <span class="picker-label">画风</span>
              <div class="chips" data-styles></div>
            </div>
            <div class="picker">
              <span class="picker-label">身体</span>
              <div class="chips" data-species></div>
            </div>
            <div class="picker">
              <span class="picker-label">毛色</span>
              <div class="chips swatches" data-palettes></div>
            </div>
            <label class="name-field">名字<input type="text" data-name maxlength="12" placeholder="给它取个名字"></label>

            <div class="studio-actions">
              <button type="button" class="btn primary big" data-save disabled>保存这个伙伴</button>
            </div>
          </section>
        </div>
      </form>`;

    const $ = (sel) => this.dialog.querySelector(sel);
    this.els = {
      drop: $('[data-drop]'),
      file: $('[data-file]'),
      framer: $('[data-framer]'),
      preview: $('[data-preview]'),
      controls: $('[data-controls]'),
      stage: $('[data-stage]'),
      styles: $('[data-styles]'),
      species: $('[data-species]'),
      palettes: $('[data-palettes]'),
      name: $('[data-name]'),
      save: $('[data-save]'),
    };

    this.character = new Character(this.els.stage, { species: 'cat', palette: 'orange' });

    this.renderChips();
    this.bind();
    this.syncControls();
  }

  renderChips() {
    this.els.styles.innerHTML = Object.entries(STYLE_PRESETS)
      .map(([key, s]) => `<button type="button" class="chip" data-style="${key}">${s.label}</button>`)
      .join('');
    this.els.species.innerHTML = Object.entries(SPECIES)
      .map(([key, s]) => `<button type="button" class="chip" data-species-key="${key}">${s.label}</button>`)
      .join('');
    this.els.palettes.innerHTML = PALETTES.map(
      (p) => `<button type="button" class="chip swatch" data-palette="${p.id}" title="${p.label}"
              style="--sw:${p.main};--sw-dark:${p.dark}"></button>`
    ).join('');
  }

  bind() {
    const { drop, file } = this.els;

    this.dialog.querySelector('[data-pick]').addEventListener('click', () => file.click());
    this.dialog.querySelector('[data-sample]').addEventListener('click', () => this.loadSample());
    file.addEventListener('change', () => {
      if (file.files?.[0]) this.loadFile(file.files[0]);
    });

    ['dragenter', 'dragover'].forEach((e) =>
      drop.addEventListener(e, (ev) => {
        ev.preventDefault();
        drop.classList.add('over');
      })
    );
    ['dragleave', 'drop'].forEach((e) =>
      drop.addEventListener(e, (ev) => {
        ev.preventDefault();
        drop.classList.remove('over');
      })
    );
    drop.addEventListener('drop', (ev) => {
      const f = ev.dataTransfer?.files?.[0];
      if (f) this.loadFile(f);
    });

    // 参数滑块
    for (const input of this.dialog.querySelectorAll('[data-p]')) {
      input.addEventListener('input', () => {
        const key = input.dataset.p;
        this.params[key] = input.type === 'checkbox' ? input.checked : Number(input.value);
        this.refresh();
      });
    }
    this.dialog.querySelector('[data-reset]').addEventListener('click', () => {
      const { style } = this.params;
      this.params = { ...defaultParams(), style };
      this.applyStylePreset(style);
      this.syncControls();
      this.refresh();
    });

    // 圆内拖动取景
    const canvas = this.els.preview;
    let dragging = false;
    let last = null;
    canvas.addEventListener('pointerdown', (e) => {
      dragging = true;
      last = { x: e.clientX, y: e.clientY };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const rect = canvas.getBoundingClientRect();
      this.params.offsetX += (e.clientX - last.x) / rect.width;
      this.params.offsetY += (e.clientY - last.y) / rect.height;
      last = { x: e.clientX, y: e.clientY };
      this.refresh();
    });
    const stopDrag = () => (dragging = false);
    canvas.addEventListener('pointerup', stopDrag);
    canvas.addEventListener('pointercancel', stopDrag);
    canvas.addEventListener(
      'wheel',
      (e) => {
        e.preventDefault();
        this.params.zoom = clamp(this.params.zoom * (e.deltaY > 0 ? 0.94 : 1.06), 0.6, 3);
        this.syncControls();
        this.refresh();
      },
      { passive: false }
    );

    this.els.styles.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-style]');
      if (!btn) return;
      this.params.style = btn.dataset.style;
      this.applyStylePreset(this.params.style);
      this.syncControls();
      this.refresh();
    });
    this.els.species.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-species-key]');
      if (!btn) return;
      this.params.species = btn.dataset.speciesKey;
      this.character.setSpecies(this.params.species);
      this.syncControls();
    });
    this.els.palettes.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-palette]');
      if (!btn) return;
      this.params.palette = btn.dataset.palette;
      this.character.setPalette(this.params.palette);
      this.character.setHead(this.headUrl);
      this.syncControls();
    });

    this.els.save.addEventListener('click', () => this.save());
  }

  applyStylePreset(style) {
    const p = STYLE_PRESETS[style];
    if (!p) return;
    this.params.levels = p.levels;
    this.params.edge = p.edge;
    this.params.saturation = p.saturation;
  }

  syncControls() {
    for (const input of this.dialog.querySelectorAll('[data-p]')) {
      const key = input.dataset.p;
      if (input.type === 'checkbox') input.checked = !!this.params[key];
      else input.value = this.params[key];
    }
    for (const btn of this.dialog.querySelectorAll('[data-style]'))
      btn.classList.toggle('on', btn.dataset.style === this.params.style);
    for (const btn of this.dialog.querySelectorAll('[data-species-key]'))
      btn.classList.toggle('on', btn.dataset.speciesKey === this.params.species);
    for (const btn of this.dialog.querySelectorAll('[data-palette]'))
      btn.classList.toggle('on', btn.dataset.palette === this.params.palette);
  }

  async loadFile(file) {
    if (!file.type.startsWith('image/')) {
      alert('请选择图片文件');
      return;
    }
    try {
      this.source = await fileToBitmap(file);
      this.afterSourceLoaded();
    } catch (err) {
      console.error(err);
      alert('这张图片读不了，换一张试试');
    }
  }

  loadSample() {
    this.source = drawSamplePhoto();
    this.afterSourceLoaded();
  }

  afterSourceLoaded() {
    this.els.drop.hidden = true;
    this.els.framer.hidden = false;
    this.els.controls.hidden = false;
    this.els.save.disabled = false;
    this.params.offsetX = 0;
    this.params.offsetY = 0;
    this.refresh();
  }

  /** 参数变了就重算头像（合并到一帧里，拖动时不会卡） */
  refresh() {
    if (!this.source || this._pending) return;
    this._pending = true;
    requestAnimationFrame(() => {
      this._pending = false;
      const canvas = stylize(this.source, {
        size: PREVIEW_SIZE,
        frame: this.params,
        style: this.params.style,
        levels: this.params.levels,
        edge: this.params.edge,
        saturation: this.params.saturation,
        brightness: this.params.brightness,
        cutout: this.params.cutout,
        feather: this.params.feather,
      });
      const ctx = this.els.preview.getContext('2d');
      ctx.clearRect(0, 0, PREVIEW_SIZE, PREVIEW_SIZE);
      ctx.drawImage(canvas, 0, 0);
      this.headUrl = canvasToDataURL(canvas);
      this.character.setHead(this.headUrl);
    });
  }

  /** 打开工作室；传入已有伙伴则进入编辑模式 */
  open(character = null) {
    this.editingId = character?.id || null;
    if (character) {
      this.params = { ...defaultParams(), ...(character.source || {}), species: character.species, palette: character.palette };
      this.headUrl = character.head;
      this.els.name.value = character.name || '';
      this.character.setSpecies(character.species);
      this.character.setPalette(character.palette);
      this.character.setHead(character.head);
      // 原图没留存，只能在已有头像上继续调身体 / 名字
      this.els.drop.hidden = true;
      this.els.framer.hidden = true;
      this.els.controls.hidden = true;
      this.els.save.disabled = false;
    } else {
      this.params = defaultParams();
      this.source = null;
      this.headUrl = null;
      this.els.name.value = '';
      this.els.drop.hidden = false;
      this.els.framer.hidden = true;
      this.els.controls.hidden = true;
      this.els.save.disabled = true;
      this.character.setHead(null);
      this.character.setSpecies(this.params.species);
      this.character.setPalette(this.params.palette);
    }
    this.syncControls();
    this.character.setState('idle');
    if (!this.dialog.open) this.dialog.showModal();
  }

  save() {
    // 导出用更高分辨率重跑一次
    let head = this.headUrl;
    if (this.source) {
      const canvas = stylize(this.source, {
        size: EXPORT_SIZE,
        frame: this.params,
        style: this.params.style,
        levels: this.params.levels,
        edge: this.params.edge,
        saturation: this.params.saturation,
        brightness: this.params.brightness,
        cutout: this.params.cutout,
        feather: this.params.feather,
      });
      head = canvasToDataURL(canvas);
    }
    if (!head) return;

    const name = this.els.name.value.trim() || defaultName(this.params.species);
    const payload = {
      name,
      species: this.params.species,
      palette: this.params.palette,
      head,
      source: { ...this.params },
    };

    const record = this.editingId
      ? store.updateCharacter(this.editingId, payload)
      : store.addCharacter(payload);

    this.character.setState('cheer');
    setTimeout(() => {
      this.dialog.close();
      this.onSaved?.(record);
    }, 700);
  }
}

function defaultParams() {
  return {
    zoom: 1.1,
    rotation: 0,
    offsetX: 0,
    offsetY: 0,
    style: 'cartoon',
    levels: STYLE_PRESETS.cartoon.levels,
    edge: STYLE_PRESETS.cartoon.edge,
    saturation: STYLE_PRESETS.cartoon.saturation,
    brightness: 1,
    feather: 0.45,
    cutout: false,
    species: 'cat',
    palette: 'orange',
  };
}

function defaultName(species) {
  return { cat: '小猫', dog: '小狗', human: '小人' }[species] || '小伙伴';
}

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

/** 没照片时的示例「照片」：程序画一只橘猫脸，方便直接体验流程 */
function drawSamplePhoto() {
  const size = 420;
  const c = document.createElement('canvas');
  c.width = c.height = size;
  const ctx = c.getContext('2d');

  const bg = ctx.createLinearGradient(0, 0, size, size);
  bg.addColorStop(0, '#cfe6f5');
  bg.addColorStop(1, '#a8cbe4');
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, size, size);

  ctx.fillStyle = '#e79a4e';
  ctx.beginPath();
  ctx.moveTo(120, 150); ctx.lineTo(96, 70); ctx.lineTo(176, 118); ctx.closePath(); ctx.fill();
  ctx.beginPath();
  ctx.moveTo(300, 150); ctx.lineTo(324, 70); ctx.lineTo(244, 118); ctx.closePath(); ctx.fill();

  ctx.beginPath();
  ctx.ellipse(210, 220, 118, 106, 0, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#f6d7ae';
  ctx.beginPath();
  ctx.ellipse(210, 250, 74, 62, 0, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = '#d98436';
  for (let i = 0; i < 4; i++) {
    ctx.fillRect(170 + i * 22, 118, 9, 34);
  }

  ctx.fillStyle = '#2f2a33';
  ctx.beginPath(); ctx.ellipse(172, 214, 17, 21, 0, 0, Math.PI * 2); ctx.fill();
  ctx.beginPath(); ctx.ellipse(248, 214, 17, 21, 0, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#fff';
  ctx.beginPath(); ctx.ellipse(178, 207, 6, 7, 0, 0, Math.PI * 2); ctx.fill();
  ctx.beginPath(); ctx.ellipse(254, 207, 6, 7, 0, 0, Math.PI * 2); ctx.fill();

  ctx.fillStyle = '#e0757f';
  ctx.beginPath();
  ctx.moveTo(210, 252); ctx.lineTo(198, 240); ctx.lineTo(222, 240); ctx.closePath(); ctx.fill();

  ctx.strokeStyle = '#3a3038';
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.moveTo(210, 254); ctx.quadraticCurveTo(196, 272, 182, 258);
  ctx.moveTo(210, 254); ctx.quadraticCurveTo(224, 272, 238, 258);
  ctx.stroke();

  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(60,50,55,.6)';
  for (const dir of [-1, 1]) {
    for (let i = 0; i < 3; i++) {
      ctx.beginPath();
      ctx.moveTo(210 + dir * 40, 250 + i * 10);
      ctx.lineTo(210 + dir * 140, 232 + i * 22);
      ctx.stroke();
    }
  }
  return c;
}
