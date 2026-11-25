export const PALETTE = ['#a6cee3','#1f78b4','#b2df8a','#33a02c','#fb9a99','#e31a1c','#fdbf6f','#ff7f00','#cab2d6','#6a3d9a','#ffff99','#b15928'];

export const state = {
  currentFile: null,
  rawText: '',
  annotations: [],
  codeTree: [],
  overviewCountsById: new Map(),
  overviewLoaded: false,
  overviewCodeTree: [],
  overviewById: new Map(),
  currentOverviewCode: null,
  collapsed: new Set(),
  pendingSelection: null,
  pickerTarget: null,
  draggedCodeId: null,
  renamingCodeId: null,
  navigating: false
};

export function hashColor(name) {
  let hash = 5381;
  for (let index = 0; index < name.length; index += 1) {
    hash = ((hash << 5) + hash) ^ name.charCodeAt(index);
  }
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

export function getCodeById(codeId) {
  return state.codeTree.find(node => node.id === codeId) || null;
}

export function colorSortKey(color) {
  const paletteIndex = PALETTE.indexOf(color);
  if (paletteIndex >= 0) {
    return `${paletteIndex}`.padStart(4, '0');
  }
  return `z-${color || ''}`;
}

export function compareColors(aColor, bColor) {
  return colorSortKey(aColor).localeCompare(colorSortKey(bColor));
}

export function getEffectiveColor(codeId) {
  const seen = new Set();
  let current = getCodeById(codeId);
  let rootName = '';

  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    rootName = current.name;
    if (current.parentId === null && current.color) {
      return current.color;
    }
    current = current.parentId ? getCodeById(current.parentId) : null;
  }

  return hashColor(rootName || codeId || 'code');
}

export function compareNodes(a, b) {
  return compareColors(getEffectiveColor(a.id), getEffectiveColor(b.id))
    || (a.order ?? 0) - (b.order ?? 0)
    || a.name.localeCompare(b.name);
}

export function getChildren(parentId) {
  return sortSiblings(state.codeTree.filter(node => node.parentId === parentId), compareNodes);
}

function sortSiblings(nodes, compare) {
  // Keep the existing color grouping until the user explicitly places a code.
  // After that, insertion order wins so a drop stays exactly where it landed.
  return nodes.sort(nodes.some(node => node.manualOrder)
    ? (a, b) => (a.order ?? 0) - (b.order ?? 0) || compare(a, b)
    : compare);
}

// --- Overview tree (the no-file view over the shared codes.json) ---
// Same shape as codeTree, plus aggregate counts; mirrors the helpers above so
// both views share one mental model: drag to nest, reorder, or un-nest.

export function getOverviewEffectiveColor(nodeId) {
  const seen = new Set();
  let current = state.overviewById.get(nodeId);
  let rootName = '';

  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    rootName = current.name;
    if (current.parentId === null && current.color) {
      return current.color;
    }
    current = current.parentId ? state.overviewById.get(current.parentId) : null;
  }

  return hashColor(rootName || nodeId || 'code');
}

export function compareOverviewNodes(a, b) {
  return compareColors(getOverviewEffectiveColor(a.id), getOverviewEffectiveColor(b.id))
    || (a.order ?? 0) - (b.order ?? 0)
    || a.name.localeCompare(b.name);
}

export function getOverviewChildren(parentId) {
  return sortSiblings(state.overviewCodeTree.filter(node => (
    (node.parentId || null) === (parentId || null)
  )), compareOverviewNodes);
}

export function overviewIsDescendant(nodeId, possibleAncestorId) {
  const seen = new Set();
  let current = state.overviewById.get(nodeId);

  while (current && current.parentId && !seen.has(current.id)) {
    if (current.parentId === possibleAncestorId) {
      return true;
    }
    seen.add(current.id);
    current = state.overviewById.get(current.parentId);
  }

  return false;
}

export function nextOverviewOrder(parentId) {
  const siblings = getOverviewChildren(parentId);
  if (siblings.length === 0) {
    return 0;
  }
  return Math.max(...siblings.map(node => node.order ?? 0)) + 1;
}

export function getCodePath(codeId) {
  const parts = [];
  const seen = new Set();
  let current = getCodeById(codeId);

  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    parts.push(current.name);
    current = current.parentId ? getCodeById(current.parentId) : null;
  }

  return parts.reverse();
}

export function getCodePathLabel(codeId) {
  return getCodePath(codeId).join(' / ');
}

export function uid() {
  return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 7);
}

export function esc(text) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function nextOrder(parentId) {
  const siblings = getChildren(parentId);
  if (siblings.length === 0) {
    return 0;
  }
  return Math.max(...siblings.map(node => node.order ?? 0)) + 1;
}

export function sanitizeCodeTree(nodes) {
  const validNodes = [];
  const ids = new Set();

  nodes.forEach((node, index) => {
    if (!node || typeof node !== 'object') {
      return;
    }
    const id = typeof node.id === 'string' ? node.id.trim() : '';
    const name = typeof node.name === 'string' ? node.name.trim() : '';
    if (!id || !name || ids.has(id)) {
      return;
    }
    ids.add(id);
    validNodes.push({
      id,
      name,
      parentId: typeof node.parentId === 'string' && node.parentId.trim() ? node.parentId.trim() : null,
      color: typeof node.color === 'string' && node.color ? node.color : undefined,
      ...(node.manualOrder === true ? {manualOrder: true} : {}),
      order: Number.isFinite(node.order) ? node.order : index
    });
  });

  const byId = new Map(validNodes.map(node => [node.id, node]));
  validNodes.forEach(node => {
    if (node.parentId === node.id || !byId.has(node.parentId)) {
      node.parentId = null;
    }
  });

  validNodes.forEach(node => {
    const seen = new Set([node.id]);
    let currentId = node.parentId;
    while (currentId) {
      if (seen.has(currentId)) {
        node.parentId = null;
        break;
      }
      seen.add(currentId);
      const current = byId.get(currentId);
      if (!current) {
        node.parentId = null;
        break;
      }
      currentId = current.parentId;
    }
  });

  validNodes.forEach(node => {
    if (node.parentId !== null) {
      delete node.color;
    }
  });

  return validNodes;
}

export function sanitizeAnnotations(items, validIds) {
  return items
    .filter(item => item && typeof item === 'object')
    .map(item => ({
      ...item,
      id: typeof item.id === 'string' && item.id ? item.id : uid(),
      codeId: typeof item.codeId === 'string' ? item.codeId.trim() : '',
      text: typeof item.text === 'string' ? item.text : '',
      start: item.start,
      end: item.end
    }))
    .filter(item => validIds.has(item.codeId));
}

function migrateLegacyPayload(data) {
  const legacyCodes = Array.isArray(data?.codes) ? data.codes : [];
  const legacyColors = data?.colors && typeof data.colors === 'object' ? data.colors : {};
  const nodes = [];
  const byId = new Map();
  const legacyOrder = new Map();

  function nextLegacyOrder(parentId) {
    const current = legacyOrder.get(parentId) || 0;
    legacyOrder.set(parentId, current + 1);
    return current;
  }

  function ensurePath(path) {
    const parts = path.split('/').map(part => part.trim()).filter(Boolean);
    let currentPath = '';
    let parentId = null;

    parts.forEach(part => {
      currentPath = currentPath ? `${currentPath}/${part}` : part;
      if (!byId.has(currentPath)) {
        const node = {
          id: currentPath,
          name: part,
          parentId,
          order: nextLegacyOrder(parentId)
        };
        if (parentId === null && typeof legacyColors[currentPath] === 'string' && legacyColors[currentPath]) {
          node.color = legacyColors[currentPath];
        }
        byId.set(currentPath, node);
        nodes.push(node);
      }
      parentId = currentPath;
    });
  }

  legacyCodes.forEach(code => {
    const path = typeof code?.code === 'string' ? code.code.trim() : '';
    if (path) {
      ensurePath(path);
    }
  });

  Object.keys(legacyColors).forEach(path => {
    if (typeof path === 'string' && path.trim()) {
      ensurePath(path.trim());
    }
  });

  const annotations = legacyCodes
    .map(code => ({
      id: typeof code?.id === 'string' && code.id ? code.id : uid(),
      codeId: typeof code?.code === 'string' ? code.code.trim() : '',
      text: typeof code?.text === 'string' ? code.text : '',
      start: code?.start,
      end: code?.end
    }))
    .filter(code => code.codeId && byId.has(code.codeId));

  return normalizeCodesData({version: 2, codeTree: nodes, annotations});
}

export function normalizeCodesData(data) {
  if (Array.isArray(data)) {
    return migrateLegacyPayload({codes: data, colors: {}});
  }
  if (!data || typeof data !== 'object') {
    return {version: 2, codeTree: [], annotations: []};
  }
  if (data.version !== 2) {
    return migrateLegacyPayload(data);
  }

  const normalizedTree = sanitizeCodeTree(Array.isArray(data.codeTree) ? data.codeTree : []);
  const validIds = new Set(normalizedTree.map(node => node.id));
  return {
    version: 2,
    codeTree: normalizedTree,
    annotations: sanitizeAnnotations(Array.isArray(data.annotations) ? data.annotations : [], validIds)
  };
}
