const key = 'hedi.packaging.session';
export const session = {
  get() {
    try { return JSON.parse(sessionStorage.getItem(key)); }
    catch { return null; }
  },
  save(account) { sessionStorage.setItem(key, JSON.stringify(account)); },
  clear() { sessionStorage.removeItem(key); },
};

async function request(path, data, method = 'GET') {
  const token = session.get()?.access_token;
  const response = await fetch(`/api${path}`, {
    method,
    headers: {
      ...(token ? {Authorization:`Bearer ${token}`} : {}),
      ...(data ? {'Content-Type':'application/json'} : {}),
    },
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (response.status === 401 && path !== '/login') {
    session.clear();
    window.dispatchEvent(new Event('session-expired'));
  }
  if (!response.ok) {
    const detail = result.detail;
    const message = Array.isArray(detail) ? detail.map(item => `${item.loc.at(-1)} · ${item.msg}`).join('；') : detail?.message || detail;
    const channels = [...(detail?.missing || []), ...(detail?.exceeds || [])];
    const error = new Error(message || `请求失败 (${response.status})`);
    error.channels = channels;
    throw error;
  }
  return result;
}

export const api = {
  login: payload => request('/login', payload, 'POST'),
  lots: () => request('/lots'),
  materials: () => request('/materials'),
  detail: id => request(`/lots/${id}`),
  register: payload => request('/lots', payload, 'POST'),
  action: (id, endpoint, payload) => request(`/lots/${id}/${endpoint}`, payload, 'POST'),
  rolls: () => request('/training/rolls'),
  registerRoll: payload => request('/training/rolls', payload, 'POST'),
  correctRoll: payload => request('/training/rolls/correct', payload, 'POST'),
  withdrawSample: payload => request('/training/samples/withdraw', payload, 'POST'),
  packages: () => request('/training/packages'),
  publishPackage: payload => request('/training/packages', payload, 'POST'),
  packageDetail: id => request(`/training/packages/${id}`),
  packageLink: id => request(`/training/packages/${id}/link`, {}, 'POST'),
  packageDownload: async (id, token) => {
    const response = await fetch(`/api/training/packages/${id}/download?token=${encodeURIComponent(token)}`, {
      headers: {Authorization: `Bearer ${session.get()?.access_token || ''}`},
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `下载失败 (${response.status})`);
    return result;
  },
};
