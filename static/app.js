import {apiFetch, apiPost} from './api.js';
import {initReading} from './reading.js';
import {initSession} from './session.js';
import {toTop} from './place.js';
import {closeVerify, initVerify, isVerifying, openVerify} from './verify.js';
import {
  compareColors,
  compareNodes,
  state,
  getChildren,
  getCodeById,
  getCodePathLabel,
  getEffectiveColor,
  getOverviewChildren,
  getOverviewEffectiveColor,
  hashColor,
  nextOrder,
  nextOverviewOrder,
  normalizeCodesData,
  overviewIsDescendant,
  sanitizeCodeTree,
  uid
} from './state.js';
import {
  bindUiEvents,
  cancelAnnotationResize,
  clearOverview,
  getCodeInputValue,
  hideColorPicker,
  hidePopover,
  markOffsets,
  openColorPicker,
  readingOffset,
  renderFiles,
  renderOverviewInstances,
  renderSidebar,
  renderText,
  scrollToOffset,
  shouldApplyColorInBulk,
  showMainContent,
  showOverviewLoading,
  showSaveStatus
} from './render.js';

const sidebarHandlers = {
  onDeleteAnnotation: deleteCode,
  onDeleteCodeNode: deleteCodeNode,
  onToggleOverview: showOverviewInstances,
  onOpenColorPicker: openColorPickerForCode,
  onReparentCode: reparentCode,
  onReparentOverviewCode: reparentOverviewCode,
  onReorderOverviewCode: (id, index, parentId) => moveCodeToGap(id, index, parentId, true),
  onStartRenameCode: startRenameCode,
  onCommitRenameCode: commitRenameCode,
  onCancelRenameCode: cancelRenameCode,
  onReorderCode: moveCodeToGap,
  onAddChildCode: addEmptyCode
};

// Files that have a .tx.json, so the audio-linked verification view is
// available for them.
const transcriptFiles = new Set();
const availableFiles = new Set();
let fileLoad = 0;

const fileBar = document.getElementById('file-bar');
const fileBarName = document.getElementById('file-bar-name');
const verifyButton = document.getElementById('verify-btn');
const textDisplay = document.getElementById('text-display');
const verifyView = document.getElementById('verify-view');
initReading();

// The .md span the verification view left off on, waiting for the coding view
// to be rendered again so it can be scrolled to and marked.
let placeToRestore = null;

initVerify(verifyView, async (dirty, span) => {
  textDisplay.hidden = false;
  fileBar.hidden = !state.currentFile;
  placeToRestore = span;
  // A correction rewrites the .md, so the coding view is reloaded rather than
  // left showing offsets that no longer describe the file. loadFile restores
  // the place itself, once the new text is on the page.
  if (dirty && state.currentFile) {
    await loadFile(state.currentFile);
  } else {
    restorePlace();
  }
});

function restorePlace() {
  if (placeToRestore) {
    const [start, end] = placeToRestore;
    placeToRestore = null;
    scrollToOffset(start);
    markOffsets(start, end);
  }
}

function resumeCoding() {
  // New annotations are appended and saved in insertion order. Use the last
  // one still rendered, since stale or invalid ranges may have been skipped.
  const annotation = state.annotations.findLast(entry => (
    textDisplay.querySelector(`.coded[data-ids~="${CSS.escape(entry.id)}"]`)
  ));
  if (annotation) {
    scrollToOffset(annotation.start);
    markOffsets(annotation.start, annotation.end);
  } else {
    toTop();
  }
}

verifyButton.onclick = async () => {
  if (!state.currentFile || isVerifying()) {
    return;
  }
  cancelAnnotationResize();
  // Read the place before hiding the text: a hidden element has no boxes.
  const at = readingOffset();
  textDisplay.hidden = true;
  fileBar.hidden = true;
  await openVerify(state.currentFile, at);
};

bindUiEvents({
  onApplyCode: applyCode,
  onDeleteAnnotation: deleteCode,
  onMoveAnnotation: moveAnnotation,
  onResizeAnnotation: resizeAnnotation,
  onPruneEmptyCodes: pruneEmptyCodes,
  onAddEmptyCode: () => addEmptyCode(),
  onToggleAllCodes: expand => {
    const tree = state.currentFile === null ? state.overviewCodeTree : state.codeTree;
    tree.forEach(node => {
      if (expand) state.collapsed.delete(node.id);
      else if (node.parentId !== null) state.collapsed.add(node.parentId);
    });
    // Search temporarily expands matches, so clear it to show the collapsed tree.
    document.getElementById('code-search').value = '';
    renderSidebar(sidebarHandlers);
    document.getElementById('codes-panel').scrollTop = 0;
  },
  onSearchCodes: () => renderSidebar(sidebarHandlers),
  onNavigateToInstance: navigateToInstance
});

// Reveal and briefly highlight an annotation's instance in the sidebar. Called
// when a coded span with a single code is clicked.
function navigateToInstance(annotationId) {
  const annot = state.annotations.find(a => a.id === annotationId);
  if (!annot) {
    return;
  }
  document.getElementById('code-search').value = '';
  // Expand the code and any collapsed ancestors so the instance is rendered.
  let node = getCodeById(annot.codeId);
  while (node) {
    state.collapsed.delete(node.id);
    node = node.parentId ? getCodeById(node.parentId) : null;
  }
  renderSidebar(sidebarHandlers);
  const el = document.querySelector(`.code-instance[data-id="${annotationId}"]`);
  if (el) {
    el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    el.classList.add('instance-flash');
    setTimeout(() => el.classList.remove('instance-flash'), 1500);
  }
}

function noteTranscripts(files) {
  transcriptFiles.clear();
  availableFiles.clear();
  for (const file of files) {
    availableFiles.add(typeof file === 'string' ? file : file.name);
    if (file && typeof file === 'object' && file.tx) {
      transcriptFiles.add(file.name);
    }
  }
  return files;
}

async function loadFiles() {
  renderFiles(noteTranscripts(await apiFetch('/api/files')), loadFile);
}

async function loadOverview() {
  const overviewData = await apiFetch('/api/codes-overview');
  const rawCodes = Array.isArray(overviewData?.codes) ? overviewData.codes : [];
  state.overviewCountsById = new Map(
    rawCodes.filter(code => code.id && Number.isFinite(code.count)).map(code => [code.id, code.count])
  );
  state.overviewCodeTree = sanitizeCodeTree(
    Array.isArray(overviewData?.codeTree) ? overviewData.codeTree : []
  );
  state.overviewById = new Map(state.overviewCodeTree.map(node => [node.id, node]));
  state.overviewLoaded = true;
  if (!state.currentFile || !state.renamingCodeId) {
    renderSidebar(sidebarHandlers);
  }
}

async function loadFile(name, {resume = true, updateUrl = true} = {}) {
  cancelAnnotationResize();
  const requestedName = name;
  // Keep existing bookmarks usable after removing the audio-preparation suffix.
  if (!availableFiles.has(name) && name.endsWith('.16k.md')) {
    const canonicalName = name.slice(0, -'.16k.md'.length) + '.md';
    if (availableFiles.has(canonicalName)) name = canonicalName;
  }
  const request = ++fileLoad;
  const previous = state.currentFile;
  // Stop playback and invalidate pending verification requests immediately.
  // This navigation owns the next render; an exit callback would reload again.
  closeVerify({keepPlace: false, notify: false});
  verifyButton.disabled = true;
  state.currentFile = name;
  state.currentOverviewCode = null;

  const [fileData, codesData, files] = await Promise.all([
    apiFetch(`/api/file?name=${encodeURIComponent(name)}`),
    apiFetch(`/api/codes?name=${encodeURIComponent(name)}`),
    apiFetch('/api/files')
  ]);
  if (request !== fileLoad) return;

  state.rawText = fileData.content;
  if (typeof codesData?.rev === 'string') codeRevs.set(name, codesData.rev);
  codeLoads.set(name, (codeLoads.get(name) || 0) + 1);
  const normalized = normalizeCodesData(codesData);
  state.annotations = normalized.annotations;
  state.codeTree = normalized.codeTree;
  syncedCodeTree = structuredClone(normalized.codeTree);
  treeLoads += 1;
  state.collapsed = new Set([...state.collapsed].filter(codeId => getCodeById(codeId)));

  renderFiles(noteTranscripts(files), loadFile);
  showMainContent();
  textDisplay.hidden = false;
  fileBar.hidden = false;
  fileBarName.textContent = name;
  verifyButton.hidden = !transcriptFiles.has(name);
  verifyButton.disabled = false;
  renderText();
  renderSidebar(sidebarHandlers);
  if (updateUrl || requestedName !== name) {
    const url = new URL(window.location.href);
    if (url.searchParams.get('file') !== name) {
      url.searchParams.set('file', name);
      if (requestedName !== name) window.history.replaceState(null, '', url);
      else window.history.pushState(null, '', url);
    }
  }
  if (name === previous && placeToRestore) {
    restorePlace();     // same transcript: the verification view's place
  } else {
    placeToRestore = null;
    if (resume) resumeCoding();
    else toTop();
  }
}

async function restoreFromUrl() {
  const name = new URL(window.location.href).searchParams.get('file');
  if (name) {
    const request = fileLoad + 1;
    try {
      await loadFile(name, {updateUrl: false});
    } catch (error) {
      // A newer selection may have completed while this request failed.
      if (request !== fileLoad) return;
      await showFileOverview();
      showSaveStatus(`could not load ${name}`, true);
    }
  } else {
    await showFileOverview();
  }
}

async function showFileOverview() {
  cancelAnnotationResize();
  ++fileLoad; // Ignore any transcript request still in flight.
  closeVerify({keepPlace: false, notify: false});
  placeToRestore = null;
  state.currentFile = null;
  state.currentOverviewCode = null;
  state.renamingCodeId = null;
  state.rawText = '';
  state.annotations = [];
  state.codeTree = [];
  textDisplay.hidden = false;
  fileBar.hidden = true;
  clearOverview();
  toTop();
  const results = await Promise.allSettled([loadFiles(), loadOverview()]);
  if (results.some(result => result.status === 'rejected')) {
    showSaveStatus('load failed', true);
  }
}

window.addEventListener('popstate', restoreFromUrl);

async function onOverviewInstanceClick(instance) {
  state.navigating = true;
  await loadFile(instance.file, {resume: false});
  const element = document.querySelector(`.coded[data-ids~="${CSS.escape(String(instance.annotId))}"]`);
  if (element) {
    element.scrollIntoView({behavior: 'smooth', block: 'center'});
    const previousOutline = element.style.outline;
    element.style.outline = '2px solid #1a73e8';
    element.style.outlineOffset = '2px';
    setTimeout(() => {
      element.style.outline = previousOutline;
      element.style.outlineOffset = '';
      state.navigating = false;
    }, 1500);
  } else {
    state.navigating = false;
  }
}

// Fetch and render the instances pane for a code. Used both on click and to
// refresh the pane after a tree change so its color/contents stay in sync with
// the sidebar.
async function renderOverviewInstancesFor(codeId) {
  const instances = await apiFetch(`/api/instances?codeId=${encodeURIComponent(codeId)}`);
  if (state.currentOverviewCode !== codeId) {
    return; // selection changed while fetching
  }
  renderOverviewInstances(codeId, instances, onOverviewInstanceClick);
}

async function showOverviewInstances(codeId) {
  if (state.currentOverviewCode === codeId) {
    state.currentOverviewCode = null;
    clearOverview();
    renderSidebar(sidebarHandlers);
    return;
  }

  state.currentOverviewCode = codeId;
  renderSidebar(sidebarHandlers);
  showOverviewLoading();
  await renderOverviewInstancesFor(codeId);
}

function openColorPickerForCode(codeId, anchorElement) {
  openColorPicker(codeId, anchorElement, applyColorFromPicker);
}

function applyColorFromPicker(color) {
  const overview = state.currentFile === null;
  const tree = overview ? state.overviewCodeTree : state.codeTree;
  const effectiveColor = overview ? getOverviewEffectiveColor : getEffectiveColor;
  const roots = overview ? getOverviewChildren(null) : getChildren(null);
  const current = effectiveColor(state.pickerTarget);
  const bulk = shouldApplyColorInBulk();
  const moved = roots.filter(node => effectiveColor(node.id) !== color
    && (bulk ? effectiveColor(node.id) === current : node.id === state.pickerTarget));

  hideColorPicker();
  state.pickerTarget = null;
  if (!moved.length) return;

  const movedIds = new Set(moved.map(node => node.id));
  const remaining = roots.filter(node => !movedIds.has(node.id));
  const lastMatch = remaining.findLastIndex(node => effectiveColor(node.id) === color);
  // Join the end of the destination group in its current displayed order.
  // If this color is new, use its normal palette position. Other roots keep
  // their relative positions, including any manual placements across colors.
  let index = lastMatch + 1;
  if (lastMatch < 0) {
    index = remaining.findIndex(node => compareColors(effectiveColor(node.id), color) > 0);
    if (index < 0) index = remaining.length;
  }
  const reordered = [...remaining.slice(0, index), ...moved, ...remaining.slice(index)];
  const orders = new Map(reordered.map((node, order) => [node.id, order]));
  const newTree = tree.map(node => orders.has(node.id)
    ? {...node, order: orders.get(node.id), ...(movedIds.has(node.id) ? {color} : {})}
    : node);
  saveCodeEdits(newTree);
}

function saveCodeEdits(newTree) {
  if (state.currentFile === null) {
    saveOverviewTree(newTree);
    return;
  }
  state.codeTree = newTree;
  renderText();
  renderSidebar(sidebarHandlers);
  saveCodes();
}

function reparentCode(codeId, newParentId) {
  const node = getCodeById(codeId);
  if (!node) {
    return;
  }
  if (newParentId === node.id) {
    return;
  }
  if (newParentId && isDescendant(newParentId, codeId)) {
    return;
  }
  if (node.parentId === newParentId) {
    return;
  }

  const currentColor = getEffectiveColor(codeId);
  state.codeTree = state.codeTree.map(entry => {
    if (entry.id !== codeId) {
      return entry;
    }
    return {
      ...entry,
      parentId: newParentId,
      color: newParentId === null ? currentColor : undefined,
      order: nextOrder(newParentId)
    };
  });

  renderText();
  renderSidebar(sidebarHandlers);
  saveCodes();
}

function reparentOverviewCode(codeId, newParentId) {
  const node = state.overviewById.get(codeId);
  if (!node) {
    return;
  }
  const targetParent = newParentId || null;
  if (targetParent === node.id || (node.parentId || null) === targetParent) {
    return;
  }
  if (targetParent && overviewIsDescendant(targetParent, codeId)) {
    return;
  }

  // Promoting to top level keeps the current (inherited) color; nesting drops it.
  const currentColor = getOverviewEffectiveColor(codeId);
  const newTree = state.overviewCodeTree.map(entry => {
    if (entry.id !== codeId) {
      return entry;
    }
    const updated = {...entry, parentId: targetParent, order: nextOverviewOrder(targetParent)};
    if (targetParent === null) {
      updated.color = currentColor;
    } else {
      delete updated.color;
    }
    return updated;
  });

  saveOverviewTree(newTree);
}

async function saveOverviewTree(newTree) {
  try {
    const response = await apiPost('/api/save-codes',
      {codeTree: newTree, codeTreeBase: state.overviewCodeTree});
    const data = await response.json();
    if (!response.ok || !data.ok) {
      throw new Error(data.error || 'save failed');
    }
    showSaveStatus('saved');
    await loadOverview();
    // Keep the open instances pane in sync with the refreshed tree (its swatch
    // color and which annotations fall under the code can both change).
    if (state.currentOverviewCode) {
      if (state.overviewById.has(state.currentOverviewCode)) {
        await renderOverviewInstancesFor(state.currentOverviewCode);
      } else {
        state.currentOverviewCode = null;
        clearOverview();
      }
    }
  } catch {
    showSaveStatus('save failed', true);
    renderSidebar(sidebarHandlers);
  }
}

function startRenameCode(codeId) {
  const tree = state.currentFile === null ? state.overviewCodeTree : state.codeTree;
  if (!tree.some(node => node.id === codeId)) {
    return;
  }
  hideColorPicker();
  state.pickerTarget = null;
  state.renamingCodeId = codeId;
  renderSidebar(sidebarHandlers);
}

function cancelRenameCode(codeId) {
  if (state.renamingCodeId !== codeId) {
    return;
  }
  state.renamingCodeId = null;
  renderSidebar(sidebarHandlers);
}

function commitRenameCode(codeId, rawName) {
  const tree = state.currentFile === null ? state.overviewCodeTree : state.codeTree;
  const node = tree.find(entry => entry.id === codeId);
  if (!node) {
    return;
  }

  const name = rawName.trim();
  if (!name) {
    showSaveStatus('code name required', true);
    renderSidebar(sidebarHandlers);
    return;
  }

  const duplicateSibling = tree.some(entry => (
    entry.id !== codeId
    && entry.parentId === node.parentId
    && entry.name.toLowerCase() === name.toLowerCase()
  ));

  if (duplicateSibling) {
    showSaveStatus('another code at this level already has that name', true);
    renderSidebar(sidebarHandlers);
    return;
  }

  state.renamingCodeId = null;
  if (name === node.name) {
    renderSidebar(sidebarHandlers);
    return;
  }

  const newTree = tree.map(entry => (
    entry.id === codeId ? {...entry, name} : entry
  ));

  saveCodeEdits(newTree);
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

async function addEmptyCode(parentId = null) {
  const overview = state.currentFile === null;
  const tree = overview ? state.overviewCodeTree : state.codeTree;
  const parent = tree.find(node => node.id === parentId);
  if (parentId !== null && !parent) return;
  const name = prompt(parent ? `New child code under "${parent.name}":` : 'New code name:');
  if (!name || !name.trim()) return;
  const trimmed = name.trim();
  const duplicate = tree.some(n => n.name.toLowerCase() === trimmed.toLowerCase() && n.parentId === parentId);
  if (duplicate) {
    showSaveStatus(parent ? 'A child code with that name already exists here' : 'A top-level code with that name already exists', true);
    return;
  }
  const node = {
    id: uid(),
    name: trimmed,
    parentId,
    order: overview ? nextOverviewOrder(parentId) : nextOrder(parentId)
  };
  if (parentId === null) node.color = hashColor(trimmed);

  function revealNewCode() {
    document.getElementById('code-search').value = '';
    let ancestor = parent;
    while (ancestor) {
      state.collapsed.delete(ancestor.id);
      ancestor = tree.find(entry => entry.id === ancestor.parentId);
    }
    renderSidebar(sidebarHandlers);
    const el = document.querySelector(`.code-group[data-code-id="${node.id}"]`);
    if (el) el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }

  if (!overview) {
    state.codeTree = [...state.codeTree, node];
    revealNewCode();
    saveCodes();
  } else {
    try {
      const response = await apiPost('/api/add-code', node);
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error('save failed');
      await loadOverview();
      revealNewCode();
      showSaveStatus('saved');
    } catch {
      showSaveStatus('save failed', true);
    }
  }
}

// Find-or-create a code from the combobox. When the user picks an existing
// suggestion we get its id directly; otherwise we resolve the typed text,
// supporting "Parent / Child" path notation to find or create nested codes.
function resolveCodeFromInput() {
  const rawName = getCodeInputValue();
  if (!rawName) {
    return null;
  }

  const segments = rawName.split('/').map(s => s.trim()).filter(Boolean);
  if (segments.length === 0) {
    return null;
  }

  // Exact full-path match wins (case-insensitive).
  const fullPath = segments.join(' / ').toLowerCase();
  const exactPath = state.codeTree.find(n => getCodePathLabel(n.id).toLowerCase() === fullPath);
  if (exactPath) {
    return exactPath;
  }

  // A bare name that uniquely matches an existing code reuses it.
  if (segments.length === 1) {
    const sameName = state.codeTree.filter(n => n.name.toLowerCase() === segments[0].toLowerCase());
    if (sameName.length === 1) {
      return sameName[0];
    }
  }

  // Walk the path, creating any missing levels.
  let parentId = null;
  let node = null;
  for (const segment of segments) {
    node = state.codeTree.find(entry =>
      entry.parentId === parentId && entry.name.toLowerCase() === segment.toLowerCase()
    );
    if (!node) {
      node = {
        id: uid(),
        name: segment,
        parentId,
        order: nextOrder(parentId)
      };
      if (parentId === null) {
        node.color = hashColor(segment);
      }
      state.codeTree = [...state.codeTree, node];
    }
    parentId = node.id;
  }
  return node;
}

function applyCode(explicitCodeId = null) {
  if (!state.pendingSelection) {
    return;
  }

  const node = explicitCodeId ? getCodeById(explicitCodeId) : resolveCodeFromInput();
  if (!node) {
    return;
  }

  state.annotations.push({
    id: uid(),
    codeId: node.id,
    text: state.pendingSelection.text,
    start: state.pendingSelection.start,
    end: state.pendingSelection.end
  });

  hidePopover();
  state.pendingSelection = null;
  window.getSelection().removeAllRanges();
  renderText();
  // Expand any collapsed ancestors so the code is visible
  let ancestor = node.parentId ? getCodeById(node.parentId) : null;
  while (ancestor) {
    state.collapsed.delete(ancestor.id);
    ancestor = ancestor.parentId ? getCodeById(ancestor.parentId) : null;
  }
  renderSidebar(sidebarHandlers);
  const el = document.querySelector(`.code-group[data-code-id="${node.id}"]`);
  if (el) el.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  saveCodes();
}

function deleteCode(annotationId) {
  const index = state.annotations.findIndex(annotation => annotation.id === annotationId);
  if (index < 0) return null;
  const removed = {...state.annotations[index]};
  const file = state.currentFile;
  const rawText = state.rawText;
  state.annotations = state.annotations.filter(annotation => annotation.id !== annotationId);
  renderText();
  renderSidebar(sidebarHandlers);
  saveCodes();
  // Restore this instance in insertion order, preserving its ID and metadata.
  // Never apply an old undo to a different file or revised transcript.
  return () => {
    if (state.currentFile !== file || state.rawText !== rawText || !getCodeById(removed.codeId)
        || state.annotations.some(annotation => annotation.id === removed.id)) return false;
    state.annotations.splice(Math.min(index, state.annotations.length), 0, removed);
    renderText();
    renderSidebar(sidebarHandlers);
    saveCodes();
    return true;
  };
}

function resizeAnnotation(annotationId, start, end) {
  const annotation = state.annotations.find(entry => entry.id === annotationId);
  if (!state.currentFile || !annotation || !Number.isInteger(start) || !Number.isInteger(end)
      || start < 0 || end > state.rawText.length || start >= end
      || !state.rawText.slice(start, end).trim()
      || (annotation.start === start && annotation.end === end)) return;
  state.annotations = state.annotations.map(entry => entry.id === annotationId
    ? {...entry, start, end, text: state.rawText.slice(start, end)} : entry);
  renderText();
  renderSidebar(sidebarHandlers);
  saveCodes();
}

function moveCodeToGap(draggedId, targetIndex, parentId, overview = false) {
  const node = overview ? state.overviewById.get(draggedId) : getCodeById(draggedId);
  const descendant = overview ? overviewIsDescendant : isDescendant;
  if (!node || parentId === draggedId || (parentId && descendant(parentId, draggedId))) return;

  const siblings = overview ? getOverviewChildren(parentId) : getChildren(parentId);
  const currentIndex = siblings.findIndex(n => n.id === draggedId);
  if (currentIndex >= 0 && (targetIndex === currentIndex || targetIndex === currentIndex + 1)) return;

  const moved = {...node, parentId};
  if (parentId === null) {
    moved.color = overview ? getOverviewEffectiveColor(draggedId) : getEffectiveColor(draggedId);
  } else {
    delete moved.color;
  }
  const withoutDragged = siblings.filter(n => n.id !== draggedId);
  const insertAt = currentIndex >= 0 && targetIndex > currentIndex ? targetIndex - 1 : targetIndex;
  const reordered = [
    ...withoutDragged.slice(0, insertAt),
    moved,
    ...withoutDragged.slice(insertAt)
  ];
  const orderById = new Map(reordered.map((entry, index) => [entry.id, index]));
  const tree = overview ? state.overviewCodeTree : state.codeTree;
  const newTree = tree.map(entry => {
    if (!orderById.has(entry.id)) return entry;
    return {...(entry.id === draggedId ? moved : entry), order: orderById.get(entry.id), manualOrder: true};
  });
  state.collapsed.delete(parentId);
  if (overview) {
    saveOverviewTree(newTree);
    return;
  }
  state.codeTree = newTree;
  renderText();
  renderSidebar(sidebarHandlers);
  saveCodes();
}

// Move an annotation via the searchable code popover: an existing code is passed
// by id, otherwise the typed text is resolved/created (supports path notation).
function moveAnnotation(annotationId, explicitCodeId) {
  const node = explicitCodeId ? getCodeById(explicitCodeId) : resolveCodeFromInput();
  if (!node) {
    return;
  }
  hidePopover();
  reassignAnnotation(annotationId, node.id);
}

function reassignAnnotation(annotationId, newCodeId) {
  if (!getCodeById(newCodeId)) {
    return;
  }
  state.annotations = state.annotations.map(annotation =>
    annotation.id === annotationId ? {...annotation, codeId: newCodeId} : annotation
  );
  renderText();
  renderSidebar(sidebarHandlers);
  saveCodes();
}

let codeSaveQueue = Promise.resolve();

// The version of each transcript's annotations this tab last loaded or saved.
// Sent with every save; if someone else saved in between, the server refuses
// (409) rather than let this tab's copy replace their work.
const codeRevs = new Map();
// Bumped each time a transcript is (re)loaded. A save snapshotted before a
// reload describes the old state; sending it with the new rev would undo
// whatever the reload brought in, so it is dropped instead.
const codeLoads = new Map();
// The code tree as this tab last synced it with the server. Sent with each
// save as the merge base: a code still identical to its base is not this
// tab's edit, so the server keeps whatever others have done to it since
// (renamed, moved, deleted) instead of reverting it to this tab's old copy.
let syncedCodeTree = null;
let treeLoads = 0;

class SaveConflict extends Error {}

async function saveCodes() {
  if (!state.currentFile) {
    return;
  }

  const file = state.currentFile;
  // A quick Undo can run before the removal request finishes. Snapshot each
  // edit and write in order so the last saved state matches the visible state.
  const payload = structuredClone({
    version: 2,
    codeTree: state.codeTree.slice().sort(compareNodes),
    annotations: state.annotations,
    ...(syncedCodeTree ? {codeTreeBase: syncedCodeTree} : {})
  });
  const loaded = codeLoads.get(file);
  const treeLoaded = treeLoads;
  const request = codeSaveQueue.then(async () => {
    if (codeLoads.get(file) !== loaded) return;
    // Read at send time: the save queued before this one may have moved it.
    const rev = codeRevs.get(file);
    const response = await apiPost(`/api/codes?name=${encodeURIComponent(file)}`,
      rev === undefined ? payload : {...payload, rev});
    if (response.status === 409) throw new SaveConflict();
    if (!response.ok) throw new Error(response.status);
    const data = await response.json().catch(() => ({}));
    if (typeof data.rev === 'string') codeRevs.set(file, data.rev);
    // What this tab sent is now what it last synced — unless a fresher tree
    // was loaded while the save was in flight.
    if (treeLoads === treeLoaded) syncedCodeTree = payload.codeTree;
  });
  codeSaveQueue = request.catch(() => {});
  try {
    await request;
    if (state.currentFile === file) showSaveStatus('saved');
    loadOverview();
  } catch (error) {
    if (state.currentFile !== file) return;
    if (error instanceof SaveConflict) {
      await reloadAfterConflict(file);
    } else {
      showSaveStatus('save failed', true);
    }
  }
}

// Someone else saved this transcript's codes after this tab loaded them.
// Show theirs, keep the reader's place, and say what happened.
async function reloadAfterConflict(file) {
  codeSaveQueue = codeSaveQueue.then(async () => {
    const offset = readingOffset();
    if (offset != null) placeToRestore = [offset, offset];
    await loadFile(file, {resume: false, updateUrl: false});
    showSaveStatus('changed by someone else — reloaded; redo your last change', true);
  }).catch(() => showSaveStatus('save failed', true));
  await codeSaveQueue;
}

async function deleteCodeNode(codeId) {
  state.renamingCodeId = null;
  let data;
  try {
    const response = await apiPost(`/api/delete-code?id=${encodeURIComponent(codeId)}`);
    data = await response.json();
  } catch {
    showSaveStatus('cannot delete', true);
    return;
  }
  if (!data.ok) {
    showSaveStatus(data.error || 'cannot delete', true);
    return;
  }

  const newTree = sanitizeCodeTree(data.codeTree);
  const validIds = new Set(newTree.map(node => node.id));
  if (state.currentFile === null) {
    state.overviewCodeTree = newTree;
    state.overviewById = new Map(newTree.map(node => [node.id, node]));
    if (state.currentOverviewCode && !validIds.has(state.currentOverviewCode)) {
      state.currentOverviewCode = null;
      clearOverview();
    }
    renderSidebar(sidebarHandlers);
    await loadOverview();
    if (state.currentOverviewCode) await renderOverviewInstancesFor(state.currentOverviewCode);
  } else {
    state.codeTree = newTree;
    syncedCodeTree = structuredClone(newTree);  // the server's tree, after its delete/prune
    state.annotations = state.annotations.filter(annotation => validIds.has(annotation.codeId));
    renderText();
    renderSidebar(sidebarHandlers);
    loadOverview();
  }
  showSaveStatus('code deleted');
}

async function pruneEmptyCodes() {
  state.renamingCodeId = null;
  let data;
  try {
    const response = await apiPost('/api/prune-codes');
    if (!response.ok) throw new Error(response.status);
    data = await response.json();
  } catch {
    showSaveStatus('prune failed', true);
    return;
  }
  const newTree = sanitizeCodeTree(Array.isArray(data.codeTree) ? data.codeTree : []);
  const validIds = new Set(newTree.map(node => node.id));
  state.codeTree = newTree;
  syncedCodeTree = structuredClone(newTree);  // the server's tree, after its delete/prune
  state.annotations = state.annotations.filter(annotation => validIds.has(annotation.codeId));
  renderText();
  renderSidebar(sidebarHandlers);
  loadOverview();
  showSaveStatus(
    data.pruned === 0 ? 'nothing to prune' : `pruned ${data.pruned} empty code${data.pruned !== 1 ? 's' : ''}`
  );
}

async function init() {
  initSession();
  const results = await Promise.allSettled([loadFiles(), loadOverview()]);
  if (results.some(result => result.status === 'rejected')) {
    showSaveStatus('load failed', true);
  }
  // Do not override a file the user selected while startup requests ran.
  if (fileLoad === 0 && new URL(window.location.href).searchParams.get('file')) {
    await restoreFromUrl();
  }
}

init();

// Keep Files compact and independently scrollable; reserve most sidebar space
// for Codes even when Files is dragged larger or the window becomes shorter.
(function () {
  const sidebar = document.getElementById('sidebar');
  const files = document.getElementById('files-panel');
  const handle = document.getElementById('files-resize-handle');
  const footer = document.getElementById('sidebar-footer');
  const sessionBar = document.getElementById('session-bar');
  const defaultHeight = 140;
  let preferredHeight = defaultHeight;

  function limits() {
    const available = Math.max(0, sidebar.clientHeight - footer.offsetHeight
      - handle.offsetHeight - (sessionBar ? sessionBar.offsetHeight : 0));
    const max = Math.floor(available * 0.4);
    return {min: Math.min(64, max), max};
  }

  function updateHeight() {
    const {min, max} = limits();
    const height = Math.max(min, Math.min(max, preferredHeight));
    files.style.height = `${height}px`;
    files.style.maxHeight = `${max}px`;
    handle.setAttribute('aria-valuemin', min);
    handle.setAttribute('aria-valuemax', max);
    handle.setAttribute('aria-valuenow', height);
  }

  function resize(height) {
    const {min, max} = limits();
    preferredHeight = Math.max(min, Math.min(max, height));
    updateHeight();
  }

  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = files.offsetHeight;
    const previousCursor = document.body.style.cursor;
    const previousSelection = document.body.style.userSelect;
    handle.classList.add('dragging');
    document.body.style.cursor = 'row-resize';
    document.body.style.userSelect = 'none';

    function onMove(event) {
      resize(startHeight + event.clientY - startY);
    }
    function onUp() {
      handle.classList.remove('dragging');
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousSelection;
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
      document.removeEventListener('pointercancel', onUp);
      window.removeEventListener('blur', onUp);
    }
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
    document.addEventListener('pointercancel', onUp);
    window.addEventListener('blur', onUp);
  });
  handle.addEventListener('keydown', event => {
    const {min, max} = limits();
    const heights = {ArrowUp: files.offsetHeight - 16, ArrowDown: files.offsetHeight + 16, Home: min, End: max};
    if (!(event.key in heights)) return;
    event.preventDefault();
    resize(heights[event.key]);
  });
  handle.addEventListener('dblclick', () => resize(defaultHeight));

  const observer = new ResizeObserver(updateHeight);
  observer.observe(sidebar);
  observer.observe(footer);
  updateHeight();
})();

// Sidebar resize
(function () {
  const handle = document.getElementById('resize-handle');
  const sidebar = document.getElementById('sidebar');
  let startX, startWidth;

  handle.addEventListener('mousedown', e => {
    startX = e.clientX;
    startWidth = sidebar.offsetWidth;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    function onMove(e) {
      const w = Math.max(160, Math.min(520, startWidth + e.clientX - startX));
      sidebar.style.width = w + 'px';
    }
    function onUp() {
      handle.classList.remove('dragging');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
})();
