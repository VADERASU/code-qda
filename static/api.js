export async function apiFetch(path) {
  const response = await fetch(path);
  if (!response.ok) {
    throw new Error(response.status);
  }
  return response.json();
}

export async function apiPost(path, data) {
  return fetch(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  });
}
