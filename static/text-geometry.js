// A DOM Range returns separate (and sometimes duplicate) rectangles for the
// elements and text nodes on a line. Overlap boundaries are rendering details,
// so passage overlays merge those fragments back into one rectangle per line.
export function lineRects(range) {
  const rects = [...range.getClientRects()]
    .filter(rect => rect.width > 0 && rect.height > 0)
    .sort((a, b) => a.top - b.top || a.left - b.left);
  const lines = [];
  for (const rect of rects) {
    const line = lines[lines.length - 1];
    if (line && Math.abs(line.top - rect.top) < 2) {
      line.left = Math.min(line.left, rect.left);
      line.right = Math.max(line.right, rect.right);
      line.bottom = Math.max(line.bottom, rect.bottom);
    } else {
      lines.push({top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right});
    }
  }
  return lines.map(line => ({...line, width: line.right - line.left, height: line.bottom - line.top}));
}
