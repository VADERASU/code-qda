// place.js — keeping your place across the coding ⇄ verification switch.
//
// The two views are two renderings of the same interview, and both scroll
// inside #main, so switching between them should not move you. They agree on
// one coordinate — a character offset into the .md — and on one line of the
// screen to put it on: the top of the scroll area, below anything pinned over
// it. Coding view offsets come from the text nodes of #text-display
// (render.js); verification offsets come from each segment's `span`, the same
// offsets the server rendered the .md from (verify.js).

const main = document.getElementById('main');

// A little air, so the anchored line is not flush against the edge.
const PAD = 8;

// How far above the reading line something must have gone before it counts as
// past it. Rows and lines meet at fractional pixels, so an exact comparison
// picks the row above — the one with a hair of itself still showing, which is
// not anywhere anyone is reading.
export const PAST = 3;

// The y a reader reads from. `pinned` is an element floating over the scroll
// area — the file bar, the playback bar — which pushes it down past itself.
export function readingLine(pinned) {
  const top = main.getBoundingClientRect().top;
  const bar = pinned ? pinned.getBoundingClientRect().bottom : 0;
  return Math.max(top, bar) + PAD;
}

// Reset the pane when there is no coding to resume, or before an explicit
// navigation target. The pane otherwise keeps its previous scroll offset.
export function toTop() {
  main.scrollTop = 0;
}

let layer = null;

export function clearPlace() {
  layer?.replaceChildren();
}

// Landing marks use measured pixel positions; discard them when the pane
// reflows rather than leaving a fading mark over different words or outside it.
let paneWidth = main.clientWidth;
new ResizeObserver(() => {
  if (main.clientWidth !== paneWidth) {
    paneWidth = main.clientWidth;
    clearPlace();
  }
}).observe(main);
window.addEventListener('resize', clearPlace);

// A wash over what you were just looking at in the other view, for as long as
// it takes the eye to find it. Landing on the right line is not enough on its
// own: the same passage is a paragraph of prose on one side and a stack of
// timed rows on the other, and nothing about the two says they are the same
// words. The mark says it.
export function markPlace(rects) {
  if (!layer) {
    layer = document.createElement('div');
    layer.id = 'place-layer';
    main.appendChild(layer);
  }
  layer.replaceChildren();          // one landing at a time, never a pile
  const box = main.getBoundingClientRect();
  for (const rect of rects) {
    const mark = document.createElement('div');
    mark.className = 'place-mark';
    // Into the scroll content's own coordinates, so the mark stays on its
    // words if the page is scrolled while it fades.
    mark.style.top = `${rect.top - box.top + main.scrollTop}px`;
    mark.style.left = `${rect.left - box.left + main.scrollLeft}px`;
    mark.style.width = `${rect.right - rect.left}px`;
    mark.style.height = `${rect.bottom - rect.top}px`;
    mark.addEventListener('animationend', () => mark.remove());
    // A mark whose animation never ends — the pane was hidden mid-fade, say —
    // must not sit on the text for the rest of the session.
    setTimeout(() => mark.remove(), 4000);
    layer.appendChild(mark);
  }
}

// Scroll so the viewport y `anchorTop` lands on `lineY`. When that would leave
// only part of whatever sits above `contentEl` — a file bar, the hint text —
// showing, go to the top instead: at the start of a transcript "the same
// place" is the beginning, not a line with its preamble sliced off.
export function placeAt(anchorTop, lineY, contentEl) {
  const target = main.scrollTop + anchorTop - lineY;
  const contentTop =
    main.scrollTop + contentEl.getBoundingClientRect().top - main.getBoundingClientRect().top;
  main.scrollTop = target <= contentTop ? 0 : target;
}
