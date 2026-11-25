// Shared, read-only matching for the sidebar and code-entry popover.
// Positions refer to the original label so normalization never changes names.
function searchable(text) {
  let value = '';
  const positions = [];
  let offset = 0;
  for (const character of text) {
    const end = offset + character.length;
    for (const normalized of character.normalize('NFKC').toLowerCase()) {
      if (/[\p{L}\p{M}\p{N}+#]/u.test(normalized)) {
        value += normalized;
        for (let i = 0; i < normalized.length; i++) positions.push([offset, end]);
      } else if (value && !value.endsWith(' ')) {
        value += ' ';
        positions.push([offset, end]);
      }
    }
    offset = end;
  }
  value = value.trimEnd();
  const words = [...value.matchAll(/\S+/g)].map(match => ({
    text: match[0], start: match.index, end: match.index + match[0].length
  }));
  return {value, words, positions};
}

// One insertion, deletion, substitution, or adjacent transposition. Short
// words and identifiers such as UR5 require literal/prefix matching.
function oneTypo(a, b) {
  if (a.length < 5 || b.length < 5 || !/^\p{L}+$/u.test(a + b)
      || Math.abs(a.length - b.length) > 1) return false;
  let i = 0;
  while (i < Math.min(a.length, b.length) && a[i] === b[i]) i++;
  if (a.length === b.length) {
    return a.slice(i + 1) === b.slice(i + 1)
      || (a[i] === b[i + 1] && a[i + 1] === b[i]
          && a.slice(i + 2) === b.slice(i + 2));
  }
  return a.length > b.length ? a.slice(i + 1) === b.slice(i)
    : a.slice(i) === b.slice(i + 1);
}

function mergeRanges(ranges) {
  const merged = [];
  for (const [start, end] of ranges.sort((a, b) => a[0] - b[0])) {
    const previous = merged.at(-1);
    if (previous && start <= previous[1]) previous[1] = Math.max(previous[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

function fieldMatch(field, query, allowTypo) {
  const index = field.value.indexOf(query.value);
  let strength;
  let ranges;
  let typo = false;
  if (index >= 0) {
    strength = field.value === query.value ? 100 : index === 0 ? 90 : 80;
    ranges = [[index, index + query.value.length]];
  } else {
    const used = new Set();
    ranges = [];
    // Specific words first, so a short prefix cannot consume a later exact word.
    const words = [...query.words].sort((a, b) => b.text.length - a.text.length);
    for (const word of words) {
      const candidates = field.words.map((candidate, i) => ({candidate, i,
        cost: candidate.text === word.text ? 0 : candidate.text.startsWith(word.text) ? 1
          : allowTypo && oneTypo(word.text, candidate.text) ? 2 : Infinity
      })).filter(entry => !used.has(entry.i) && entry.cost < Infinity)
        .sort((a, b) => a.cost - b.cost || a.i - b.i);
      const match = candidates.find(entry => entry.cost < 2 || !typo);
      if (!match) return null;
      used.add(match.i);
      typo ||= match.cost === 2;
      ranges.push([match.candidate.start, match.cost === 2 ? match.candidate.end
        : match.candidate.start + word.text.length]);
    }
    strength = typo ? 10 : 70;
  }
  return {strength, typo, ranges: mergeRanges(ranges.map(([start, end]) => (
    [field.positions[start][0], field.positions[end - 1][1]]
  )))};
}

export function searchCodes(tree, rawQuery) {
  const query = searchable(rawQuery);
  if (rawQuery.trim() && !query.value) return [];
  const byId = new Map(tree.map(node => [node.id, node]));
  const literalPath = rawQuery.split('/').map(part => part.trim()).filter(Boolean)
    .join(' / ').toLowerCase();
  const entries = tree.map(node => {
    const ancestors = [];
    const seen = new Set();
    let current = node;
    while (current && !seen.has(current.id)) {
      seen.add(current.id);
      ancestors.unshift(current);
      current = byId.get(current.parentId);
    }
    let label = '';
    const path = ancestors.map(ancestor => {
      if (label) label += ' / ';
      const start = label.length;
      label += ancestor.name;
      return {node: ancestor, start, end: label.length};
    });
    return {node, path, label, name: searchable(node.name), full: searchable(label),
      exactPath: Boolean(literalPath) && label.toLowerCase() === literalPath,
      exactName: Boolean(rawQuery.trim()) && node.name.toLowerCase() === rawQuery.trim().toLowerCase()};
  });

  function match(entry, allowTypo) {
    const {node, path, label, exactName, exactPath} = entry;
    if (!query.value) return {node, path, label, score: 1, ranges: [], exactName, exactPath};
    const nameMatch = fieldMatch(entry.name, query, allowTypo);
    const pathMatch = fieldMatch(entry.full, query, allowTypo);
    const choices = [];
    if (nameMatch) choices.push({...nameMatch,
      score: (nameMatch.typo ? 100 : 1000) + nameMatch.strength,
      ranges: nameMatch.ranges.map(([start, end]) => [start + path.at(-1).start, end + path.at(-1).start])});
    if (pathMatch) choices.push({...pathMatch,
      score: (pathMatch.typo ? 50 : rawQuery.includes('/') ? 1100 : 500) + pathMatch.strength});
    choices.sort((a, b) => b.score - a.score);
    const best = choices[0];
    return best ? {node, path, label, ...best, exactName, exactPath} : null;
  }

  // Spelling suggestions are a fallback, not noise alongside literal matches.
  let matches = entries.map(entry => match(entry, false)).filter(Boolean);
  if (!matches.length) matches = entries.map(entry => match(entry, true)).filter(Boolean);
  return matches.sort((a, b) => b.score - a.score
    || Number(b.exactPath) - Number(a.exactPath)
    || Number(b.exactName) - Number(a.exactName) || a.label.localeCompare(b.label));
}
