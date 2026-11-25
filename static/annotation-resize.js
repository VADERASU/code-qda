import {state} from './state.js';
import {lineRects} from './text-geometry.js';

// Keep every control and preview outside the transcript: DOM text offsets
// must continue to match the saved Markdown exactly, even across overlaps.
export function createAnnotationResizer({textDisplay, main, fileBar, codeLabel, onSave}) {
  const layer = document.createElement('div');
  layer.id = 'annotation-resize';
  layer.hidden = true;
  const preview = document.createElement('div');
  preview.className = 'annotation-resize-preview';
  const boxes = document.createElement('div');
  preview.appendChild(boxes);
  const bar = document.createElement('div');
  bar.className = 'annotation-resize-bar';
  bar.setAttribute('role', 'group');
  bar.setAttribute('aria-label', 'Resize coded span');
  const title = document.createElement('strong');
  const hint = document.createElement('span');
  hint.textContent = 'Drag either handle or select the new span. Enter to save · Esc to cancel.';
  const save = document.createElement('button');
  save.type = 'button';
  save.textContent = 'Save span';
  save.setAttribute('aria-keyshortcuts', 'Enter');
  save.className = 'annotation-resize-save';
  const cancelButton = document.createElement('button');
  cancelButton.type = 'button';
  cancelButton.textContent = 'Cancel';
  bar.append(title, hint, save, cancelButton);
  layer.append(preview, bar);
  document.body.appendChild(layer);

  let editing = null;
  let drag = null;
  let scrollFrame = null;
  const handles = {};

  function boundaryAt(offset) {
    const walker = document.createTreeWalker(textDisplay, NodeFilter.SHOW_TEXT);
    let node;
    let remaining = offset;
    while ((node = walker.nextNode())) {
      if (remaining <= node.length) return {node, offset: remaining};
      remaining -= node.length;
    }
    return null;
  }

  function rangeFor(start, end) {
    const from = boundaryAt(start);
    const to = boundaryAt(end);
    if (!from || !to) return null;
    const range = document.createRange();
    range.setStart(from.node, from.offset);
    range.setEnd(to.node, to.offset);
    return range;
  }

  function offsetAt(node, offset) {
    if (!node || !textDisplay.contains(node)) return null;
    const range = document.createRange();
    range.selectNodeContents(textDisplay);
    range.setEnd(node, offset);
    return range.toString().length;
  }

  function validContext() {
    return editing && editing.file === state.currentFile && editing.rawText === state.rawText
      && state.annotations.some(a => a.id === editing.id
        && a.start === editing.originalStart && a.end === editing.originalEnd);
  }

  function draw() {
    if (!editing) return;
    if (!validContext() || textDisplay.hidden) {
      cancel();
      return;
    }
    const bounds = main.getBoundingClientRect();
    const top = fileBar.hidden ? bounds.top : Math.max(bounds.top, fileBar.getBoundingClientRect().bottom);
    preview.style.clipPath = `inset(${top}px ${Math.max(0, innerWidth - bounds.right)}px ${Math.max(0, innerHeight - bounds.bottom)}px ${bounds.left}px)`;
    bar.style.maxWidth = `${Math.max(0, bounds.width - 24)}px`;
    bar.style.right = `${Math.max(12, innerWidth - bounds.right + 12)}px`;
    const range = rangeFor(editing.start, editing.end);
    boxes.replaceChildren();
    for (const rect of lineRects(range)) {
      const box = document.createElement('div');
      box.className = 'annotation-resize-box';
      Object.assign(box.style, {left: `${rect.left}px`, top: `${rect.top}px`,
        width: `${rect.width}px`, height: `${rect.height}px`});
      boxes.appendChild(box);
    }
    // Use a single character at each endpoint, not the hovered overlap fragment.
    const first = rangeFor(editing.start, Math.min(editing.start + 1, editing.end)).getClientRects()[0];
    const lastRects = rangeFor(Math.max(editing.start, editing.end - 1), editing.end).getClientRects();
    const last = lastRects[lastRects.length - 1];
    for (const [edge, rect] of [['start', first], ['end', last]]) {
      const handle = handles[edge];
      handle.hidden = !rect;
      if (rect) Object.assign(handle.style, {
        left: `${(edge === 'start' ? rect.left : rect.right) - 6}px`, top: `${rect.top}px`,
        height: `${Math.max(18, rect.height)}px`
      });
      handle.setAttribute('aria-valuenow', String(editing[edge]));
      handle.setAttribute('aria-valuetext', `${edge === 'start' ? 'Start' : 'End'} of ${state.rawText.slice(editing.start, editing.end).trim().slice(0, 100)}`);
      handle.setAttribute('aria-valuemin', String(edge === 'start' ? 0 : editing.start + 1));
      handle.setAttribute('aria-valuemax', String(edge === 'start' ? editing.end - 1 : state.rawText.length));
    }
    save.disabled = !state.rawText.slice(editing.start, editing.end).trim()
      || (editing.start === editing.originalStart && editing.end === editing.originalEnd);
  }

  function setBoundary(edge, offset) {
    if (!editing) return;
    editing[edge] = edge === 'start' ? Math.max(0, Math.min(offset, editing.end - 1))
      : Math.max(editing.start + 1, Math.min(offset, state.rawText.length));
    draw();
  }

  function updateFromPoint(x, y) {
    if (!drag || !editing) return;
    // Ignore overlay hit targets when asking the browser for a text caret.
    layer.style.visibility = 'hidden';
    let node, offset;
    try {
      if (document.caretPositionFromPoint) {
        const position = document.caretPositionFromPoint(x, y);
        node = position?.offsetNode;
        offset = position?.offset;
      } else {
        const range = document.caretRangeFromPoint(x, y);
        node = range?.startContainer;
        offset = range?.startOffset;
      }
    } finally {
      layer.style.visibility = '';
    }
    const at = offsetAt(node, offset);
    if (at !== null) setBoundary(drag.edge, at);
  }

  function autoScroll() {
    scrollFrame = null;
    if (!drag || !editing) return;
    const bounds = main.getBoundingClientRect();
    const top = fileBar.hidden ? bounds.top : fileBar.getBoundingClientRect().bottom;
    const step = drag.y < top + 30 ? -12 : drag.y > bounds.bottom - 35 ? 12 : 0;
    if (step && drag.x >= bounds.left && drag.x <= bounds.right) {
      main.scrollTop += step;
      updateFromPoint(drag.x, Math.max(top + 2, Math.min(drag.y, bounds.bottom - 2)));
    }
    scrollFrame = requestAnimationFrame(autoScroll);
  }

  function finishDrag(revert = false) {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (scrollFrame !== null) cancelAnimationFrame(scrollFrame);
    scrollFrame = null;
    const handle = handles[previous.edge];
    if (handle.hasPointerCapture(previous.pointerId)) handle.releasePointerCapture(previous.pointerId);
    if (revert && editing) {
      editing.start = previous.start;
      editing.end = previous.end;
      draw();
    }
  }

  for (const edge of ['start', 'end']) {
    const handle = document.createElement('button');
    handle.type = 'button';
    handle.className = `annotation-resize-handle annotation-resize-${edge}`;
    handle.setAttribute('role', 'slider');
    handle.setAttribute('aria-label', `${edge === 'start' ? 'Start' : 'End'} of coded span`);
    handle.setAttribute('aria-orientation', 'horizontal');
    handle.title = `Drag to resize ${edge}; use Left/Right arrow keys when focused`;
    handle.addEventListener('pointerdown', event => {
      if (!editing || event.button !== 0) return;
      event.preventDefault();
      handle.focus({preventScroll: true});
      window.getSelection().removeAllRanges();
      drag = {edge, pointerId: event.pointerId, start: editing.start, end: editing.end,
        x: event.clientX, y: event.clientY};
      // Capture is best effort: the pointer may already have been cancelled.
      try { handle.setPointerCapture(event.pointerId); } catch { /* document listeners still handle the drag */ }
      scrollFrame = requestAnimationFrame(autoScroll);
    });
    handle.addEventListener('keydown', event => {
      if (!editing || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      let offset = editing[edge];
      if (event.key === 'ArrowRight') offset += String.fromCodePoint(state.rawText.codePointAt(offset) ?? 0).length;
      else offset -= offset >= 2 && /[\uDC00-\uDFFF]/.test(state.rawText[offset - 1]) ? 2 : 1;
      setBoundary(edge, offset);
    });
    handles[edge] = handle;
    preview.appendChild(handle);
  }

  function cancel() {
    finishDrag();
    editing = null;
    main.classList.remove('resizing-annotation');
    layer.hidden = true;
    boxes.replaceChildren();
  }

  function useSelection() {
    if (!editing || drag) return;
    const selection = window.getSelection();
    if (!selection.rangeCount || selection.isCollapsed) return;
    const range = selection.getRangeAt(0);
    const start = offsetAt(range.startContainer, range.startOffset);
    const end = offsetAt(range.endContainer, range.endOffset);
    if (start !== null && end !== null && start < end) {
      editing.start = start;
      editing.end = end;
      draw();
    }
  }

  save.addEventListener('click', () => {
    if (!validContext() || save.disabled) return;
    const {id, start, end} = editing;
    cancel();
    window.getSelection().removeAllRanges();
    onSave(id, start, end);
  });
  cancelButton.addEventListener('click', cancel);
  document.addEventListener('pointermove', event => {
    if (!drag || drag.pointerId !== event.pointerId) return;
    event.preventDefault();
    drag.x = event.clientX;
    drag.y = event.clientY;
    updateFromPoint(drag.x, drag.y);
  });
  document.addEventListener('pointerup', event => {
    if (drag?.pointerId === event.pointerId) finishDrag();
  });
  document.addEventListener('pointercancel', event => {
    if (drag?.pointerId === event.pointerId) finishDrag(true);
  });
  document.addEventListener('keydown', event => {
    if (editing && event.key === 'Enter' && !event.isComposing && !event.repeat
        && event.target !== cancelButton
        && !event.target.closest?.('input, textarea, select, [contenteditable="true"]')) {
      event.preventDefault();
      useSelection();
      save.click();
    }
    if (editing && event.key === 'Escape') {
      event.preventDefault();
      cancel();
      window.getSelection().removeAllRanges();
    }
  });
  document.addEventListener('selectionchange', useSelection);
  document.addEventListener('scroll', draw, true);
  window.addEventListener('resize', draw);
  main.addEventListener('readingchange', draw);
  new ResizeObserver(draw).observe(textDisplay);
  window.addEventListener('blur', () => finishDrag(true));

  return {
    get active() { return Boolean(editing); },
    cancel,
    useSelection,
    begin(id) {
      cancel();
      const annotation = state.annotations.find(a => a.id === id);
      if (!annotation || !state.currentFile || textDisplay.hidden
          || !Number.isInteger(annotation.start) || !Number.isInteger(annotation.end)
          || annotation.start < 0 || annotation.end > state.rawText.length
          || annotation.start >= annotation.end) return;
      editing = {id, file: state.currentFile, rawText: state.rawText,
        start: annotation.start, end: annotation.end,
        originalStart: annotation.start, originalEnd: annotation.end};
      title.textContent = `Resize: ${codeLabel(annotation.codeId)}`;
      title.title = title.textContent;
      window.getSelection().removeAllRanges();
      main.classList.add('resizing-annotation');
      layer.hidden = false;
      // A sidebar entry can represent many screens of text. Bring the end
      // into view so extending it does not begin with an invisible handle.
      const rects = rangeFor(annotation.end - 1, annotation.end).getClientRects();
      const last = rects[rects.length - 1];
      const bounds = main.getBoundingClientRect();
      const top = fileBar.hidden ? bounds.top : fileBar.getBoundingClientRect().bottom;
      if (last && (last.top < top || last.bottom > bounds.bottom - 110)) {
        main.scrollTop += last.top - (top + (bounds.bottom - top) / 2);
      }
      draw();
      handles.end.focus({preventScroll: true});
    }
  };
}
