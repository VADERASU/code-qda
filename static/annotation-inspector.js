import {state, getCodeById, getCodePathLabel, getEffectiveColor} from './state.js';
import {lineRects} from './text-geometry.js';

// Overlays stay outside the transcript so DOM text offsets still match rawText.
export function createAnnotationInspector({textDisplay, main, fileBar, isResizing,
  onInspect, onLocate, onResize, onMove, onAddCode, onRemove}) {
  const card = document.createElement('div');
  card.id = 'annotation-card';
  card.hidden = true;
  const scope = document.createElement('div');
  scope.id = 'annotation-scope';
  scope.setAttribute('aria-hidden', 'true');
  const notice = document.createElement('div');
  notice.id = 'annotation-undo';
  notice.hidden = true;
  document.body.append(scope, card, notice);

  let activeSpan = null;
  let anchorIndex = 0;
  let pinned = false;
  let context = null;
  let previewId = null;
  let selectedId = null;
  let showTimer, hideTimer, layoutFrame;
  let dismissedSpan = null;

  const annotationsFor = span => (span?.dataset.ids || '').split(' ')
    .map(id => state.annotations.find(a => a.id === id))
    .filter(a => a && getCodeById(a.codeId))
    .sort((a, b) => a.start - b.start || b.end - a.end || a.id.localeCompare(b.id));
  const valid = () => context && context.file === state.currentFile
    && context.text === state.rawText && activeSpan?.isConnected && !textDisplay.hidden;
  const bounds = () => {
    const r = main.getBoundingClientRect();
    return {left: r.left + 8, right: Math.min(innerWidth, r.right) - 8,
      top: Math.max(r.top, fileBar.hidden ? r.top : fileBar.getBoundingClientRect().bottom) + 8,
      bottom: Math.min(innerHeight, r.bottom) - 8};
  };

  function drawScope(id) {
    previewId = id;
    scope.replaceChildren();
    card.querySelectorAll('.annotation-row').forEach(row => {
      row.classList.toggle('is-previewed', row.dataset.id === id);
    });
    const annotation = state.annotations.find(a => a.id === id);
    if (!annotation || !valid()) return;
    const walker = document.createTreeWalker(textDisplay, NodeFilter.SHOW_TEXT);
    const range = document.createRange();
    let node, at = 0, foundStart = false;
    while ((node = walker.nextNode())) {
      if (!foundStart && annotation.start < at + node.length) {
        range.setStart(node, annotation.start - at);
        foundStart = true;
      }
      if (foundStart && annotation.end <= at + node.length) {
        range.setEnd(node, annotation.end - at);
        break;
      }
      at += node.length;
    }
    if (!node || !foundStart) return;
    const b = bounds();
    scope.style.clipPath = `inset(${b.top}px ${innerWidth - b.right}px ${innerHeight - b.bottom}px ${b.left}px)`;
    for (const r of lineRects(range)) {
      const box = document.createElement('div');
      box.className = 'annotation-scope-box';
      Object.assign(box.style, {left: `${r.left}px`, top: `${r.top}px`,
        width: `${r.width}px`, height: `${r.height}px`,
        '--code-color': getEffectiveColor(annotation.codeId)});
      scope.append(box);
    }
  }

  function position(preferred = null) {
    if (card.hidden || !valid()) return;
    const b = bounds();
    card.style.maxWidth = `${Math.max(0, b.right - b.left)}px`;
    card.style.maxHeight = `${Math.max(0, b.bottom - b.top)}px`;
    const rects = activeSpan.getClientRects();
    const r = rects[Math.min(anchorIndex, rects.length - 1)];
    if (!r) return;
    const width = card.offsetWidth, height = card.offsetHeight;
    const gutter = textDisplay.getBoundingClientRect().right + 12;
    let left, top;
    if (preferred) {
      ({left, top} = preferred);
    } else if (gutter + width <= b.right) {
      left = gutter;
      top = r.top;
    } else {
      left = r.left;
      // Keep a wrapped highlight readable when there is room beside its full
      // extent. Very long spans still use the hovered line as the fallback.
      const above = rects[0].top - height - 8;
      const below = rects[rects.length - 1].bottom + 8;
      top = above >= b.top ? above
        : below + height <= b.bottom ? below
          : r.top - height - 8 >= b.top ? r.top - height - 8 : r.bottom + 8;
    }
    card.style.left = `${Math.max(b.left, Math.min(left, b.right - width))}px`;
    card.style.top = `${Math.max(b.top, Math.min(top, b.bottom - height))}px`;
  }

  function hide({restoreFocus = false} = {}) {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    cancelAnimationFrame(layoutFrame);
    const trigger = activeSpan;
    const returnFocus = restoreFocus && card.contains(document.activeElement);
    if (trigger) {
      trigger.setAttribute('aria-expanded', 'false');
      trigger.removeAttribute('aria-describedby');
    }
    card.hidden = true;
    scope.replaceChildren();
    pinned = false;
    activeSpan = null;
    context = null;
    previewId = null;
    selectedId = null;
    if (returnFocus && trigger?.isConnected) {
      dismissedSpan = trigger;
      trigger.focus({preventScroll: true});
    }
  }

  const scheduleHide = () => {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    if (!pinned) hideTimer = setTimeout(() => {
      if (!card.matches(':hover') && !card.contains(document.activeElement)) hide();
    }, 250);
  };
  const button = (text, className, action, label) => {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = className;
    element.textContent = text;
    if (label) element.setAttribute('aria-label', label);
    element.addEventListener('click', action);
    return element;
  };

  function remove(annotation) {
    const returnFocus = card.contains(document.activeElement);
    hide();
    const undo = onRemove(annotation.id);
    if (!undo) return;
    notice.replaceChildren();
    const message = document.createElement('span');
    message.setAttribute('role', 'status');
    message.textContent = `Removed coding: ${getCodePathLabel(annotation.codeId)}`;
    const undoButton = button('Undo', 'annotation-undo-action', () => {
      notice.hidden = true;
      if (undo()) {
        const trigger = [...textDisplay.querySelectorAll('.coded')]
          .find(span => span.dataset.ids.split(' ').includes(annotation.id));
        if (trigger && returnFocus) {
          dismissedSpan = trigger;
          trigger.focus({preventScroll: true});
        }
      }
    });
    notice.append(message, undoButton,
      button('×', 'annotation-close', () => notice.hidden = true, 'Dismiss undo notice'));
    notice.hidden = false;
    if (returnFocus) undoButton.focus({preventScroll: true});
  }

  function selectAnnotation(id, {locate = true} = {}) {
    const row = [...card.querySelectorAll('.annotation-row')].find(element => element.dataset.id === id);
    if (!row || !valid()) return;
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    const previous = card.getBoundingClientRect();
    selectedId = id;
    if (!pinned) {
      pinned = true;
      dismissedSpan = null;
      card.classList.add('is-pinned');
      onInspect();
    }
    for (const element of card.querySelectorAll('.annotation-row')) {
      const selected = element.dataset.id === id;
      element.classList.toggle('is-selected', selected);
      element.querySelector('.annotation-code-button').setAttribute('aria-pressed', String(selected));
      element.querySelector('.annotation-actions').hidden = true;
      element.querySelector('.annotation-more').setAttribute('aria-expanded', 'false');
    }
    // Selection changes emphasis and navigation, never the card's layout.
    position(previous);
    row.querySelector('.annotation-code-button').focus({preventScroll: true});
    drawScope(id);
    // Navigation accompanies selection; it does not replace the editing card.
    if (locate) onLocate(id);
  }

  function render(annotations) {
    card.replaceChildren();
    card.classList.toggle('is-pinned', pinned);
    // The hover card contains code buttons, so it is an interactive popup.
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-labelledby', 'annotation-card-title');
    const header = document.createElement('div');
    header.className = 'annotation-card-header';
    const title = document.createElement('span');
    title.id = 'annotation-card-title';
    title.textContent = `Applied here · ${annotations.length} ${annotations.length === 1 ? 'code' : 'codes'}`;
    header.append(title,
      button('×', 'annotation-close', () => hide({restoreFocus: true}), 'Close annotation card'));
    card.append(header);
    for (const annotation of annotations) {
      const path = getCodePathLabel(annotation.codeId);
      const code = getCodeById(annotation.codeId);
      const row = document.createElement('div');
      row.className = 'annotation-row';
      row.dataset.id = annotation.id;
      const swatch = document.createElement('span');
      swatch.className = 'annotation-swatch';
      swatch.style.background = getEffectiveColor(annotation.codeId);
      swatch.setAttribute('aria-hidden', 'true');
      const label = button('', 'annotation-code-button', () => selectAnnotation(annotation.id), `Show ${path} in codebook`);
      label.setAttribute('aria-pressed', 'false');
      label.classList.add('annotation-code-label');
      const name = document.createElement('span');
      name.className = 'annotation-code-name';
      name.textContent = code.name;
      label.append(name);
      if (code.parentId) {
        const parent = document.createElement('span');
        parent.className = 'annotation-code-path';
        parent.textContent = getCodePathLabel(code.parentId);
        label.append(parent);
      }
      const add = button('+ Code', 'annotation-add-code', () => {
        const r = add.getBoundingClientRect();
        hide();
        onAddCode(annotation, r);
      }, `Add code to this passage: ${path}`);
      row.append(swatch, label);
      const tools = document.createElement('div');
      tools.className = 'annotation-tools';
      tools.id = `annotation-tools-${annotation.id}`;
      const resize = button('Adjust span', 'annotation-adjust', () => onResize(annotation.id), `Adjust span: ${path}`);
      resize.dataset.id = annotation.id;
      const actions = document.createElement('div');
      actions.className = 'annotation-actions';
      actions.id = `annotation-actions-${annotation.id}`;
      actions.hidden = true;
      const more = button('More', 'annotation-more', () => {
        const previous = card.getBoundingClientRect();
        const open = actions.hidden;
        card.querySelectorAll('.annotation-actions').forEach(el => el.hidden = true);
        card.querySelectorAll('.annotation-more').forEach(el => el.setAttribute('aria-expanded', 'false'));
        actions.hidden = !open;
        more.setAttribute('aria-expanded', String(open));
        position(previous);
      }, `More actions: ${path}`);
      more.setAttribute('aria-expanded', 'false');
      more.setAttribute('aria-controls', actions.id);
      actions.append(
        button('Move to another code', '', () => {
          const r = more.getBoundingClientRect();
          hide();
          onMove(annotation.id, r);
        }),
        button('Remove coding', 'annotation-remove', () => remove(annotation))
      );
      tools.append(add, resize, more);
      row.append(tools, actions);
      row.addEventListener('click', event => {
        if (!event.target.closest('button')) selectAnnotation(annotation.id);
      });
      row.addEventListener('mouseenter', () => drawScope(annotation.id));
      row.addEventListener('focusin', () => drawScope(annotation.id));
      row.addEventListener('mouseleave', () => {
        const focused = card.querySelector('.annotation-row:focus-within');
        drawScope(focused?.dataset.id || selectedId);
      });
      row.addEventListener('focusout', event => {
        if (!row.contains(event.relatedTarget)) drawScope(selectedId);
      });
      card.append(row);
    }
    const hint = document.createElement('div');
    hint.className = 'annotation-card-hint';
    hint.textContent = 'Click a name to find it in Codes.';
    card.append(hint);
  }

  function show(span, event, pin = false) {
    if (isResizing() || window.getSelection()?.toString() || (!pin && pinned)) return;
    const annotations = annotationsFor(span);
    if (!annotations.length) return;
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    if (activeSpan) {
      activeSpan.setAttribute('aria-expanded', 'false');
      activeSpan.removeAttribute('aria-describedby');
    }
    activeSpan = span;
    pinned = pin;
    context = {file: state.currentFile, text: state.rawText};
    const rects = [...span.getClientRects()];
    anchorIndex = Math.max(0, rects.findIndex(r => event && event.clientY >= r.top && event.clientY <= r.bottom));
    scope.replaceChildren();
    previewId = null;
    selectedId = null;
    render(annotations);
    card.hidden = false;
    span.setAttribute('aria-expanded', 'true');
    position();
    if (pin) {
      dismissedSpan = null;
      onInspect();
      if (annotations.length === 1) selectAnnotation(annotations[0].id, {locate: false});
      else card.querySelector('.annotation-code-button')?.focus({preventScroll: true});
    }
  }

  function preview(event) {
    const span = event.target.closest('.coded[data-ids]');
    if (!span || span === dismissedSpan || pinned || isResizing() || event.buttons
        || window.getSelection()?.toString()) return;
    clearTimeout(hideTimer);
    clearTimeout(showTimer);
    if (!card.hidden && activeSpan === span) return;
    showTimer = setTimeout(() => show(span, event), 225);
  }

  textDisplay.addEventListener('mouseover', preview);
  textDisplay.addEventListener('focusin', preview);
  const leave = event => {
    const span = event.target.closest('.coded[data-ids]');
    if (!span || span.contains(event.relatedTarget)) return;
    if (dismissedSpan === span) dismissedSpan = null;
    if (!card.contains(event.relatedTarget)) scheduleHide();
  };
  textDisplay.addEventListener('mouseout', leave);
  textDisplay.addEventListener('focusout', leave);
  textDisplay.addEventListener('click', event => {
    const span = event.target.closest('.coded[data-ids]');
    if (span) show(span, event, true);
  });
  textDisplay.addEventListener('keydown', event => {
    const span = event.target.closest('.coded[data-ids]');
    if (span && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      show(span, null, true);
    }
  });
  card.addEventListener('mouseenter', () => {
    clearTimeout(hideTimer);
    clearTimeout(showTimer);
  });
  card.addEventListener('mouseleave', event => {
    if (!activeSpan?.contains(event.relatedTarget)) scheduleHide();
  });
  card.addEventListener('focusin', () => {
    clearTimeout(hideTimer);
    clearTimeout(showTimer);
  });
  card.addEventListener('focusout', event => {
    if (!card.contains(event.relatedTarget)) scheduleHide();
  });
  document.addEventListener('mousedown', event => {
    if (!card.contains(event.target) && !event.target.closest('.coded[data-ids]')) hide();
  });
  // Touch and mouse selection take precedence over any hover preview.
  textDisplay.addEventListener('mousedown', () => hide());
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || card.hidden) return;
    const expanded = card.querySelector('.annotation-more[aria-expanded="true"]');
    if (expanded) {
      const previous = card.getBoundingClientRect();
      card.querySelectorAll('.annotation-actions').forEach(el => el.hidden = true);
      expanded.setAttribute('aria-expanded', 'false');
      expanded.focus();
      position(previous);
    } else {
      dismissedSpan = activeSpan;
      hide({restoreFocus: true});
    }
  });
  const layout = event => {
    if (card.hidden || card.contains(event.target)) return;
    // Revealing a codebook entry scrolls the sidebar, not the transcript.
    // Keep the pinned operations in place during that navigation.
    if (event.type === 'scroll' && event.target instanceof Element && !main.contains(event.target)) return;
    if (!pinned || !valid()) { hide(); return; }
    cancelAnimationFrame(layoutFrame);
    layoutFrame = requestAnimationFrame(() => {
      position();
      if (previewId) drawScope(previewId);
    });
  };
  document.addEventListener('scroll', layout, true);
  window.addEventListener('resize', layout);
  main.addEventListener('readingchange', layout);
  const observer = new ResizeObserver(() => layout({target: main}));
  observer.observe(main);
  observer.observe(textDisplay);

  return {hide, reset() { hide(); notice.hidden = true; dismissedSpan = null; }};
}
