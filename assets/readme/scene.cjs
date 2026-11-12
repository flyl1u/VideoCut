// README banner for VideoCut. Build with the readme-banner skill:
//   node <readme-banner>/scripts/build.cjs assets/readme/scene.cjs --out assets/readme/build/hero-v1
// then copy build/hero-v1/hero.svg to assets/readme/hero.svg.
module.exports = ({draw: d}) => {
  const ink = '#343932', muted = '#6E7065';

  // Left card: the raw transcript, with the filler and the false start struck out.
  const L = {x: 640, y: 168, w: 330, h: 334};
  const rowY = [262, 318, 374, 430];
  const strike = (x1, x2, y) => d.path(`M${x1} ${y} Q${(x1 + x2) / 2} ${y - 3} ${x2} ${y + 1}`, {color: '#BA533B', width: 3.2});
  const leftArt =
    d.box({x: L.x + 10, y: L.y + 10, w: L.w, h: L.h, fill: '#E7ECDD', rotate: -3, seed: 5})
    + d.box({x: L.x, y: L.y, w: L.w, h: L.h, fill: '#FFFDF7', rotate: -1.5, seed: 6})
    + d.path(`M${L.x + 22} ${L.y + 62} Q${L.x + 165} ${L.y + 65} ${L.x + 308} ${L.y + 61}`, {color: '#B8B5A7', width: 1.7})
    + `<g transform="rotate(-1.5 ${L.x + L.w / 2} ${L.y + L.h / 2})">`
    + `<rect x="${L.x + 18}" y="${rowY[1] - 4}" width="118" height="44" rx="10" fill="#F6DFD2" opacity=".9"/>`
    + `<rect x="${L.x + 18}" y="${rowY[2] - 4}" width="178" height="44" rx="10" fill="#F6DFD2" opacity=".9"/>`
    + strike(L.x + 30, L.x + 120, rowY[1] + 20)
    + strike(L.x + 30, L.x + 182, rowY[2] + 20)
    + '</g>';

  // Center: the skill itself, a yellow tile with hand-drawn scissors.
  const C = {x: 1040, y: 246, w: 176, h: 170};
  const sx = C.x + C.w / 2, sy = C.y + 66;
  const scissors =
    d.path(`M${sx - 10} ${sy + 12} L${sx + 24} ${sy - 36}`, {width: 4})
    + d.path(`M${sx + 10} ${sy + 12} L${sx - 24} ${sy - 36}`, {width: 4})
    + `<circle cx="${sx - 17}" cy="${sy + 24}" r="12" fill="#FFFDF7" stroke="${ink}" stroke-width="3.6"/>`
    + `<circle cx="${sx + 17}" cy="${sy + 24}" r="12" fill="#FFFDF7" stroke="${ink}" stroke-width="3.6"/>`
    + `<circle cx="${sx}" cy="${sy - 2}" r="2.6" fill="${ink}"/>`;
  const centerArt =
    d.box({...C, fill: '#F4D786', rotate: -2, seed: 11}) + scissors
    + d.path(`M${C.x + 150} ${C.y - 22} l8 -13 M${C.x + 172} ${C.y - 10} l16 -4 M${C.x + 130} ${C.y - 30} l-1 -15`, {color: '#BD9744', width: 2.7});

  // Right card: the result back in FocuSee: a frame with a subtitle and a joined cut list.
  const R = {x: 1290, y: 168, w: 330, h: 334};
  const scr = {x: R.x + 22, y: R.y + 78, w: 286, h: 150};
  const segs = [[0, 64], [64, 132], [132, 214], [214, 286]];
  const rightArt =
    d.box({x: R.x - 8, y: R.y + 10, w: R.w, h: R.h, fill: '#DDEDE2', rotate: 3, seed: 3})
    + d.box({x: R.x, y: R.y, w: R.w, h: R.h, fill: '#FFFDF7', rotate: 1, seed: 4})
    + d.path(`M${R.x + 22} ${R.y + 62} Q${R.x + 165} ${R.y + 65} ${R.x + 308} ${R.y + 61}`, {color: '#B8B5A7', width: 1.7})
    + `<rect x="${scr.x}" y="${scr.y}" width="${scr.w}" height="${scr.h}" rx="12" fill="#DFEAFA" stroke="${ink}" stroke-width="2.6"/>`
    + d.path(`M${scr.x + 22} ${scr.y + 30} H${scr.x + 150} M${scr.x + 22} ${scr.y + 52} H${scr.x + 118}`, {color: '#8FA3BF', width: 5})
    + `<circle cx="${scr.x + scr.w - 40}" cy="${scr.y + 40}" r="24" fill="#F6DFD2" stroke="${ink}" stroke-width="2.6"/>`
    + d.path(`M${scr.x + scr.w - 49} ${scr.y + 45} Q${scr.x + scr.w - 40} ${scr.y + 53} ${scr.x + scr.w - 31} ${scr.y + 45}`, {width: 2.4})
    + `<circle cx="${scr.x + scr.w - 48}" cy="${scr.y + 34}" r="2.4" fill="${ink}"/><circle cx="${scr.x + scr.w - 32}" cy="${scr.y + 34}" r="2.4" fill="${ink}"/>`
    + `<rect x="${scr.x + 46}" y="${scr.y + 98}" width="194" height="36" rx="9" fill="#343932" opacity=".88"/>`
    + segs.map(([a, b], i) => `<rect x="${scr.x + a + 1}" y="${R.y + 262}" width="${b - a - 3}" height="26" rx="6" fill="${['#F4D786', '#C9DFC9', '#CDDDF1', '#E8E2EF'][i]}" stroke="${ink}" stroke-width="2"/>`).join('');

  return {
    id: 'hero',
    title: 'VideoCut - rough-cut FocuSee screen recordings by transcript',
    description: 'A raw transcript with a struck-out "um" and a false start goes through the VideoCut skill and comes back as a FocuSee project with a joined cut list and subtitles. Schematic illustration, not a screenshot.',
    width: 1680, height: 640,
    art: d.path('M65 301 Q250 312 470 298', {color: '#F4D786', width: 17, opacity: .7})
      + d.path('M587 146 Q583 309 588 484', {color: '#C9C5B6', width: 1.4, opacity: .65})
      + leftArt
      + d.arrow({from: [990, 331], to: [1018, 331], width: 3.1, head: 15})
      + centerArt
      + d.arrow({from: [1234, 331], to: [1270, 331], width: 3.3, head: 16})
      + rightArt,
    labels: [
      {text: 'Agent Skill for FocuSee', x: 66, y: 171, w: 470, h: 36, size: 23, color: muted},
      {text: 'VideoCut', x: 60, y: 192, w: 505, h: 148, size: 98, bold: true, letterSpacing: -3},
      {text: 'Rough cuts, from the transcript.', x: 66, y: 335, w: 500, h: 57, size: 29},
      {text: 'Pauses, fillers and retakes out. Subtitles in.', x: 68, y: 408, w: 500, h: 42, size: 20, color: muted},

      {text: 'transcript', x: L.x + 26, y: L.y + 16, w: 200, h: 36, size: 22, letteringStyle: 'body', color: muted, rotate: -1.5},
      {text: 'Hi everyone.', x: L.x + 30, y: rowY[0], w: 280, h: 40, size: 25, letteringStyle: 'body', rotate: -1.5},
      {text: 'um...', x: L.x + 30, y: rowY[1], w: 120, h: 40, size: 25, letteringStyle: 'body', rotate: -1.5},
      {text: "Let's bu-", x: L.x + 30, y: rowY[2], w: 170, h: 40, size: 25, letteringStyle: 'body', rotate: -1.5},
      {text: 'retake', x: L.x + 212, y: rowY[2] + 2, w: 96, h: 36, size: 20, letteringStyle: 'body', color: '#BA533B', rotate: -1.5},
      {text: "Let's build a page.", x: L.x + 30, y: rowY[3], w: 280, h: 40, size: 25, letteringStyle: 'body', rotate: -1.5},
      {text: 'Raw take', x: L.x + 15, y: 522, w: 300, h: 46, size: 28, letteringStyle: 'body', align: 'center'},

      {text: 'VideoCut', x: C.x + 4, y: C.y + 112, w: C.w - 8, h: 42, size: 28, bold: true, align: 'center', rotate: -2},

      {text: 'project.focusee', x: R.x + 26, y: R.y + 16, w: 240, h: 36, size: 22, letteringStyle: 'body', color: muted, rotate: 1},
      {text: "Let's build a page.", x: scr.x + 50, y: scr.y + 98, w: 186, h: 36, size: 19, letteringStyle: 'body', color: '#FFFDF7', align: 'center', rotate: 1},
      {text: 'Ready to polish', x: R.x + 15, y: 522, w: 300, h: 46, size: 28, letteringStyle: 'body', align: 'center'},
    ],
    sources: [
      'Original schematic for the VideoCut README; it is an illustration of the workflow, not a FocuSee screenshot.',
      'FocuSee is named as text only; no FocuSee logo or other brand mark is drawn.',
      'Scissors, video frame and cut-list drawings are original SVG geometry; paper, boxes, arrows and lettering come from the readme-banner skill (OFL Comic Relief and Kalam outlines).',
    ],
  };
};
