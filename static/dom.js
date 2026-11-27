// Build elements with DOM APIs (textContent), never by pasting account or
// project data into HTML strings. Shared by the projects and admin pages.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') el.className = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, '');
    else if (value !== false && value != null) el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child != null) el.append(child instanceof Node ? child : String(child));
  }
  return el;
}
