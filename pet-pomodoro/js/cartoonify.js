// 照片 -> 卡通头像的图像处理管线（纯前端 Canvas，无需任何后端 / API key）
//
// 流程：裁切取景 -> 可选抠背景 -> 边缘保持平滑 -> 色阶量化 + 提饱和 -> Sobel 描边 -> 圆形羽化蒙版
//
// 想换成 AI 重绘时，只需要替换 stylize() 这一个函数：
// 输入一张 ImageBitmap / HTMLImageElement，输出一个带 alpha 的方形 canvas 即可，
// 其余（取景、蒙版、装配到角色身体上）都不用动。

export const STYLE_PRESETS = {
  cartoon: { label: '卡通', levels: 5, edge: 0.9, saturation: 1.35, smooth: 3, pixel: 0, ink: [38, 26, 30] },
  crayon: { label: '手绘', levels: 7, edge: 0.55, saturation: 1.1, smooth: 5, pixel: 0, ink: [70, 52, 46] },
  pixel: { label: '像素', levels: 4, edge: 0.25, saturation: 1.45, smooth: 2, pixel: 12, ink: [30, 26, 40] },
  bold: { label: '厚涂', levels: 3, edge: 1.0, saturation: 1.6, smooth: 6, pixel: 0, ink: [24, 18, 26] },
};

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);

/** 把用户选的文件读成可绘制的位图 */
export async function fileToBitmap(file) {
  if (typeof createImageBitmap === 'function') {
    try {
      return await createImageBitmap(file);
    } catch {
      /* 少数格式 createImageBitmap 会失败，回退到 <img> */
    }
  }
  const url = URL.createObjectURL(file);
  try {
    return await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error('图片读取失败'));
      img.src = url;
    });
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
}

/** 按取景参数把原图画进方形画布（cover 铺满 + 缩放 / 平移 / 旋转） */
function drawFramed(ctx, src, size, frame) {
  const { zoom = 1, offsetX = 0, offsetY = 0, rotation = 0 } = frame;
  const w = src.width || src.naturalWidth;
  const h = src.height || src.naturalHeight;
  const scale = (size / Math.min(w, h)) * zoom;

  ctx.save();
  ctx.translate(size / 2 + offsetX * size, size / 2 + offsetY * size);
  ctx.rotate((rotation * Math.PI) / 180);
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(src, (-w * scale) / 2, (-h * scale) / 2, w * scale, h * scale);
  ctx.restore();
}

/** 可分离盒式模糊，多跑几遍近似高斯；alpha 单独处理，保住抠出来的边 */
function boxBlur(data, w, h, radius, passes = 2) {
  if (radius < 1) return data;
  const tmp = new Uint8ClampedArray(data.length);
  for (let p = 0; p < passes; p++) {
    blurPass(data, tmp, w, h, radius, true);
    blurPass(tmp, data, w, h, radius, false);
  }
  return data;
}

function blurPass(src, dst, w, h, radius, horizontal) {
  const lineLen = horizontal ? w : h;
  const lines = horizontal ? h : w;
  const step = horizontal ? 4 : w * 4;
  const lineStep = horizontal ? w * 4 : 4;
  const div = radius * 2 + 1;

  for (let line = 0; line < lines; line++) {
    const base = line * lineStep;
    let r = 0, g = 0, b = 0, a = 0;
    // 预热窗口：左边界重复采样
    for (let i = -radius; i <= radius; i++) {
      const idx = base + clamp(i, 0, lineLen - 1) * step;
      r += src[idx]; g += src[idx + 1]; b += src[idx + 2]; a += src[idx + 3];
    }
    for (let i = 0; i < lineLen; i++) {
      const out = base + i * step;
      dst[out] = r / div; dst[out + 1] = g / div; dst[out + 2] = b / div; dst[out + 3] = a / div;

      const addIdx = base + clamp(i + radius + 1, 0, lineLen - 1) * step;
      const subIdx = base + clamp(i - radius, 0, lineLen - 1) * step;
      r += src[addIdx] - src[subIdx];
      g += src[addIdx + 1] - src[subIdx + 1];
      b += src[addIdx + 2] - src[subIdx + 2];
      a += src[addIdx + 3] - src[subIdx + 3];
    }
  }
}

/** 从四条边泛洪，去掉颜色接近的背景（适合纯色 / 简单背景） */
function removeBackground(data, w, h, tolerance) {
  const visited = new Uint8Array(w * h);
  const queue = [];
  const tol = tolerance * tolerance * 3;

  const seedColors = [];
  const push = (x, y) => {
    const p = y * w + x;
    if (visited[p]) return;
    visited[p] = 1;
    queue.push(p);
    seedColors.push(data[p * 4], data[p * 4 + 1], data[p * 4 + 2]);
  };
  for (let x = 0; x < w; x++) { push(x, 0); push(x, h - 1); }
  for (let y = 0; y < h; y++) { push(0, y); push(w - 1, y); }

  // 用边缘像素的均值当参考色
  let br = 0, bg = 0, bb = 0;
  for (let i = 0; i < seedColors.length; i += 3) {
    br += seedColors[i]; bg += seedColors[i + 1]; bb += seedColors[i + 2];
  }
  const n = seedColors.length / 3;
  br /= n; bg /= n; bb /= n;

  const matches = (p) => {
    const dr = data[p * 4] - br;
    const dg = data[p * 4 + 1] - bg;
    const db = data[p * 4 + 2] - bb;
    return dr * dr + dg * dg + db * db <= tol;
  };

  const out = [];
  while (queue.length) {
    const p = queue.pop();
    if (!matches(p)) continue;
    out.push(p);
    const x = p % w;
    const y = (p / w) | 0;
    if (x > 0 && !visited[p - 1]) { visited[p - 1] = 1; queue.push(p - 1); }
    if (x < w - 1 && !visited[p + 1]) { visited[p + 1] = 1; queue.push(p + 1); }
    if (y > 0 && !visited[p - w]) { visited[p - w] = 1; queue.push(p - w); }
    if (y < h - 1 && !visited[p + w]) { visited[p + w] = 1; queue.push(p + w); }
  }
  for (const p of out) data[p * 4 + 3] = 0;
}

function rgbToHsv(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let h = 0;
  if (d !== 0) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  return [h, max === 0 ? 0 : d / max, max];
}

function hsvToRgb(h, s, v) {
  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = c; g = x; }
  else if (h < 120) { r = x; g = c; }
  else if (h < 180) { g = c; b = x; }
  else if (h < 240) { g = x; b = c; }
  else if (h < 300) { r = x; b = c; }
  else { r = c; b = x; }
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

/**
 * 核心风格化：输入位图 + 参数，输出带 alpha 的方形 canvas。
 * 换 AI 重绘时替换这个函数即可。
 */
export function stylize(src, options = {}) {
  const {
    size = 384,
    frame = {},
    style = 'cartoon',
    levels,
    edge,
    saturation,
    brightness = 1,
    cutout = false,
    cutoutTolerance = 46,
    feather = 0.45,
    mask = true,
    raw = false,     // 只取景，不做风格化（AI 生成的图已经是成品，别再滤镜一遍）
  } = options;

  const preset = STYLE_PRESETS[style] || STYLE_PRESETS.cartoon;
  const nLevels = clamp(levels ?? preset.levels, 2, 12);
  const edgeAmount = clamp(edge ?? preset.edge, 0, 1.5);
  const sat = clamp(saturation ?? preset.saturation, 0, 2.5);

  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });

  // 像素风：先降分辨率画，再放大，得到硬边马赛克
  if (preset.pixel > 0) {
    const small = document.createElement('canvas');
    small.width = small.height = Math.max(24, Math.round(size / preset.pixel));
    const sctx = small.getContext('2d');
    drawFramed(sctx, src, small.width, frame);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(small, 0, 0, size, size);
    ctx.imageSmoothingEnabled = true;
  } else {
    drawFramed(ctx, src, size, frame);
  }

  const image = ctx.getImageData(0, 0, size, size);
  const data = image.data;

  if (cutout) removeBackground(data, size, size, cutoutTolerance);

  if (raw) {
    if (mask) applyCircleMask(data, size, feather);
    ctx.putImageData(image, 0, 0);
    return canvas;
  }

  // 平滑一份副本用于「填色」和「找边」，原图只用来保留细节
  const smoothed = new Uint8ClampedArray(data);
  boxBlur(smoothed, size, size, Math.max(1, Math.round(preset.smooth)), 2);

  // 亮度图，供 Sobel 使用
  const luma = new Float32Array(size * size);
  for (let i = 0, p = 0; i < data.length; i += 4, p++) {
    luma[p] = 0.299 * smoothed[i] + 0.587 * smoothed[i + 1] + 0.114 * smoothed[i + 2];
  }

  const ink = preset.ink;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const p = y * size + x;
      const i = p * 4;

      // 1. 量化 + 提饱和：得到卡通的「色块感」
      let [h, s, v] = rgbToHsv(smoothed[i], smoothed[i + 1], smoothed[i + 2]);
      v = Math.round(v * (nLevels - 1)) / (nLevels - 1);
      v = clamp(0.06 + v * 0.94 * brightness, 0, 1);
      s = clamp(s * sat, 0, 1);
      let [r, g, b] = hsvToRgb(h, s, v);

      // 2. Sobel 描边
      if (edgeAmount > 0 && x > 0 && y > 0 && x < size - 1 && y < size - 1) {
        const gx =
          -luma[p - size - 1] - 2 * luma[p - 1] - luma[p + size - 1] +
          luma[p - size + 1] + 2 * luma[p + 1] + luma[p + size + 1];
        const gy =
          -luma[p - size - 1] - 2 * luma[p - size] - luma[p - size + 1] +
          luma[p + size - 1] + 2 * luma[p + size] + luma[p + size + 1];
        const mag = Math.sqrt(gx * gx + gy * gy) / 1020; // 归一到 0..1
        const strokeAlpha = clamp((mag - 0.05) * 6 * edgeAmount, 0, 1);
        if (strokeAlpha > 0) {
          r += (ink[0] - r) * strokeAlpha;
          g += (ink[1] - g) * strokeAlpha;
          b += (ink[2] - b) * strokeAlpha;
        }
      }

      data[i] = r;
      data[i + 1] = g;
      data[i + 2] = b;
      data[i + 3] = smoothed[i + 3];
    }
  }

  // 3. 圆形羽化蒙版：头像要贴到角色的圆脑袋上
  if (mask) applyCircleMask(data, size, feather);

  ctx.putImageData(image, 0, 0);
  return canvas;
}

/** 圆形羽化：羽化越强，照片背景越是「化」进角色的头里，不会像戴了个头盔 */
function applyCircleMask(data, size, feather) {
  const c = size / 2;
  const rOut = size * 0.5;
  const rIn = rOut * (1 - 0.09 - 0.32 * clamp(feather, 0, 1));
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const i = (y * size + x) * 4;
      const d = Math.hypot(x - c, y - c);
      if (d >= rOut) data[i + 3] = 0;
      else if (d > rIn) data[i + 3] *= 1 - (d - rIn) / (rOut - rIn);
    }
  }
}

/** 导出成尽量小的 dataURL，方便存进 localStorage */
export function canvasToDataURL(canvas) {
  const webp = canvas.toDataURL('image/webp', 0.88);
  if (webp.startsWith('data:image/webp')) return webp;
  return canvas.toDataURL('image/png');
}
