import {readingOffset, scrollToOffset} from './render.js';
import {clearPlace} from './place.js';

const STORAGE_KEY = 'code-qda.reading';
const DEFAULTS = {font: 'system', size: '20', spacing: '1.6', theme: 'light', marks: 'color'};
const FONTS = {
  system: "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  arial: 'Arial, Helvetica, sans-serif',
  georgia: 'Georgia, serif'
};

export function initReading() {
  const menu = document.getElementById('reading-settings');
  const main = document.getElementById('main');
  const controls = [...menu.querySelectorAll('select')];
  let preferences = {...DEFAULTS};
  try {
    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY));
    for (const control of controls) {
      if ([...control.options].some(option => option.value === stored?.[control.name])) {
        preferences[control.name] = stored[control.name];
      }
    }
  } catch { /* Defaults also work when storage is unavailable or malformed. */ }

  function apply() {
    // Keep the same passage on screen without replacing text nodes or ranges.
    const offset = main.scrollTop > 0 ? readingOffset() : null;
    clearPlace();
    main.style.setProperty('--reading-font', FONTS[preferences.font]);
    main.style.setProperty('--reading-size', `${preferences.size}px`);
    main.style.setProperty('--reading-spacing', preferences.spacing);
    main.dataset.readingTheme = preferences.theme;
    main.dataset.readingMarks = preferences.marks;
    for (const control of controls) control.value = preferences[control.name];
    if (offset !== null) scrollToOffset(offset);
    main.dispatchEvent(new Event('readingchange'));
  }

  function save() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences)); }
    catch { /* Reading controls remain usable without persistent storage. */ }
    apply();
  }

  for (const control of controls) {
    control.addEventListener('change', () => {
      preferences[control.name] = control.value;
      save();
    });
  }
  document.getElementById('reading-reset').addEventListener('click', () => {
    preferences = {...DEFAULTS};
    save();
  });
  document.addEventListener('mousedown', event => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      menu.open = false;
      menu.querySelector('summary').focus();
    }
  });
  apply();
}
