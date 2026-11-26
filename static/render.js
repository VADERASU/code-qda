import {PAST, markPlace, placeAt, readingLine} from './place.js';
import {searchCodes} from './code-search.js';
import {createAnnotationResizer} from './annotation-resize.js';
import {createAnnotationInspector} from './annotation-inspector.js';
import {lineRects} from './text-geometry.js';
import {
  PALETTE,
  state,
  esc,
  getChildren,
  getCodeById,
  getCodePathLabel,
  getEffectiveColor,
  hashColor,
  getOverviewChildren,
  getOverviewEffectiveColor,
  overviewIsDescendant
} from './state.js';

const dom = {
  fileList: document.getElementById('file-list'),
  codeList: document.getElementById('code-list'),
  codeSearch: document.getElementById('code-search'),
  collapseCodesButton: document.getElementById('collapse-codes-btn'),
  saveStatus: document.getElementById('save-status'),
  emptyState: document.getElementById('empty-state'),
  textDisplay: document.getElementById('text-display'),
  popover: document.getElementById('popover'),
  codeInput: document.getElementById('code-input'),
  codeSuggestions: document.getElementById('code-suggestions'),
  codeChoiceHint: document.getElementById('code-choice-hint'),
  codeSave: document.getElementById('code-save'),
  colorPicker: document.getElementById('color-picker'),
  cpTitle: document.getElementById('cp-title'),
  cpGrid: document.getElementById('cp-grid'),
  cpBulk: document.getElementById('cp-bulk'),
  cpBulkCheck: document.getElementById('cp-bulk-check'),
  cpBulkText: document.getElementById('cp-bulk-text'),
  fileBar: document.getElementById('file-bar'),
  pruneButton: document.getElementById('prune-btn'),
  addCodeButton: document.getElementById('add-code-btn')
};

const TREE_INDENT_PX = 7;
const INSTANCE_OFFSET_PX = 16;

function charOffset(container, node, offset) {
  if (!container.contains(node)) return -1;
  const range = document.createRange();
  range.selectNodeContents(container);
  range.setEnd(node, offset);
  return range.toString().length;
}

function makeInstance(annotation, depth, handlers) {
  const instance = document.createElement('div');
  instance.className = 'code-instance';
  instance.dataset.id = annotation.id;
  instance.style.paddingLeft = `${INSTANCE_OFFSET_PX + depth * TREE_INDENT_PX}px`;
  const preview = annotation.text.replace(/\s+/g, ' ').trim();

  const textSpan = document.createElement('span');
  textSpan.className = 'code-instance-text';
  textSpan.title = preview;
  textSpan.textContent = preview.slice(0, 55) + (preview.length > 55 ? '…' : '');

  const moveButton = document.createElement('button');
  moveButton.className = 'code-action';
  moveButton.type = 'button';
  moveButton.title = 'Move to another code';
  moveButton.textContent = 'move';

  const deleteButton = document.createElement('button');
  deleteButton.className = 'code-delete';
  deleteButton.type = 'button';
  deleteButton.title = 'Remove';
  deleteButton.textContent = '×';

  const resizeButton = document.createElement('button');
  resizeButton.className = 'code-action code-resize';
  resizeButton.type = 'button';
  resizeButton.title = `Resize coding: ${getCodePathLabel(annotation.codeId)}`;
  resizeButton.setAttribute('aria-label', resizeButton.title);
  resizeButton.textContent = 'resize';
  resizeButton.addEventListener('click', event => {
    event.stopPropagation();
    beginAnnotationResize(annotation.id);
  });

  moveButton.addEventListener('click', event => {
    event.stopPropagation();
    openMovePopover(annotation.id, moveButton.getBoundingClientRect());
  });

  deleteButton.addEventListener('click', event => {
    event.stopPropagation();
    handlers.onDeleteAnnotation(annotation.id);
  });

  instance.addEventListener('click', () => {
    const element = document.querySelector(`.coded[data-ids~="${annotation.id}"]`);
    if (element) {
      element.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
  });

  instance.appendChild(textSpan);
  instance.appendChild(resizeButton);
  instance.appendChild(moveButton);
  instance.appendChild(deleteButton);

  return instance;
}

function annotationsByCodeId() {
  const grouped = new Map();
  state.annotations.forEach(annotation => {
    if (!getCodeById(annotation.codeId)) {
      return;
    }
    if (!grouped.has(annotation.codeId)) {
      grouped.set(annotation.codeId, []);
    }
    grouped.get(annotation.codeId).push(annotation);
  });
  return grouped;
}

function subtreeCount(nodeId, grouped) {
  let total = (grouped.get(nodeId) || []).length;
  getChildren(nodeId).forEach(child => {
    total += subtreeCount(child.id, grouped);
  });
  return total;
}

function firstAnnotationId(nodeId, grouped) {
  const direct = grouped.get(nodeId) || [];
  if (direct.length > 0) {
    return direct[0].id;
  }
  for (const child of getChildren(nodeId)) {
    const childId = firstAnnotationId(child.id, grouped);
    if (childId) {
      return childId;
    }
  }
  return null;
}

function isDescendant(nodeId, possibleAncestorId) {
  const seen = new Set();
  let current = getCodeById(nodeId);

  while (current && current.parentId && !seen.has(current.id)) {
    if (current.parentId === possibleAncestorId) {
      return true;
    }
    seen.add(current.id);
    current = getCodeById(current.parentId);
  }

  return false;
}

function canDropOn(targetId) {
  return Boolean(state.draggedCodeId)
    && state.draggedCodeId !== targetId
    && !isDescendant(targetId, state.draggedCodeId);
}

function attachDragHandlers(label, node, handlers) {
  label.draggable = true;
  label.addEventListener('dragstart', event => {
    state.draggedCodeId = node.id;
    label.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', node.id);
  });
  label.addEventListener('dragend', () => {
    state.draggedCodeId = null;
    label.classList.remove('dragging');
    document.querySelectorAll('.drag-over').forEach(element => element.classList.remove('drag-over'));
  });
  label.addEventListener('dragover', event => {
    if (!canDropOn(node.id)) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    label.classList.add('drag-over');
  });
  label.addEventListener('dragleave', () => label.classList.remove('drag-over'));
  label.addEventListener('drop', event => {
    if (!canDropOn(node.id)) {
      return;
    }
    event.preventDefault();
    label.classList.remove('drag-over');
    handlers.onReparentCode(state.draggedCodeId, node.id);
  });
}

function canDropOnOverview(targetId) {
  return Boolean(state.draggedCodeId)
    && state.draggedCodeId !== targetId
    && !overviewIsDescendant(targetId, state.draggedCodeId);
}

function attachOverviewDragHandlers(label, node, handlers) {
  label.draggable = true;
  label.addEventListener('dragstart', event => {
    event.stopPropagation();
    state.draggedCodeId = node.id;
    label.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', node.id);
  });
  label.addEventListener('dragend', () => {
    state.draggedCodeId = null;
    label.classList.remove('dragging');
    document.querySelectorAll('.drag-over').forEach(element => element.classList.remove('drag-over'));
  });
  label.addEventListener('dragover', event => {
    if (!canDropOnOverview(node.id)) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    label.classList.add('drag-over');
  });
  label.addEventListener('dragleave', () => label.classList.remove('drag-over'));
  label.addEventListener('drop', event => {
    if (!canDropOnOverview(node.id)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    label.classList.remove('drag-over');
    handlers.onReparentOverviewCode(state.draggedCodeId, node.id);
  });
}

function canDropOnGap(targetIndex, parentId, overview) {
  if (!state.draggedCodeId) return false;
  const node = overview ? state.overviewById.get(state.draggedCodeId) : getCodeById(state.draggedCodeId);
  const descendant = overview ? overviewIsDescendant : isDescendant;
  if (!node || parentId === node.id || (parentId && descendant(parentId, node.id))) return false;
  // A gap identifies the destination level, so crossing parents also promotes
  // or reparents the code. Color never limits where a code can be placed.
  if (node.parentId !== parentId) return true;
  const siblings = overview ? getOverviewChildren(parentId) : getChildren(parentId);
  const currentIndex = siblings.findIndex(n => n.id === state.draggedCodeId);
  return currentIndex >= 0 && targetIndex !== currentIndex && targetIndex !== currentIndex + 1;
}

function makeGapDropTarget(targetIndex, parentId, handlers, depth = 0, overview = false) {
  const gap = document.createElement('div');
  gap.className = 'code-gap-target';
  gap.dataset.parentId = parentId || '';
  gap.dataset.index = targetIndex;
  gap.style.marginLeft = `${2 + depth * TREE_INDENT_PX}px`;
  gap.addEventListener('dragover', event => {
    if (!canDropOnGap(targetIndex, parentId, overview)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    gap.classList.add('drag-over');
  });
  gap.addEventListener('dragleave', () => gap.classList.remove('drag-over'));
  gap.addEventListener('drop', event => {
    if (!canDropOnGap(targetIndex, parentId, overview)) return;
    event.preventDefault();
    event.stopPropagation();
    gap.classList.remove('drag-over');
    const onMove = overview ? handlers.onReorderOverviewCode : handlers.onReorderCode;
    onMove(state.draggedCodeId, targetIndex, parentId);
  });
  return gap;
}

function makeSwatch(node, handlers) {
  const effectiveColor = state.currentFile === null ? getOverviewEffectiveColor : getEffectiveColor;
  const swatch = document.createElement('span');
  swatch.className = 'code-swatch';
  swatch.style.background = effectiveColor(node.id);
  if (node.parentId === null) {
    swatch.title = 'Change color';
    swatch.addEventListener('click', event => {
      event.stopPropagation();
      handlers.onOpenColorPicker(node.id, swatch);
    });
  } else {
    swatch.title = 'Child codes inherit their root color';
    swatch.style.cursor = 'default';
  }
  return swatch;
}

function makeRenameButton(node, handlers) {
  const button = document.createElement('button');
  button.className = 'code-action';
  button.type = 'button';
  button.textContent = 'edit';
  button.title = 'Rename code';
  button.setAttribute('aria-label', `Rename ${node.name}`);
  button.addEventListener('click', event => {
    event.stopPropagation();
    handlers.onStartRenameCode(node.id);
  });
  return button;
}

function makeAddChildButton(node, handlers) {
  const button = document.createElement('button');
  button.className = 'code-action code-add-child';
  button.type = 'button';
  button.textContent = '+';
  button.title = 'Add child code';
  button.setAttribute('aria-label', `Add child code under ${node.name}`);
  button.addEventListener('click', event => {
    event.stopPropagation();
    handlers.onAddChildCode(node.id);
  });
  return button;
}

function makeDeleteCodeButton(node, handlers, localCount = 0) {
  // Counts from another transcript must also protect a code and its subtree.
  // While aggregate counts are loading, offer no deletion shortcut.
  if (!state.overviewLoaded || localCount > 0) return null;
  const tree = state.currentFile === null ? state.overviewCodeTree : state.codeTree;
  const pending = [node.id];
  while (pending.length > 0) {
    const id = pending.pop();
    if ((state.overviewCountsById.get(id) || 0) > 0) return null;
    tree.filter(entry => entry.parentId === id).forEach(child => pending.push(child.id));
  }
  const button = document.createElement('button');
  button.className = 'code-delete';
  button.type = 'button';
  button.textContent = '×';
  button.title = 'Delete code (only if unused in all files)';
  button.setAttribute('aria-label', `Delete unused code ${node.name}`);
  button.addEventListener('click', event => {
    event.stopPropagation();
    handlers.onDeleteCodeNode(node.id);
  });
  return button;
}

function makeCodeName(node, depth, handlers, highlights = []) {
  if (state.renamingCodeId === node.id) {
    const input = document.createElement('input');
    input.className = 'code-rename-input';
    input.type = 'text';
    input.value = node.name;
    input.spellcheck = false;
    input.setAttribute('aria-label', `Rename ${node.name}`);

    let handled = false;
    const commit = () => {
      if (handled) {
        return;
      }
      handled = true;
      handlers.onCommitRenameCode(node.id, input.value);
    };
    const cancel = () => {
      if (handled) {
        return;
      }
      handled = true;
      handlers.onCancelRenameCode(node.id);
    };

    input.addEventListener('click', event => event.stopPropagation());
    input.addEventListener('mousedown', event => event.stopPropagation());
    input.addEventListener('keydown', event => {
      event.stopPropagation();
      if (event.key === 'Enter') {
        event.preventDefault();
        commit();
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        cancel();
      }
    });
    input.addEventListener('blur', commit);

    requestAnimationFrame(() => {
      if (!input.isConnected) return;
      input.focus();
      input.select();
    });
    return input;
  }
  const nameElement = document.createElement('span');
  nameElement.className = 'code-name' + (depth > 0 ? ' child' : '');
  nameElement.innerHTML = highlightedHtml(node.name, highlights);
  nameElement.title = state.currentFile === null ? node.name : getCodePathLabel(node.id);
  return nameElement;
}

function makeCodeGroup(node, grouped, depth, handlers, visibleIds, nameHighlights) {
  if (visibleIds && !visibleIds.has(node.id)) {
    return null;
  }
  const group = document.createElement('div');
  group.className = 'code-group';
  group.dataset.codeId = node.id;

  const children = getChildren(node.id);
  const hasChildren = children.length > 0;
  const isCollapsed = !visibleIds && state.collapsed.has(node.id);
  const directAnnotations = grouped.get(node.id) || [];
  const isRenaming = state.renamingCodeId === node.id;

  const label = document.createElement('div');
  label.className = 'code-label' + (isRenaming ? ' editing' : '');
  label.style.paddingLeft = `${depth * TREE_INDENT_PX}px`;
  if (!isRenaming) {
    attachDragHandlers(label, node, handlers);
  }

  if (hasChildren) {
    const toggle = document.createElement('span');
    toggle.className = 'tree-toggle';
    toggle.textContent = isCollapsed ? '▸' : '▾';
    if (visibleIds) toggle.title = 'Expanded while searching';
    toggle.addEventListener('click', event => {
      event.stopPropagation();
      if (visibleIds) return;
      if (state.collapsed.has(node.id)) {
        state.collapsed.delete(node.id);
      } else {
        state.collapsed.add(node.id);
      }
      renderSidebar(handlers);
    });
    label.appendChild(toggle);
  } else {
    const spacer = document.createElement('span');
    spacer.className = 'tree-spacer';
    label.appendChild(spacer);
  }

  label.appendChild(makeSwatch(node, handlers));
  label.appendChild(makeCodeName(node, depth, handlers, nameHighlights.get(node.id)));

  const count = subtreeCount(node.id, grouped);
  const countElement = document.createElement('span');
  countElement.className = 'code-count';
  countElement.textContent = count;
  label.appendChild(countElement);

  label.appendChild(makeRenameButton(node, handlers));
  label.appendChild(makeAddChildButton(node, handlers));

  const deleteButton = makeDeleteCodeButton(node, handlers, count);
  if (deleteButton) label.appendChild(deleteButton);

  label.addEventListener('click', () => {
    const annotationId = firstAnnotationId(node.id, grouped);
    if (!annotationId) {
      return;
    }
    const target = document.querySelector(`.coded[data-ids~="${annotationId}"]`);
    if (target) {
      target.scrollIntoView({behavior: 'smooth', block: 'center'});
    }
  });

  group.appendChild(label);

  if (!isCollapsed) {
    directAnnotations.forEach(annotation => {
      group.appendChild(makeInstance(annotation, depth, handlers));
    });
    children.forEach((child, i) => {
      const childGroup = makeCodeGroup(child, grouped, depth + 1, handlers, visibleIds, nameHighlights);
      if (!childGroup) return;
      group.appendChild(makeGapDropTarget(i, node.id, handlers, depth + 1));
      group.appendChild(childGroup);
    });
    if (children.length > 0) {
      group.appendChild(makeGapDropTarget(children.length, node.id, handlers, depth + 1));
    }
  }

  return group;
}

function annotationColorLanes(annotations) {
  const lanes = [];
  const byId = new Map();
  // Each annotation keeps its lane until it ends. An overlapping annotation
  // with the same inherited color shares that lane instead of duplicating it.
  for (const annotation of annotations) {
    const color = getEffectiveColor(annotation.codeId);
    let lane = lanes.findIndex(entry => entry.end > annotation.start && entry.color === color);
    if (lane < 0) lane = lanes.findIndex(entry => entry.end <= annotation.start);
    if (lane < 0) lane = lanes.length;
    const end = Math.max(annotation.end, lanes[lane]?.end ?? 0);
    lanes[lane] = {color, end};
    byId.set(annotation.id, lane);
  }
  return {byId, count: Math.max(2, lanes.length)};
}

function buildSpanStyle(annotations, lanes) {
  const colors = Array(lanes.count).fill('transparent');
  for (const annotation of annotations) {
    colors[lanes.byId.get(annotation.id)] = getEffectiveColor(annotation.codeId);
  }
  const stops = colors.map((color, i) => `${color} ${i * 2}px ${(i + 1) * 2}px`);
  const primary = getEffectiveColor(annotations[0].codeId);
  return `--annotation-color:${primary};--annotation-lines:linear-gradient(to bottom,${stops.join(',')})`;
}

// --- code combobox -------------------------------------------------------

let uiHandlers = {};
let annotationInspector = null;
const annotationResizer = createAnnotationResizer({
  textDisplay: dom.textDisplay,
  main: document.getElementById('main'),
  fileBar: dom.fileBar,
  codeLabel: getCodePathLabel,
  onSave: (id, start, end) => uiHandlers.onResizeAnnotation(id, start, end)
});

function beginAnnotationResize(annotationId) {
  hidePopover();
  annotationInspector?.hide();
  state.pendingSelection = null;
  annotationResizer.begin(annotationId);
}

export function cancelAnnotationResize() {
  annotationResizer.cancel();
  annotationInspector?.reset();
  hidePopover();
}

let suggestionItems = [];
let highlightIndex = -1;
// Called when the user commits a choice in the code popover. Set per-open so the
// same searchable combobox can both assign a new code and move an existing one.
// Receives the chosen existing codeId, or undefined when the user typed free text
// (the callback then resolves/creates from the input value).
let popoverOnPick = () => {};
let popoverAnchor = null;

function positionCodePopover() {
  if (dom.popover.hidden || !popoverAnchor) return;
  const gap = 8;
  const {range, anchorRange, lineIndex, rect, file, text} = popoverAnchor;
  if (file !== state.currentFile || text !== state.rawText
      || (range && (!dom.textDisplay.contains(range.commonAncestorContainer) || dom.textDisplay.hidden))) {
    hidePopover();
    return;
  }
  const anchor = anchorRange?.getBoundingClientRect()
    || (range ? lineRects(range)[lineIndex] : rect);
  const main = document.getElementById('main').getBoundingClientRect();
  const visibleTop = Math.max(main.top, dom.fileBar.hidden ? main.top : dom.fileBar.getBoundingClientRect().bottom);
  if (!anchor || (range && (anchor.bottom < visibleTop || anchor.top > Math.min(main.bottom, innerHeight)))) {
    hidePopover();
    return;
  }
  const {width, height} = dom.popover.getBoundingClientRect();
  const {left, top, bottom} = anchor;
  let y = bottom + gap;
  if (y + height > window.innerHeight - gap) {
    y = top - height - gap;
  }
  dom.popover.style.left = `${Math.max(gap, Math.min(left, window.innerWidth - width - gap))}px`;
  dom.popover.style.top = `${Math.max(gap, Math.min(y, window.innerHeight - height - gap))}px`;
}

function highlightedHtml(label, ranges = []) {
  const merged = [];
  for (const [start, end] of [...ranges].sort((a, b) => a[0] - b[0])) {
    const previous = merged.at(-1);
    if (previous && start <= previous[1]) previous[1] = Math.max(previous[1], end);
    else merged.push([start, end]);
  }
  let offset = 0;
  let html = '';
  for (const [start, end] of merged) {
    html += esc(label.slice(offset, start)) + '<mark>' + esc(label.slice(start, end)) + '</mark>';
    offset = end;
  }
  return html + esc(label.slice(offset));
}

function setHighlight(index, {scroll = true} = {}) {
  const items = dom.codeSuggestions.children;
  if (items.length === 0) return;
  highlightIndex = (index + items.length) % items.length;
  Array.from(items).forEach((el, i) => {
    el.classList.toggle('active', i === highlightIndex);
  });
  dom.codeSave.disabled = false;
  dom.codeChoiceHint.hidden = true;
  if (scroll) items[highlightIndex].scrollIntoView({block: 'nearest'});
}

function chooseSuggestion(index) {
  const item = suggestionItems[index];
  if (!item) return;
  if (item.codeId) {
    popoverOnPick(item.codeId);
  } else {
    dom.codeInput.value = item.label;
    popoverOnPick();
  }
}

// Fill the input with the highlighted suggestion without applying, so the user
// can keep typing (e.g. append "/ child" to nest under it).
function completeWithHighlight() {
  const item = suggestionItems[highlightIndex];
  if (!item || item.isCreate) return;
  dom.codeInput.value = item.label;
  dom.codeInput.setSelectionRange(item.label.length, item.label.length);
  renderCodeSuggestions(item.label);
}

// Color a not-yet-created code will get once committed, mirroring
// resolveCodeFromInput: nested codes inherit their root's color; a new
// top-level code gets a deterministic hashColor of its name.
function previewCreateColor(label) {
  const firstSegment = label.split('/').map(s => s.trim()).filter(Boolean)[0] || label;
  const existingRoot = state.codeTree.find(n =>
    n.parentId === null && n.name.toLowerCase() === firstSegment.toLowerCase());
  return existingRoot ? getEffectiveColor(existingRoot.id) : hashColor(firstSegment);
}

function renderCodeSuggestions(query) {
  const list = dom.codeSuggestions;
  list.innerHTML = '';
  suggestionItems = [];
  highlightIndex = -1;
  const q = query.trim();

  const matches = searchCodes(state.codeTree, q);
  const scored = matches.slice(0, 8);

  scored.forEach(entry =>
    suggestionItems.push({codeId: entry.node.id, label: entry.label, ranges: entry.ranges,
      nameStart: entry.path.at(-1).start, parentEnd: entry.path.at(-2)?.end ?? 0}));

  const exactPaths = matches.filter(entry => entry.exactPath);
  const exactNames = matches.filter(entry => entry.exactName);
  const exact = exactPaths.length === 1 ? exactPaths[0] : exactNames.length === 1 ? exactNames[0] : null;
  if (q && !exact) {
    suggestionItems.push({codeId: null, label: q, isCreate: true});
  }

  dom.codeSave.disabled = true;
  dom.codeChoiceHint.hidden = false;
  if (suggestionItems.length === 0) {
    list.hidden = true;
    positionCodePopover();
    return;
  }

  suggestionItems.forEach((item, i) => {
    const el = document.createElement('div');
    el.className = 'suggestion-item';
    if (item.codeId) el.dataset.codeId = item.codeId;
    const color = item.codeId ? getEffectiveColor(item.codeId) : previewCreateColor(item.label);
    const swatch = `<span class="suggestion-swatch" style="background:${esc(String(color))}"></span>`;
    // Matching offsets refer to the full path. Keep the highlights attached to
    // their original text when showing the leaf name ahead of its ancestors.
    const part = (start, end) => highlightedHtml(item.label.slice(start, end),
      item.ranges.filter(([from, to]) => from < end && to > start)
        .map(([from, to]) => [Math.max(from, start) - start, Math.min(to, end) - start]));
    const label = item.isCreate
      ? '<span class="suggestion-create">+ Create new code</span>'
        + `<span class="suggestion-name">${esc(item.label)}</span>`
      : `<span class="suggestion-name">${part(item.nameStart, item.label.length)}</span>`
        + (item.parentEnd ? `<span class="suggestion-path">${part(0, item.parentEnd)
          .replace(/ \/ /g, ' <span class="suggestion-sep">/</span> ')}</span>` : '');
    el.classList.toggle('is-create', Boolean(item.isCreate));
    el.innerHTML = swatch + `<span class="suggestion-label">${label}</span>`;
    el.addEventListener('mousedown', event => {
      event.preventDefault();
      chooseSuggestion(i);
    });
    el.addEventListener('mouseenter', () => setHighlight(i, {scroll: false}));
    list.appendChild(el);
  });
  list.hidden = false;
  positionCodePopover();
  if (exact) {
    const index = suggestionItems.findIndex(item => item.codeId === exact.node.id);
    if (index >= 0) setHighlight(index);
  } else if (q && scored.length === 0) {
    // A new name has only the create option; make it ready to accept.
    setHighlight(0);
  }
}

// Open the searchable code popover anchored to `rect`. `onPick` is invoked with
// the chosen existing codeId, or with no argument when the user committed free
// text (the handler then resolves/creates from the input value).
function openCodePopover({rect, range = null, anchorRange = null, initialText = '', onPick}) {
  cancelAnnotationResize();
  popoverOnPick = onPick;
  const lines = range ? lineRects(range) : [];
  // For an existing passage, use the line beside the clicked action. For a
  // fresh selection, the live caret range follows the end of the user's drag.
  const lineIndex = lines.reduce((best, line, i) =>
    Math.abs(line.top - rect.top) < Math.abs(lines[best].top - rect.top) ? i : best, 0);
  popoverAnchor = {rect: {left: rect.left, top: rect.top ?? rect.bottom, bottom: rect.bottom},
    range, anchorRange, lineIndex, file: state.currentFile, text: state.rawText};
  dom.popover.hidden = false;
  dom.codeInput.value = initialText;
  renderCodeSuggestions(initialText);
  if (dom.popover.hidden) return;
  if (range && CSS.highlights) CSS.highlights.set('pending-code', new Highlight(range));
  dom.codeInput.focus({preventScroll: true});
  if (initialText) dom.codeInput.select();
}

function openPopoverForSpan({start, end, text}, rect, anchorRange = null) {
  const runs = textRuns();
  const from = runs.find(run => start < run.start + run.node.length);
  const to = runs.find(run => end <= run.start + run.node.length);
  if (!from || !to) return;
  const range = document.createRange();
  range.setStart(from.node, start - from.start);
  range.setEnd(to.node, end - to.start);
  // A selected word or short phrase makes a useful starting query. Keep the
  // whole passage unchanged, and leave longer passages ready for a code name.
  const query = text.trim();
  openCodePopover({
    rect, range, anchorRange,
    initialText: anchorRange && query.split(/\s+/).length <= 2 ? query : '',
    onPick: codeId => uiHandlers.onApplyCode(codeId)
  });
  if (!dom.popover.hidden) state.pendingSelection = {start, end, text};
}

// Open the same combobox to move an existing annotation to another code.
export function openMovePopover(annotationId, rect) {
  openCodePopover({
    rect,
    onPick: codeId => uiHandlers.onMoveAnnotation(annotationId, codeId)
  });
}

function setupAnnotationInspector() {
  if (annotationInspector) return;
  annotationInspector = createAnnotationInspector({
    textDisplay: dom.textDisplay,
    main: document.getElementById('main'),
    fileBar: dom.fileBar,
    isResizing: () => annotationResizer.active || !dom.popover.hidden,
    onInspect: () => {
      hidePopover();
      state.pendingSelection = null;
    },
    onLocate: id => uiHandlers.onNavigateToInstance(id),
    onResize: beginAnnotationResize,
    onMove: openMovePopover,
    onAddCode: (annotation, rect) => openPopoverForSpan(annotation, rect),
    onRemove: id => uiHandlers.onDeleteAnnotation(id)
  });
}

export function bindUiEvents(handlers) {
  uiHandlers = handlers;
  setupAnnotationInspector();
  window.addEventListener('resize', positionCodePopover);
  document.addEventListener('scroll', event => {
    if (dom.popover.hidden || dom.popover.contains(event.target)) return;
    if (event.target === document || document.getElementById('main').contains(event.target)) {
      // Move with the passage, and dismiss once its anchor leaves the viewport.
      if (popoverAnchor?.range) positionCodePopover();
      else hidePopover();
    }
  }, true);
  const layoutObserver = new ResizeObserver(positionCodePopover);
  layoutObserver.observe(dom.textDisplay);
  document.getElementById('main').addEventListener('readingchange', positionCodePopover);
  dom.textDisplay.addEventListener('mouseup', () => {
    requestAnimationFrame(() => {
      if (state.currentFile === null || state.navigating) return;
      if (annotationResizer.active) {
        annotationResizer.useSelection();
        return;
      }
      const selection = window.getSelection();
      if (!selection || !selection.rangeCount || selection.isCollapsed || !selection.toString().trim()) {
        return;
      }
      const range = selection.getRangeAt(0);
      if (!dom.textDisplay.contains(range.commonAncestorContainer)) {
        return;
      }
      const start = charOffset(dom.textDisplay, range.startContainer, range.startOffset);
      const end = charOffset(dom.textDisplay, range.endContainer, range.endOffset);
      if (start < 0 || end < 0 || start >= end) {
        return;
      }
      const anchorRange = range.cloneRange();
      anchorRange.setStart(selection.focusNode, selection.focusOffset);
      anchorRange.collapse(true);
      openPopoverForSpan({start, end, text: selection.toString()}, anchorRange.getBoundingClientRect(), anchorRange);
    });
  });

  dom.codeSave.addEventListener('click', () => {
    if (highlightIndex >= 0) chooseSuggestion(highlightIndex);
  });
  dom.codeInput.addEventListener('input', () => {
    renderCodeSuggestions(dom.codeInput.value);
  });
  dom.codeInput.addEventListener('keydown', event => {
    if (event.key === 'Tab' && !dom.codeSuggestions.hidden
        && suggestionItems.length && !suggestionItems[Math.max(0, highlightIndex)].isCreate) {
      event.preventDefault();
      if (highlightIndex < 0) setHighlight(0);
      completeWithHighlight();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      setHighlight(highlightIndex + 1);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setHighlight(highlightIndex < 0 ? suggestionItems.length - 1 : highlightIndex - 1);
    } else if (event.key === 'Enter') {
      event.preventDefault();
      dom.codeSave.click();
    } else if (event.key === 'Escape') {
      hidePopover();
      state.pendingSelection = null;
    }
  });

  document.addEventListener('mousedown', event => {
    if (!dom.popover.hidden && !dom.popover.contains(event.target)) {
      hidePopover();
      state.pendingSelection = null;
    }
    if (!dom.colorPicker.hidden && !dom.colorPicker.contains(event.target)) {
      hideColorPicker();
      state.pickerTarget = null;
    }
  });

  dom.pruneButton.addEventListener('click', handlers.onPruneEmptyCodes);
  dom.addCodeButton.addEventListener('click', handlers.onAddEmptyCode);
  dom.collapseCodesButton.addEventListener('click', () => {
    handlers.onToggleAllCodes(dom.collapseCodesButton.dataset.action === 'expand');
  });
  dom.codeSearch.addEventListener('input', handlers.onSearchCodes);
  dom.codeSearch.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault();
      dom.codeSearch.value = '';
      handlers.onSearchCodes();
    }
  });
}

export function renderFiles(files, onFileSelect) {
  dom.fileList.innerHTML = '';
  if (files.length === 0) {
    dom.fileList.innerHTML = '<div style="color:#aaa;font-size:12px;padding:4px 8px">No .md files in md/</div>';
    return;
  }

  files.forEach(file => {
    // /api/files returns {name, tx, audioAvailable}; tolerate bare strings too.
    const fileName = typeof file === 'string' ? file : file.name;
    const hasTranscript = typeof file === 'string' ? false : Boolean(file.tx);
    const element = document.createElement('div');
    element.className = 'file-item';
    element.textContent = fileName;
    element.classList.toggle('active', fileName === state.currentFile);
    if (hasTranscript) {
      const badge = document.createElement('span');
      badge.className = 'file-audio-badge';
      badge.classList.toggle('file-audio-available', Boolean(file.audioAvailable));
      badge.textContent = '♪';
      badge.title = file.audioAvailable
        ? 'Audio available — open to verify'
        : 'Audio missing on this computer — transcript editing is available';
      badge.setAttribute('aria-label', badge.title);
      element.appendChild(badge);
    }
    element.onclick = () => onFileSelect(fileName);
    dom.fileList.appendChild(element);
  });
}

export function renderText() {
  cancelAnnotationResize();
  const valid = state.annotations
    .filter(annotation => getCodeById(annotation.codeId))
    .filter(annotation => (
      typeof annotation.start === 'number'
      && annotation.start >= 0
      && annotation.end <= state.rawText.length
      && annotation.start < annotation.end
    ))
    .sort((a, b) => a.start - b.start || b.end - a.end || a.id.localeCompare(b.id));

  const lanes = annotationColorLanes(valid);
  // Reserve the same painting space throughout the transcript so continuing
  // colors stay aligned across fragments and wrapped lines.
  dom.textDisplay.style.setProperty('--annotation-stack-height', `${lanes.count * 2}px`);

  // Split annotations into non-overlapping segments at every boundary point
  const cuts = new Set();
  for (const a of valid) { cuts.add(a.start); cuts.add(a.end); }
  const boundaries = [...cuts].sort((a, b) => a - b);

  const groups = [];
  for (let i = 0; i < boundaries.length - 1; i++) {
    const s = boundaries[i], e = boundaries[i + 1];
    const covering = valid.filter(a => a.start <= s && a.end >= e);
    if (covering.length) {
      groups.push({start: s, end: e, annotations: covering});
    }
  }

  let html = '';
  let position = 0;
  for (const group of groups) {
    html += esc(state.rawText.slice(position, group.start));
    const style = buildSpanStyle(group.annotations, lanes);
    const title = group.annotations.map(a => getCodePathLabel(a.codeId)).join('\n');
    const ids = group.annotations.map(a => a.id).join(' ');
    html += `<span class="coded" tabindex="0" role="button" aria-haspopup="dialog" aria-expanded="false" aria-controls="annotation-card" aria-label="${esc(`Inspect coding: ${title.replace(/\n/g, '; ')}`)}" data-id="${esc(String(group.annotations[0].id))}" data-ids="${esc(ids)}" style="${esc(style)}" data-title="${esc(title)}">${esc(state.rawText.slice(group.start, group.end))}</span>`;
    position = group.end;
  }
  html += esc(state.rawText.slice(position));
  dom.textDisplay.innerHTML = html;

}

// --- keeping your place -----------------------------------------------------
// #text-display holds state.rawText verbatim, so a character offset into the
// .md is also a position in this DOM. That offset is what the verification
// view is handed on the way in and hands back on the way out (see place.js).

function textRuns() {
  const walker = document.createTreeWalker(dom.textDisplay, NodeFilter.SHOW_TEXT);
  const runs = [];
  let at = 0;
  let node;
  while ((node = walker.nextNode())) {
    runs.push({node, start: at});
    at += node.length;
  }
  return runs;
}

// The file bar is pinned over the text, so the first line you can actually
// read starts below it.
function readingLineHere() {
  return readingLine(dom.fileBar.hidden ? null : dom.fileBar);
}

// The bottom of the last line a character range occupies. Ranges over a bare
// newline can report no rects at all, hence the fallback.
function bottomOf(node, from, to) {
  const range = document.createRange();
  range.setStart(node, from);
  range.setEnd(node, to);
  const rects = range.getClientRects();
  return rects.length
    ? rects[rects.length - 1].bottom
    : range.getBoundingClientRect().bottom;
}

// The .md offset of the first character still on screen, or null when there is
// no transcript showing.
export function readingOffset() {
  if (dom.textDisplay.hidden || !state.rawText) {
    return null;
  }
  const runs = textRuns();
  if (!runs.length) {
    return null;
  }
  const y = readingLineHere();
  // Text nodes are in document order, so their bottoms only grow down the
  // page: binary search rather than walk a transcript's worth of rects.
  // See PAST: a line that only just reaches the reading line is above it.
  let lo = 0;
  let hi = runs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    const {node} = runs[mid];
    if (bottomOf(node, 0, node.length) > y + PAST) {
      hi = mid;
    } else {
      lo = mid + 1;
    }
  }
  const {node, start} = runs[lo];
  let from = 0;
  let to = node.length;
  while (from < to) {
    const mid = (from + to) >> 1;
    if (bottomOf(node, mid, mid + 1) > y + PAST) {
      to = mid;
    } else {
      from = mid + 1;
    }
  }
  return start + from;
}

// Flash the .md text between two offsets: the words the other view was showing.
export function markOffsets(start, end) {
  const from = locate(start);
  const to = locate(end);
  if (!from || !to) {
    return;
  }
  const range = document.createRange();
  range.setStart(from.node, from.index);
  range.setEnd(to.node, to.index);
  markPlace(lineRects(range));
}

// Put the character at `offset` back on the line it was read from.
// The text node holding a .md offset, and where in it. Past the end of the
// text, the last node's last character.
function locate(offset) {
  if (typeof offset !== 'number' || dom.textDisplay.hidden) {
    return null;
  }
  const runs = textRuns().filter(run => run.node.length > 0);
  if (!runs.length) {
    return null;
  }
  const run = runs.find(r => offset < r.start + r.node.length) || runs[runs.length - 1];
  return {
    node: run.node,
    index: Math.max(0, Math.min(offset - run.start, run.node.length - 1))
  };
}

export function scrollToOffset(offset) {
  const at = locate(offset);
  if (!at) {
    return;
  }
  const range = document.createRange();
  range.setStart(at.node, at.index);
  range.setEnd(at.node, at.index + 1);
  const rect = range.getClientRects()[0] || range.getBoundingClientRect();
  placeAt(rect.top, readingLineHere(), dom.textDisplay);
}

// Match full paths so a parent name also finds its descendants. Keep ancestors
// visible to give each match context, without changing saved collapse choices.
function sidebarSearch(tree, query) {
  const visibleIds = new Set();
  const nameHighlights = new Map();
  for (const match of searchCodes(tree, query)) {
    for (const {node, start, end} of match.path) {
      visibleIds.add(node.id);
      const ranges = nameHighlights.get(node.id) || [];
      for (const [from, to] of match.ranges) {
        if (from < end && to > start) ranges.push([Math.max(from, start) - start, Math.min(to, end) - start]);
      }
      nameHighlights.set(node.id, ranges);
    }
  }
  return {visibleIds, nameHighlights};
}

export function renderSidebar(handlers) {
  dom.pruneButton.hidden = state.currentFile !== null;
  const tree = state.currentFile === null ? state.overviewCodeTree : state.codeTree;
  const branchIds = new Set(tree.filter(node => node.parentId !== null).map(node => node.parentId));
  const expand = !dom.codeSearch.value.trim() && branchIds.size > 0
    && [...branchIds].every(id => state.collapsed.has(id));
  const toggleLabel = expand ? 'Expand all codes' : 'Collapse all codes';
  dom.collapseCodesButton.dataset.action = expand ? 'expand' : 'collapse';
  dom.collapseCodesButton.title = toggleLabel;
  dom.collapseCodesButton.setAttribute('aria-label', toggleLabel);
  dom.collapseCodesButton.disabled = branchIds.size === 0;
  if (state.currentFile === null && state.overviewCodeTree.length === 0) {
    dom.codeList.innerHTML = '<div style="color:#aaa;font-size:12px">No codes found.<br>Add or open md/*.codes.json data.</div>';
    return;
  }

  if (state.currentFile !== null && state.codeTree.length === 0) {
    dom.codeList.innerHTML = '<div style="color:#aaa;font-size:12px">No codes yet.<br>Select text to begin.</div>';
    return;
  }

  dom.codeList.innerHTML = '';
  const query = dom.codeSearch.value.trim().toLowerCase();
  const {visibleIds, nameHighlights} = query ? sidebarSearch(tree, query)
    : {visibleIds: null, nameHighlights: new Map()};
  if (visibleIds && visibleIds.size === 0) {
    const message = document.createElement('div');
    message.className = 'code-search-empty';
    message.setAttribute('role', 'status');
    message.textContent = 'No matching codes.';
    dom.codeList.appendChild(message);
    return;
  }

  if (state.currentFile === null) {
    function overviewSubtreeCount(nodeId) {
      let total = state.overviewCountsById.get(nodeId) || 0;
      getOverviewChildren(nodeId).forEach(child => {
        total += overviewSubtreeCount(child.id);
      });
      return total;
    }

    // Render visible children with gaps indexed into the full sibling list,
    // including codes hidden by search. The leading gap allows insertion at
    // the very beginning.
    function renderOverviewChildren(container, parentId, depth) {
      const children = getOverviewChildren(parentId);
      let rendered = 0;
      children.forEach((child, i) => {
        const childGroup = makeOverviewGroup(child, depth);
        if (!childGroup) {
          return;
        }
        container.appendChild(makeGapDropTarget(rendered === 0 ? 0 : i, parentId, handlers, depth, true));
        container.appendChild(childGroup);
        rendered += 1;
      });
      if (rendered > 0) {
        container.appendChild(makeGapDropTarget(children.length, parentId, handlers, depth, true));
      }
    }

    function makeOverviewGroup(node, depth) {
      if (visibleIds && !visibleIds.has(node.id)) {
        return null;
      }
      const subtreeTotal = overviewSubtreeCount(node.id);

      const group = document.createElement('div');
      group.className = 'code-group';
      group.dataset.codeId = node.id;

      const children = getOverviewChildren(node.id);
      const isRenaming = state.renamingCodeId === node.id;
      const label = document.createElement('div');
      label.className = 'code-label' + (node.id === state.currentOverviewCode ? ' ov-selected' : '')
        + (isRenaming ? ' editing' : '');
      label.style.paddingLeft = `${depth * TREE_INDENT_PX}px`;
      label.style.cursor = 'pointer';
      label.addEventListener('click', () => handlers.onToggleOverview(node.id));
      if (!isRenaming) attachOverviewDragHandlers(label, node, handlers);

      const toggle = document.createElement('span');
      if (children.length > 0) {
        const isCollapsed = !visibleIds && state.collapsed.has(node.id);
        toggle.className = 'tree-toggle';
        toggle.textContent = isCollapsed ? '▸' : '▾';
        if (visibleIds) toggle.title = 'Expanded while searching';
        toggle.addEventListener('click', event => {
          event.stopPropagation();
          if (visibleIds) return;
          if (state.collapsed.has(node.id)) {
            state.collapsed.delete(node.id);
          } else {
            state.collapsed.add(node.id);
          }
          renderSidebar(handlers);
        });
      } else {
        toggle.className = 'tree-spacer';
      }
      label.appendChild(toggle);

      label.appendChild(makeSwatch(node, handlers));
      label.appendChild(makeCodeName(node, depth, handlers, nameHighlights.get(node.id)));

      const countElement = document.createElement('span');
      countElement.className = 'code-count';
      countElement.textContent = subtreeTotal;
      label.appendChild(countElement);
      label.appendChild(makeRenameButton(node, handlers));
      label.appendChild(makeAddChildButton(node, handlers));
      const deleteButton = makeDeleteCodeButton(node, handlers);
      if (deleteButton) label.appendChild(deleteButton);

      group.appendChild(label);
      if (visibleIds || !state.collapsed.has(node.id)) {
        renderOverviewChildren(group, node.id, depth + 1);
      }

      return group;
    }

    renderOverviewChildren(dom.codeList, null, 0);
    return;
  }

  const grouped = annotationsByCodeId();
  const roots = getChildren(null);
  roots.forEach((root, i) => {
    const group = makeCodeGroup(root, grouped, 0, handlers, visibleIds, nameHighlights);
    if (!group) return;
    dom.codeList.appendChild(makeGapDropTarget(i, null, handlers));
    dom.codeList.appendChild(group);
  });
  dom.codeList.appendChild(makeGapDropTarget(roots.length, null, handlers));
}

export function showMainContent() {
  dom.emptyState.hidden = true;
}

export function showOverviewLoading() {
  cancelAnnotationResize();
  dom.emptyState.hidden = true;
  dom.textDisplay.innerHTML = '<div class="instance-list"><p style="color:#aaa;font-size:13px">Loading…</p></div>';
}

export function clearOverview() {
  cancelAnnotationResize();
  dom.textDisplay.innerHTML = '';
  dom.emptyState.hidden = false;
}

export function renderOverviewInstances(codeId, instances, onInstanceClick) {
  dom.emptyState.hidden = true;
  const node = state.overviewById.get(codeId);
  const codeName = node ? node.name : codeId;

  const container = document.createElement('div');
  container.className = 'instance-list';

  const header = document.createElement('div');
  header.className = 'instance-list-header';
  const swatch = document.createElement('span');
  swatch.className = 'code-swatch';
  swatch.style.background = getOverviewEffectiveColor(codeId);
  swatch.style.cursor = 'default';
  header.appendChild(swatch);
  header.appendChild(document.createTextNode(
    `${codeName} — ${instances.length} instance${instances.length !== 1 ? 's' : ''}`
  ));
  container.appendChild(header);

  if (instances.length === 0) {
    const message = document.createElement('p');
    message.style.cssText = 'color:#aaa;font-size:13px';
    message.textContent = 'No instances found.';
    container.appendChild(message);
  } else {
    instances.forEach(instance => {
      const card = document.createElement('div');
      card.className = 'instance-card';

      const meta = document.createElement('div');
      meta.className = 'instance-card-meta';
      const fileSpan = document.createElement('span');
      fileSpan.textContent = instance.file;
      meta.appendChild(fileSpan);
      if (instance.codeId !== codeId) {
        const codeSpan = document.createElement('span');
        codeSpan.className = 'instance-card-code';
        const descendant = state.overviewById.get(instance.codeId);
        codeSpan.textContent = descendant ? descendant.name : instance.codeId;
        meta.appendChild(codeSpan);
      }
      card.appendChild(meta);

      const textElement = document.createElement('div');
      textElement.className = 'instance-card-text';
      const context = instance.context || instance.text;
      const contextStart = instance.contextStart ?? 0;
      const contextEnd = instance.contextEnd ?? context.length;
      textElement.appendChild(document.createTextNode(context.slice(0, contextStart)));
      const mark = document.createElement('mark');
      mark.textContent = context.slice(contextStart, contextEnd);
      textElement.appendChild(mark);
      textElement.appendChild(document.createTextNode(context.slice(contextEnd)));
      card.appendChild(textElement);

      card.addEventListener('click', () => onInstanceClick(instance));
      container.appendChild(card);
    });
  }

  dom.textDisplay.innerHTML = '';
  dom.textDisplay.appendChild(container);
}

export function openColorPicker(codeId, anchorElement, onApplyColor) {
  const overview = state.currentFile === null;
  const node = overview ? state.overviewById.get(codeId) : getCodeById(codeId);
  const effectiveColor = overview ? getOverviewEffectiveColor : getEffectiveColor;
  if (!node || node.parentId !== null) {
    return;
  }

  state.pickerTarget = codeId;
  dom.cpTitle.textContent = node.name;

  const roots = overview ? getOverviewChildren(null) : getChildren(null);
  const usage = {};
  roots.filter(root => root.id !== codeId).forEach(root => {
    const color = effectiveColor(root.id);
    usage[color] = (usage[color] || 0) + 1;
  });

  const current = effectiveColor(codeId);
  dom.cpGrid.innerHTML = '';
  PALETTE.forEach(color => {
    const element = document.createElement('div');
    element.className = 'cp-color' + (color === current ? ' active' : '');
    element.style.background = color;
    if (usage[color]) {
      element.innerHTML = `<span class="cp-badge">${usage[color]}</span>`;
    }
    element.title = usage[color]
      ? `Shared by ${usage[color]} other code${usage[color] > 1 ? 's' : ''}`
      : '';
    element.addEventListener('click', () => onApplyColor(color));
    dom.cpGrid.appendChild(element);
  });

  const sameColor = roots.filter(root => root.id !== codeId && effectiveColor(root.id) === current);
  if (sameColor.length > 0) {
    dom.cpBulk.hidden = false;
    dom.cpBulkCheck.checked = false;
    dom.cpBulkText.textContent = `Also change ${sameColor.length} code${sameColor.length > 1 ? 's' : ''} with same color`;
  } else {
    dom.cpBulk.hidden = true;
  }

  dom.colorPicker.hidden = false;
  const rect = anchorElement.getBoundingClientRect();
  const pickerWidth = dom.colorPicker.offsetWidth || 148;
  const pickerHeight = dom.colorPicker.offsetHeight || 110;
  let left = rect.right + 8;
  if (left + pickerWidth > window.innerWidth - 8) {
    left = rect.left - pickerWidth - 8;
  }
  let top = rect.top - 4;
  if (top + pickerHeight > window.innerHeight - 8) {
    top = window.innerHeight - pickerHeight - 8;
  }
  dom.colorPicker.style.left = `${Math.max(8, left)}px`;
  dom.colorPicker.style.top = `${Math.max(8, top)}px`;
}

export function shouldApplyColorInBulk() {
  return !dom.cpBulk.hidden && dom.cpBulkCheck.checked;
}

export function getCodeInputValue() {
  return dom.codeInput.value.trim();
}

export function hidePopover() {
  dom.popover.hidden = true;
  popoverAnchor = null;
  state.pendingSelection = null;
  CSS.highlights?.delete('pending-code');
  dom.codeSuggestions.hidden = true;
  dom.codeSuggestions.innerHTML = '';
  suggestionItems = [];
  highlightIndex = -1;
}

export function hideColorPicker() {
  dom.colorPicker.hidden = true;
}

export function showSaveStatus(message, error = false) {
  dom.saveStatus.textContent = message;
  dom.saveStatus.className = error ? 'error' : '';
  clearTimeout(dom.saveStatus._timer);
  dom.saveStatus._timer = setTimeout(() => {
    dom.saveStatus.textContent = '';
    dom.saveStatus.className = '';
  }, 2000);
}
